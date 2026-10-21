"""一致性检查 ``ctrlstore fsck``（企划第 11.5 节）。

默认只报告，不删任何原件。``--fix-public-links`` 只修公开区的链接，
而且严格按「已提交的发布状态」来修：不会把待发布、已撤回或回收站里的内容重新公开。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .assets import publish
from .models import Asset, AssetVersion
from .storage import LocalStorage

#: 问题代号，供脚本判断。
MISSING_FILE = "missing_file"
ORPHAN_FILE = "orphan_file"
MISSING_LINK = "missing_link"
STALE_LINK = "stale_link"
HASH_MISMATCH = "hash_mismatch"
TEMP_LINK = "temp_link"


#: 一条问题的说明。``fixable`` 表示 --fix-public-links 能处理它。
@dataclass(frozen=True)
class Issue:
    code: str
    detail: str
    fixable: bool = False


@dataclass
class Report:
    """一次检查的结果。"""

    issues: list[Issue] = field(default_factory=list)
    fixed: int = 0
    checked_files: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def add(self, code: str, detail: str, *, fixable: bool = False) -> None:
        self.issues.append(Issue(code=code, detail=detail, fixable=fixable))


def run(
    db: DbSession,
    *,
    storage: LocalStorage,
    fix_public_links: bool = False,
    check_hashes: bool = True,
) -> Report:
    """按数据库记录与磁盘状态互相比对。"""
    report = Report()
    versions = db.scalars(select(AssetVersion)).all()
    assets = {asset.id: asset for asset in db.scalars(select(Asset)).all()}

    _check_files(storage, versions, report, check_hashes=check_hashes)
    _check_orphans(storage, versions, report)
    _check_public_area(storage, assets, report, fix=fix_public_links)
    _check_missing_links(storage, assets, report, fix=fix_public_links)
    return report


def _check_files(
    storage: LocalStorage, versions: list[AssetVersion], report: Report, *, check_hashes: bool
) -> None:
    for version in versions:
        report.checked_files += 1
        path = storage.original_path(version.storage_key)
        if not path.is_file():
            report.add(
                MISSING_FILE,
                f"{version.storage_key}（内容 {version.asset_id} v{version.version_no}）",
            )
            continue
        if not check_hashes:
            continue
        digest, size = storage.hash_file(path)
        if digest != version.sha256 or size != version.byte_size:
            report.add(
                HASH_MISMATCH,
                f"{version.storage_key}：库里 {version.sha256[:12]}…/{version.byte_size} 字节，"
                f"磁盘 {digest[:12]}…/{size} 字节",
            )


def _check_orphans(storage: LocalStorage, versions: list[AssetVersion], report: Report) -> None:
    """有文件但没有记录。只报告，不自动删——可能是手工放进去的备份。"""
    known = {storage.original_path(version.storage_key) for version in versions}
    for path in storage.stored_files():
        if path not in known:
            report.add(ORPHAN_FILE, str(path.relative_to(storage.originals_dir)))


def _check_public_area(
    storage: LocalStorage, assets: dict[str, Asset], report: Report, *, fix: bool
) -> None:
    """公开区里不该存在的东西：多余链接与 ``.tmp`` 残留。"""
    for asset_id in storage.public_asset_ids():
        files = storage.public_files(asset_id)
        temps = [path for path in files if path.name.endswith(".tmp")]
        for path in temps:
            report.add(TEMP_LINK, str(path.relative_to(storage.public_dir)), fixable=True)

        asset = assets.get(asset_id)
        # 应有的链接由 publish 自己按「已发布版本」推导：替换过文件但还没发布时，
        # 公开区仍应指向旧版，不能拿当前版本去判断。
        wanted = publish.desired_links(asset) if asset else None
        expected = (
            {path.relative_to(storage.public_dir) for path in wanted.targets(storage)}
            if wanted
            else set()
        )
        actual = {
            path.relative_to(storage.public_dir) for path in files if not path.name.endswith(".tmp")
        }
        stale = sorted(actual - expected)
        for relative in stale:
            report.add(STALE_LINK, str(relative), fixable=True)

        if fix and (temps or stale):
            # 整个目录重建最省事：旧扩展名、旧版本号、.tmp 一次清干净。
            storage.clear_public(asset_id)
            report.fixed += 1
            if wanted is not None:
                _relink(storage, asset, report)


def _check_missing_links(
    storage: LocalStorage, assets: dict[str, Asset], report: Report, *, fix: bool
) -> None:
    """该公开却没有链接的内容。判断同样以已发布版本为准。"""
    for asset in assets.values():
        wanted = publish.desired_links(asset)
        if wanted is None:
            continue
        for path in wanted.targets(storage):
            if not path.is_file():
                report.add(MISSING_LINK, str(path.relative_to(storage.public_dir)), fixable=True)
        if fix:
            _relink(storage, asset, report)


def _relink(storage: LocalStorage, asset: Asset, report: Report) -> None:
    """按已提交状态重建链接。失败记一条问题，不抛。"""
    try:
        publish.sync_links(storage, asset)
        report.fixed += 1
    except Exception as exc:  # 链接失败要让运维看到，但不该中断整轮检查
        report.add(MISSING_LINK, f"重建失败：{asset.id}（{exc}）")
