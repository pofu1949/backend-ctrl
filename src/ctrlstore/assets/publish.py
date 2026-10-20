"""发布、撤回、归档、回收站与公开链接。

只有「已发布且可见性允许」的内容才在 ``public/`` 下有链接（企划第 12.7 节）。
链接由 :func:`desired_links` 一处推导，发布、改可见性、fsck 修复都调用它，
所以「什么该公开」只有一份判断。

失败时的落点都是「更不可访问」：

* 发布：先提交状态（占住「已发布」），再建链接。链接失败 = 暂时 404，fsck 可修；
  反过来若先建链接再提交，提交失败就会让未发布内容短暂可访问。
* 撤回/归档/进回收站：先删链接，再提交状态。删链接失败 = 404，链接残留由 fsck 清掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import audit
from ..errors import ApiError
from ..models import (
    ASSET_ARCHIVED,
    ASSET_DRAFT,
    ASSET_PUBLISHED,
    ASSET_TRASHED,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNLISTED,
    Asset,
    AssetReference,
    AssetVersion,
    User,
    utcnow,
)
from ..storage import LocalStorage, keys

#: 会出现在 ``/p/`` 下的可见性。仅站点与私有都不进公开区。
PUBLIC_VISIBILITIES = frozenset({VISIBILITY_PUBLIC, VISIBILITY_UNLISTED})

#: 允许发布的状态。
PUBLISHABLE_STATUSES = frozenset({ASSET_DRAFT, ASSET_ARCHIVED})


@dataclass(frozen=True)
class Links:
    """一个内容在公开区应有的文件。"""

    asset_id: str
    version_no: int
    extension: str

    def paths(self, storage: LocalStorage) -> list[Path]:
        return [
            storage.public_current_path(self.asset_id, self.extension),
            storage.public_version_path(self.asset_id, self.version_no, self.extension),
        ]


def published_version(asset: Asset) -> AssetVersion | None:
    """线上那一版。它可能与 ``current_version_id`` 不是同一行——

    发布之后又替换文件时，``current`` 指向待发布的新版本，``published`` 仍是线上那版。
    公开区的判断必须用这一个，不能用「当前版本」。
    """
    if asset.published_version_id is None:
        return None
    for version in asset.versions:
        if version.id == asset.published_version_id:
            return version
    return None


def desired_links(asset: Asset) -> Links | None:
    """按已提交状态推导应有的公开文件；不该公开就返回 ``None``。

    这里自己取已发布版本，不接受调用方传入：传错一次（比如传成当前版本）就会
    把仍在线上的一版判成"该删"。
    """
    if asset.status != ASSET_PUBLISHED or asset.visibility not in PUBLIC_VISIBILITIES:
        return None
    version = published_version(asset)
    if version is None:
        return None
    return Links(
        asset_id=asset.id,
        version_no=version.version_no,
        extension=_extension_of(version.storage_key),
    )


def publish(
    db: DbSession, *, storage: LocalStorage, asset: Asset, revision: int, actor: User
) -> Asset:
    """把当前版本发布成线上版本，并复制发布快照。"""
    _ensure_revision(asset, revision)
    if asset.status == ASSET_TRASHED:
        raise ApiError.conflict("回收站中的内容不能发布")

    version = _current_version(asset)
    _ensure_references_published(db, asset)
    if asset.status == ASSET_PUBLISHED and asset.published_version_id == version.id:
        return asset  # 幂等：同一版本重复发布，不改 revision

    snapshot = {
        "published_version_id": version.id,
        "published_title": asset.title,
        "published_summary": asset.summary,
        "published_cover_asset_id": asset.cover_asset_id,
        "published_tags": _tag_snapshot(asset),
    }
    for name, value in snapshot.items():
        setattr(asset, name, value)
    asset.status = ASSET_PUBLISHED
    asset.published_at = utcnow()
    asset.revision += 1
    asset.updated_at = utcnow()
    audit.record_for_user(
        db,
        actor,
        action=audit.ASSET_PUBLISHED,
        target_type=audit.TARGET_ASSET,
        target_id=asset.id,
        detail={"version_no": version.version_no, "visibility": asset.visibility},
    )

    # 先提交状态再动文件：链接失败只会让公开地址 404，不会泄露未发布内容。
    db.commit()
    try:
        sync_links(storage, asset)
    except (ApiError, OSError) as exc:
        # 状态已落库，链接没建成：公开地址会是 404，而不是半个文件。
        raise ApiError.conflict(
            f"发布状态已保存，但公开文件未能建立（{exc}）；请运行 ctrlstore fsck --fix-public-links"
        ) from exc
    return asset


def unpublish(
    db: DbSession, *, storage: LocalStorage, asset: Asset, revision: int, actor: User
) -> Asset:
    """撤回发布：内容回到草稿，公开地址立即 404。"""
    _ensure_revision(asset, revision)
    if asset.status != ASSET_PUBLISHED:
        raise ApiError.conflict("只有已发布的内容可以撤回")

    storage.clear_public(asset.id)
    _reset_snapshot(asset)
    asset.status = ASSET_DRAFT
    asset.revision += 1
    asset.updated_at = utcnow()
    audit.record_for_user(
        db,
        actor,
        action=audit.ASSET_UNPUBLISHED,
        target_type=audit.TARGET_ASSET,
        target_id=asset.id,
    )
    return asset


def archive(
    db: DbSession, *, storage: LocalStorage, asset: Asset, revision: int, actor: User
) -> Asset:
    """归档：下架但保留内容，不自动重新公开。"""
    _ensure_revision(asset, revision)
    if asset.status == ASSET_TRASHED:
        raise ApiError.conflict("回收站中的内容不能归档")

    storage.clear_public(asset.id)
    _reset_snapshot(asset)
    asset.status = ASSET_ARCHIVED
    asset.revision += 1
    asset.updated_at = utcnow()
    audit.record_for_user(
        db, actor, action=audit.ASSET_ARCHIVED, target_type=audit.TARGET_ASSET, target_id=asset.id
    )
    return asset


def trash(
    db: DbSession, *, storage: LocalStorage, asset: Asset, revision: int, actor: User
) -> Asset:
    """放入回收站：先撤下公开链接，再改状态。"""
    _ensure_revision(asset, revision)
    if asset.status == ASSET_TRASHED:
        return asset

    storage.clear_public(asset.id)
    _reset_snapshot(asset)
    asset.status = ASSET_TRASHED
    asset.trashed_at = utcnow()
    asset.revision += 1
    asset.updated_at = utcnow()
    audit.record_for_user(
        db, actor, action=audit.ASSET_TRASHED, target_type=audit.TARGET_ASSET, target_id=asset.id
    )
    return asset


def restore(
    db: DbSession, *, storage: LocalStorage, asset: Asset, revision: int, actor: User
) -> Asset:
    """从回收站恢复。只改状态，不自动重新公开（企划第 8.6 节）。"""
    _ensure_revision(asset, revision)
    if asset.status != ASSET_TRASHED:
        raise ApiError.conflict("只有回收站中的内容可以恢复")

    storage.clear_public(asset.id)
    asset.status = ASSET_DRAFT
    asset.trashed_at = None
    asset.revision += 1
    asset.updated_at = utcnow()
    audit.record_for_user(
        db, actor, action=audit.ASSET_RESTORED, target_type=audit.TARGET_ASSET, target_id=asset.id
    )
    return asset


def purge(db: DbSession, *, storage: LocalStorage, asset: Asset, actor: User) -> None:
    """彻底删除（仅所有者）。必须已在回收站，且没有被其他内容当封面引用。"""
    if asset.status != ASSET_TRASHED:
        raise ApiError.conflict("只有回收站中的内容可以彻底删除")
    referrers = db.scalars(
        select(Asset.id).where(Asset.cover_asset_id == asset.id, Asset.id != asset.id)
    ).all()
    if referrers:
        raise ApiError.conflict("仍有内容把它当封面，先解除引用：" + ", ".join(sorted(referrers)))

    for version in list(asset.versions):
        storage.discard_original(version.storage_key)
    storage.clear_public(asset.id)
    db.delete(asset)
    audit.record_for_user(
        db,
        actor,
        action=audit.ASSET_PURGED,
        target_type=audit.TARGET_ASSET,
        target_id=asset.id,
        detail={"kind": asset.kind},
    )


def sync_links(storage: LocalStorage, asset: Asset) -> bool:
    """让公开区与已提交状态一致。返回是否产生了链接。

    判断与源文件都取「已发布版本」：替换过文件但还没发布时，公开区应当继续指向旧版。
    """
    wanted = desired_links(asset)
    if wanted is None:
        storage.clear_public(asset.id)
        return False

    version = published_version(asset)
    if version is None:  # desired_links 已经保证不会为空，这里只是收窄类型
        storage.clear_public(asset.id)
        return False

    source = storage.original_path(version.storage_key)
    if not source.is_file():
        raise ApiError.conflict("已发布版本的文件不存在")

    # 先清掉旧扩展名/旧版本号留下的链接：扩展名随真实类型变化，旧地址必须 404。
    storage.clear_public(asset.id)
    storage.link_public(source, asset_id=asset.id, targets=wanted.paths(storage))
    return True


# —— 内部 ——


def _ensure_revision(asset: Asset, revision: int) -> None:
    if asset.revision != revision:
        raise ApiError.conflict(f"内容已被修改（当前修订号 {asset.revision}），请重新读取后再提交")


def _current_version(asset: Asset) -> AssetVersion:
    """当前版本。抽取失败不挡发布——发布只依赖原件（企划第 8.5 节）。"""
    for version in asset.versions:
        if version.id == asset.current_version_id:
            return version
    raise ApiError.conflict("内容还没有可发布的版本")


def _ensure_references_published(db: DbSession, asset: Asset) -> None:
    """正文引用的图片必须先发布（企划第 8.4 节 T4）。"""
    targets = db.scalars(
        select(AssetReference.to_asset_id).where(AssetReference.from_asset_id == asset.id)
    ).all()
    if not targets:
        return
    published = set(
        db.scalars(
            select(Asset.id).where(Asset.id.in_(targets), Asset.status == ASSET_PUBLISHED)
        ).all()
    )
    missing = sorted(set(targets) - published)
    if missing:
        raise ApiError.conflict("引用的图片尚未发布：" + ", ".join(missing))


def _reset_snapshot(asset: Asset) -> None:
    """离开已发布状态就清空快照。

    ``published_version_id`` 非空当且仅当当前是已发布状态——公开查询只靠这一个条件，
    不需要再判断状态，少一个出错的地方。
    """
    asset.published_version_id = None
    asset.published_title = None
    asset.published_summary = None
    asset.published_cover_asset_id = None
    asset.published_tags = None
    asset.published_at = None


def _tag_snapshot(asset: Asset) -> str:
    return json.dumps(sorted(tag.name for tag in asset.tags), ensure_ascii=False)


def _extension_of(storage_key: str) -> str:
    """从存储键取扩展名。键的形状由 ``storage.keys`` 保证。"""
    keys.validate(storage_key)
    return storage_key.rsplit(".", 1)[-1]


def is_publicly_visible(asset: Asset) -> bool:
    """公开读取的唯一判据：已发布且可见性允许。"""
    return asset.status == ASSET_PUBLISHED and asset.visibility in PUBLIC_VISIBILITIES
