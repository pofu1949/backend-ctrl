"""内容登记、元数据与版本。

三条不变量，写在这里也由库层约束兜底：

1. **版本文件写完不再改。** 替换只会新增 ``asset_versions`` 行，`(asset_id, version_no)` 唯一。
2. **``current_version_id`` 指向最新，``published_version_id`` 指向线上。** 第 4 部分只写前者，
   后者留空到第 5 部分发布；替换永远不碰它。
3. **保留份数不能删掉指针占用的版本。** 图片与文档留 5 版，视频留 2 版；
   ``current`` 与 ``published`` 指向的版本始终保留。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession

from .. import audit
from ..config import Settings
from ..errors import ApiError
from ..models import (
    ASSET_DRAFT,
    VISIBILITY_PRIVATE,
    Asset,
    AssetReference,
    AssetVersion,
    Folder,
    Tag,
    UploadSession,
    User,
    utcnow,
)
from ..storage import LocalStorage, keys
from ..uploads.inspection import KIND_MARKDOWN, KIND_TEXT, KIND_VIDEO, Inspection
from . import markdown

#: 版本保留：图片、文档留最近 5 版。
MAX_VERSIONS = 5

#: 视频留待发布版与已发布版，因此上限 2。
MAX_VERSIONS_VIDEO = 2

#: 列表默认与最大条数（企划第 13.2 节）。
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: 可以由 PATCH 修改的字段。其余字段只能通过发布或专门的接口改变。
METADATA_FIELDS = (
    "title",
    "summary",
    "description",
    "alt",
    "language",
    "slug",
    "folder_id",
    "cover_asset_id",
    "visibility",
)


def register_upload(
    db: DbSession,
    *,
    settings: Settings,
    storage: LocalStorage,
    session: UploadSession,
    merged: Path,
    inspection: Inspection,
) -> tuple[Asset, AssetVersion]:
    """把一次已完成的上传登记成内容（新内容或该内容的新版本）。

    ``session.asset_id`` 为空表示新建；有值表示替换那条内容。
    字节已经在临时目录里校验通过，这里只负责按最终键落盘并写库。
    返回 (内容, 新版本)：调用方不用再去猜哪一版是当前版本。
    """
    owner = db.get(User, session.actor_id)
    if owner is None:
        raise ApiError.invalid("上传人已不存在")

    if session.asset_id:
        asset = get_asset(db, session.asset_id)
        _ensure_replaceable(asset, inspection)
        version_no = next_version_no(db, asset)
    else:
        asset = Asset(
            kind=inspection.kind,
            folder_id=None,
            owner_id=owner.id,
            title=title_from_filename(session.original_filename),
            language="zh-CN",
            status=ASSET_DRAFT,
            visibility=VISIBILITY_PRIVATE,
            revision=1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(asset)
        db.flush()
        version_no = 1

    version = _add_version(
        db,
        storage=storage,
        asset=asset,
        version_no=version_no,
        source=merged,
        inspection=inspection,
        filename=session.original_filename,
    )
    asset.current_version_id = version.id
    if version_no > 1:
        asset.revision += 1
    asset.updated_at = utcnow()

    prune_versions(db, storage=storage, asset=asset)
    audit.record(
        db,
        action=audit.ASSET_VERSION_ADDED if version_no > 1 else audit.ASSET_CREATED,
        actor_type=audit.ACTOR_USER,
        actor_id=owner.id,
        target_type=audit.TARGET_ASSET,
        target_id=asset.id,
        detail={
            "kind": asset.kind,
            "version_no": version_no,
            "sha256": inspection.sha256,
            "bytes": inspection.byte_size,
        },
    )
    return asset, version


def create_text(
    db: DbSession,
    *,
    settings: Settings,
    storage: LocalStorage,
    actor: User,
    kind: str,
    title: str | None,
    body: str,
    folder_id: str | None = None,
    tags: Iterable[str] = (),
    slug: str | None = None,
    summary: str | None = None,
) -> tuple[Asset, AssetVersion]:
    """直接创建 Markdown 或纯文本（企划第 8.4 节 T1）。

    内容与上传走同一套版本结构：正文就是 ``v1`` 的文件，因此发布、替换、
    保留规则都只有一份实现。返回 (内容, 首版)。
    """
    if kind not in (KIND_MARKDOWN, KIND_TEXT):
        raise ApiError.invalid("只能直接创建 markdown 或 text")

    parsed = markdown.parse(body) if kind == KIND_MARKDOWN else None
    final_title = (title or (parsed.title if parsed else None) or _first_line(body)).strip()
    if not final_title:
        raise ApiError.invalid("标题不能为空")

    data = body.encode("utf-8")
    limit = settings.limits.for_kind(kind)
    if len(data) > limit:
        raise ApiError.too_large(f"正文超过 {kind} 的上限（{limit} 字节）")

    asset = Asset(
        kind=kind,
        folder_id=_folder_id(db, folder_id),
        owner_id=actor.id,
        title=final_title[:255],
        summary=(summary or (parsed.summary if parsed else None) or "")[:1024],
        language=(parsed.language if parsed and parsed.language else "zh-CN"),
        slug=_clean_slug(slug or (parsed.slug if parsed else None)),
        status=ASSET_DRAFT,
        visibility=VISIBILITY_PRIVATE,
        revision=1,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(asset)
    db.flush()

    storage_key = keys.build(asset.id, 1, _extension(kind))
    storage.write_original(data, storage_key)
    version = AssetVersion(
        asset_id=asset.id,
        version_no=1,
        original_filename=f"{asset.slug or asset.id}.{_extension(kind)}",
        mime="text/markdown" if kind == KIND_MARKDOWN else "text/plain",
        byte_size=len(data),
        sha256=_sha256(data),
        storage_key=storage_key,
        extract_status="pending",
        created_at=utcnow(),
    )
    asset.versions.append(version)
    db.flush()
    asset.current_version_id = version.id

    tag_names = list(tags) or (parsed.tags if parsed else [])
    if tag_names:
        set_tags(db, asset, tag_names, actor=actor)
    if kind == KIND_MARKDOWN:
        refresh_references(db, asset, body=body)
    if parsed and parsed.cover:
        asset.cover_asset_id = resolve_cover(db, markdown.cover_id(parsed.cover))

    audit.record_for_user(
        db,
        actor,
        action=audit.ASSET_CREATED,
        target_type=audit.TARGET_ASSET,
        target_id=asset.id,
        detail={"kind": kind, "bytes": len(data)},
    )
    return asset, version


@dataclass(frozen=True)
class Update:
    """一次修改请求的内容。字段都可空：只改给出的部分。"""

    changes: dict[str, object] = field(default_factory=dict)
    tags: list[str] | None = None
    #: 只有 Markdown 与纯文本可以直接改正文，改正文会产生新版本。
    body: str | None = None


def apply_update(
    db: DbSession,
    *,
    settings: Settings,
    storage: LocalStorage,
    asset: Asset,
    update: Update,
    expected_revision: int,
    actor: User,
) -> Asset:
    """改元数据、标签、正文。``revision`` 不一致就 409，避免两个人互相覆盖。

    正文的修改会写成新版本（版本文件写完不再改），因此与替换文件同义：
    都只动编辑中的内容，线上快照要等发布（第 5 部分）。
    一次请求只加一次修订号。
    """
    if asset.revision != expected_revision:
        raise ApiError.conflict(f"内容已被修改（当前修订号 {asset.revision}），请重新读取后再提交")

    unknown = sorted(set(update.changes) - set(METADATA_FIELDS))
    if unknown:
        raise ApiError.invalid(f"不能修改这些字段：{', '.join(unknown)}")

    # 请求里显式给出的字段优先于正文 front matter。
    explicit = set(update.changes) | ({"tags"} if update.tags is not None else set())
    applied: dict[str, object] = {}

    parsed: markdown.Parsed | None = None
    if update.body is not None:
        parsed = _write_body(db, settings=settings, storage=storage, asset=asset, body=update.body)
        applied["body"] = len(update.body.encode("utf-8"))

    applied |= _apply_front_matter(db, asset, parsed, explicit=explicit)
    applied |= _apply_changes(db, asset, update.changes)

    tag_names = update.tags if update.tags is not None else (parsed.tags if parsed else None)
    if tag_names is not None:
        set_tags(db, asset, tag_names, actor=actor)
        applied["tags"] = sorted(tag.name for tag in asset.tags)

    if not applied:
        return asset

    asset.revision += 1
    asset.updated_at = utcnow()
    audit.record_for_user(
        db,
        actor,
        action=audit.ASSET_UPDATED,
        target_type=audit.TARGET_ASSET,
        target_id=asset.id,
        detail={"fields": sorted(applied)},
    )
    return asset


def _write_body(
    db: DbSession, *, settings: Settings, storage: LocalStorage, asset: Asset, body: str
) -> markdown.Parsed | None:
    """把正文写成新版本。只有文本类内容可以直接改正文。"""
    if asset.kind not in (KIND_MARKDOWN, KIND_TEXT):
        raise ApiError.invalid("只有 Markdown 与纯文本可以直接改正文，其他类型请替换文件")

    data = body.encode("utf-8")
    limit = settings.limits.for_kind(asset.kind)
    if len(data) > limit:
        raise ApiError.too_large(f"正文超过 {asset.kind} 的上限（{limit} 字节）")

    version_no = next_version_no(db, asset)
    extension = _extension(asset.kind)
    storage_key = keys.build(asset.id, version_no, extension)
    storage.write_original(data, storage_key)
    version = AssetVersion(
        asset_id=asset.id,
        version_no=version_no,
        original_filename=f"{asset.slug or asset.id}-v{version_no}.{extension}",
        mime="text/markdown" if asset.kind == KIND_MARKDOWN else "text/plain",
        byte_size=len(data),
        sha256=_sha256(data),
        storage_key=storage_key,
        extract_status="pending",
        created_at=utcnow(),
    )
    asset.versions.append(version)
    db.flush()
    asset.current_version_id = version.id

    parsed = markdown.parse(body) if asset.kind == KIND_MARKDOWN else None
    if parsed is not None:
        refresh_references(db, asset, body=body)
    prune_versions(db, storage=storage, asset=asset)
    return parsed


def _apply_front_matter(
    db: DbSession, asset: Asset, parsed: markdown.Parsed | None, *, explicit: set[str]
) -> dict[str, object]:
    """正文 front matter 映射到字段；请求里显式给出的字段不覆盖。"""
    if parsed is None:
        return {}
    applied: dict[str, object] = {}
    candidates = {
        "title": (parsed.title[:255] if parsed.title else None),
        "slug": _clean_slug(parsed.slug),
        "summary": (parsed.summary[:1024] if parsed.summary else None),
        "language": parsed.language,
        "cover_asset_id": resolve_cover(db, markdown.cover_id(parsed.cover)),
    }
    for name, value in candidates.items():
        if value is None or name in explicit:
            continue
        if getattr(asset, name) != value:
            setattr(asset, name, value)
            applied[name] = value
    return applied


def _apply_changes(db: DbSession, asset: Asset, changes: dict[str, object]) -> dict[str, object]:
    """按字段值规整后写入，返回实际变化的字段。"""
    applied: dict[str, object] = {}
    for name, value in changes.items():
        if name == "folder_id":
            value = _folder_id(db, _as_str(value))
        elif name == "cover_asset_id":
            value = resolve_cover(db, _as_str(value))
        elif name == "slug":
            value = _clean_slug(_as_str(value))
        elif isinstance(value, str):
            value = value.strip()
        if getattr(asset, name) != value:
            setattr(asset, name, value)
            applied[name] = value
    return applied


def set_tags(db: DbSession, asset: Asset, names: Iterable[str], *, actor: User) -> Asset:
    """整体替换标签。名称不存在就创建，因此调用方只需要给名字。"""
    from ..tags import service as tag_service

    tags: list[Tag] = tag_service.ensure_tags(db, names, actor=actor)
    asset.tags = tags
    return asset


def refresh_references(db: DbSession, asset: Asset, *, body: str) -> None:
    """重算正文引用的图片。只记录库里确实存在且是图片的内容。"""
    ids = markdown.referenced_ids(body)
    current = db.scalars(
        select(AssetReference).where(AssetReference.from_asset_id == asset.id)
    ).all()
    for reference in current:
        db.delete(reference)
    db.flush()
    if not ids:
        return

    images = db.scalars(
        select(Asset).where(Asset.id.in_(ids), Asset.kind == "image", Asset.id != asset.id)
    ).all()
    for image in images:
        db.add(AssetReference(from_asset_id=asset.id, to_asset_id=image.id))
    db.flush()


def resolve_cover(db: DbSession, cover_id: str | None) -> str | None:
    """封面只允许指向图片内容（企划第 8.4 节 T4）。"""
    if not cover_id:
        return None
    target = db.get(Asset, cover_id)
    if target is None:
        raise ApiError.invalid("封面指向的内容不存在")
    if target.kind != "image":
        raise ApiError.invalid("封面只能指向图片")
    return target.id


def get_asset(db: DbSession, asset_id: str) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise ApiError.not_found("内容不存在")
    return asset


def get_version(db: DbSession, version_id: str) -> AssetVersion:
    version = db.get(AssetVersion, version_id)
    if version is None:
        raise ApiError.not_found("版本不存在")
    return version


def current_version(asset: Asset) -> AssetVersion | None:
    for version in asset.versions:
        if version.id == asset.current_version_id:
            return version
    return None


def next_version_no(db: DbSession, asset: Asset) -> int:
    highest = db.scalar(
        select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset.id)
    )
    return int(highest or 0) + 1


def prune_versions(db: DbSession, *, storage: LocalStorage, asset: Asset) -> int:
    """按保留规则删旧版本。当前版本与已发布版本指向的文件永远保留。

    顺序是先删文件再删行，两者都在同一个请求事务里。若提交阶段失败，
    可能出现「行还在、文件已删」的状态；这类不一致由 ``fsck`` 报告（第 5、9 部分），
    不在这里用两阶段提交换复杂度。
    """
    keep_limit = MAX_VERSIONS_VIDEO if asset.kind == KIND_VIDEO else MAX_VERSIONS
    protected = {asset.current_version_id, asset.published_version_id} - {None}

    versions = sorted(asset.versions, key=lambda item: item.version_no, reverse=True)
    keep_ids = {version.id for version in versions[:keep_limit]} | protected
    removed = 0
    for version in versions:
        if version.id in keep_ids:
            continue
        storage.discard_original(version.storage_key)
        db.delete(version)
        removed += 1
    if removed:
        db.flush()
        audit.record(
            db,
            action=audit.ASSET_VERSION_PRUNED,
            actor_type=audit.ACTOR_SYSTEM,
            target_type=audit.TARGET_ASSET,
            target_id=asset.id,
            detail={"removed": removed, "keep": keep_limit},
        )
    return removed


def list_assets(
    db: DbSession,
    *,
    kind: str | None = None,
    folder_id: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    query: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[Asset], str | None]:
    """按 ``created_at, id`` 排序的游标分页（企划第 13.2 节）。"""
    limit = max(1, min(limit, MAX_LIMIT))
    statement = select(Asset)

    if kind:
        statement = statement.where(Asset.kind == kind)
    if folder_id:
        statement = statement.where(Asset.folder_id == folder_id)
    if status:
        statement = statement.where(Asset.status == status)
    else:
        statement = statement.where(Asset.status != "trashed")
    if query:
        needle = f"%{query.strip()}%"
        statement = statement.where(or_(Asset.title.ilike(needle), Asset.slug.ilike(needle)))
    if tag:
        statement = statement.join(Asset.tags).where(Tag.slug == tag)

    if cursor:
        created_at, _, identifier = cursor.partition("~")
        try:
            after = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise ApiError.invalid("游标格式不正确") from exc
        statement = statement.where(
            or_(
                Asset.created_at > after,
                (Asset.created_at == after) & (Asset.id > identifier),
            )
        )

    statement = statement.order_by(Asset.created_at, Asset.id).limit(limit + 1)
    rows = list(db.scalars(statement).unique().all())
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = f"{page[-1].created_at.isoformat()}~{page[-1].id}" if has_more and page else None
    return page, next_cursor


# —— 内部 ——


def title_from_filename(filename: str) -> str:
    """标题默认取文件名主干。文件名本身只在下载时用（第 5 部分）。"""
    stem = Path(filename).stem.strip()
    return stem[:255] or "未命名"


def _add_version(
    db: DbSession,
    *,
    storage: LocalStorage,
    asset: Asset,
    version_no: int,
    source: Path,
    inspection: Inspection,
    filename: str,
) -> AssetVersion:
    storage_key = keys.build(asset.id, version_no, inspection.extension)
    storage.store_original(source, storage_key)
    version = AssetVersion(
        asset_id=asset.id,
        version_no=version_no,
        original_filename=filename[:255],
        mime=inspection.mime,
        byte_size=inspection.byte_size,
        sha256=inspection.sha256,
        storage_key=storage_key,
        width=inspection.width,
        height=inspection.height,
        extract_status="pending",
        created_at=utcnow(),
    )
    # 追加到关系而不是只 db.add：集合里已经加载过旧版本时，
    # 保留规则（prune_versions）必须看得见这一版，否则永远删不掉旧文件。
    asset.versions.append(version)
    db.flush()
    return version


def _ensure_replaceable(asset: Asset, inspection: Inspection) -> None:
    """替换时类型必须一致：图片不能变成视频，否则公开地址的扩展名会漂移。"""
    if asset.status == "trashed":
        raise ApiError.conflict("回收站中的内容不能替换文件")
    if asset.kind != inspection.kind:
        raise ApiError.invalid(f"新文件类型为 {inspection.kind}，与内容的 {asset.kind} 不一致")


def _folder_id(db: DbSession, folder_id: str | None) -> str | None:
    if not folder_id:
        return None
    if db.get(Folder, folder_id) is None:
        raise ApiError.invalid("目录不存在")
    return folder_id


def _clean_slug(value: str | None) -> str | None:
    clean = (value or "").strip()
    return clean[:128] or None


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _first_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


def _extension(kind: str) -> str:
    return "md" if kind == KIND_MARKDOWN else "txt"


def _sha256(data: bytes) -> str:
    return sha256(data).hexdigest()
