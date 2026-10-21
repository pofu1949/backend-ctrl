"""集合与站点授权（企划第 8.2 节 F4–F6、第 10.5 节）。

集合是网站读取内容的入口：按别名取一组有序条目。三个约束值得记住：

* 同一集合里同一内容只出现一次；
* 位置在同一集合内唯一，所以重排要先让旧值腾地方（同一条事务）；
* 站点授权按**点号分段**匹配前缀：``home`` 覆盖 ``home.hero``，不覆盖 ``homepage``。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from .. import audit
from ..errors import ApiError
from ..models import Asset, Collection, CollectionItem, Site, SiteGrant, User, new_id, utcnow
from ..uploads.inspection import (
    KIND_DOCX,
    KIND_IMAGE,
    KIND_MARKDOWN,
    KIND_PDF,
    KIND_TEXT,
    KIND_VIDEO,
    KIND_XLSX,
)

#: 别名允许的字符：小写字母、数字、点、连字符。
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")

#: 别名长度上限，与列宽一致。
MAX_SLUG_LENGTH = 128

#: 允许放进集合的类型。
KINDS = (KIND_IMAGE, KIND_VIDEO, KIND_MARKDOWN, KIND_PDF, KIND_DOCX, KIND_XLSX, KIND_TEXT)

#: 重排时的临时偏移：先把旧位置挪到不可能冲突的区间，再写最终值。
_REORDER_OFFSET = 1_000_000


def list_collections(db: DbSession) -> list[Collection]:
    return list(db.scalars(select(Collection).order_by(Collection.slug)).all())


def get_collection(db: DbSession, collection_id: str) -> Collection:
    collection = db.get(Collection, collection_id)
    if collection is None:
        raise ApiError.not_found("集合不存在")
    return collection


def get_by_slug(db: DbSession, slug: str) -> Collection | None:
    return db.scalar(select(Collection).where(Collection.slug == slug))


def create_collection(
    db: DbSession,
    *,
    slug: str,
    title: str,
    site_id: str | None = None,
    kinds: Iterable[str] = (),
    actor: User | None = None,
) -> Collection:
    clean = _validate_slug(slug)
    if site_id and db.get(Site, site_id) is None:
        raise ApiError.invalid("站点不存在")

    collection = Collection(
        id=new_id(),
        site_id=site_id,
        slug=clean,
        title=title.strip()[:255],
        created_at=utcnow(),
    )
    collection.kinds = _validate_kinds(kinds)
    db.add(collection)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ApiError.conflict(f"集合别名已存在：{clean}") from exc

    audit.record(
        db,
        action=audit.COLLECTION_CREATED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_COLLECTION,
        target_id=collection.id,
        detail={"slug": clean},
    )
    return collection


def update_collection(
    db: DbSession,
    collection: Collection,
    *,
    title: str | None = None,
    kinds: Iterable[str] | None = None,
    actor: User | None = None,
) -> Collection:
    if title is not None:
        collection.title = title.strip()[:255]
    if kinds is not None:
        selected = _validate_kinds(kinds)
        _ensure_existing_items_allowed(collection, selected)
        collection.kinds = selected
    audit.record(
        db,
        action=audit.COLLECTION_UPDATED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_COLLECTION,
        target_id=collection.id,
    )
    return collection


def add_item(
    db: DbSession,
    collection: Collection,
    *,
    asset_id: str,
    caption: str = "",
    position: int | None = None,
    actor: User | None = None,
) -> CollectionItem:
    """追加一条。不给位置就放到末尾。"""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise ApiError.not_found("内容不存在")
    _ensure_kind_allowed(collection, asset)

    existing = db.scalar(
        select(CollectionItem).where(
            CollectionItem.collection_id == collection.id, CollectionItem.asset_id == asset.id
        )
    )
    if existing is not None:
        raise ApiError.conflict("这条内容已经在集合里")

    items = list(collection.items)
    target = len(items) if position is None else max(0, min(position, len(items)))
    item = CollectionItem(
        id=new_id(),
        asset_id=asset.id,
        # 先放到末尾，统一由 reorder 把所有人排成连续位置。
        position=len(items) + _REORDER_OFFSET,
        caption=caption.strip()[:255],
    )
    # 追加到关系而不是只 db.add：reorder 要拿关系里的现有条目做校验。
    collection.items.append(item)
    db.flush()

    order = [row.asset_id for row in items]
    order.insert(target, asset.id)
    reorder(db, collection, order=order)
    audit.record(
        db,
        action=audit.COLLECTION_ITEM_ADDED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_COLLECTION,
        target_id=collection.id,
        detail={"asset_id": asset.id, "position": target},
    )
    return item


def remove_item(
    db: DbSession, collection: Collection, *, asset_id: str, actor: User | None = None
) -> None:
    item = next((row for row in collection.items if row.asset_id == asset_id), None)
    if item is None:
        raise ApiError.not_found("集合里没有这条内容")
    # 从关系里移除，让级联删除同时把集合里的这一条摘掉，后续重排才看得到真实列表。
    collection.items.remove(item)
    db.flush()

    remaining = [row.asset_id for row in sorted(collection.items, key=lambda r: r.position)]
    reorder(db, collection, order=remaining)
    audit.record(
        db,
        action=audit.COLLECTION_ITEM_REMOVED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_COLLECTION,
        target_id=collection.id,
        detail={"asset_id": asset_id},
    )


def reorder(db: DbSession, collection: Collection, *, order: list[str]) -> Collection:
    """按给定顺序重排。整体提交，中途失败则整次无效。

    位置有唯一约束，不能直接逐行改（会撞上尚未让位的旧值）。先把该集合所有位置
    挪到偏移区，再按新顺序写成 0..n-1。
    """
    current = {row.asset_id for row in collection.items}
    if set(order) != current:
        raise ApiError.invalid("排序列表必须与集合现有条目完全一致")

    db.execute(
        update(CollectionItem)
        .where(CollectionItem.collection_id == collection.id)
        .values(position=CollectionItem.position + _REORDER_OFFSET)
    )
    for index, asset_id in enumerate(order):
        db.execute(
            update(CollectionItem)
            .where(
                CollectionItem.collection_id == collection.id,
                CollectionItem.asset_id == asset_id,
            )
            .values(position=index)
        )
    db.flush()
    db.expire(collection, ["items"])
    return collection


def set_caption(
    db: DbSession, collection: Collection, *, asset_id: str, caption: str
) -> CollectionItem:
    item = db.scalar(
        select(CollectionItem).where(
            CollectionItem.collection_id == collection.id, CollectionItem.asset_id == asset_id
        )
    )
    if item is None:
        raise ApiError.not_found("集合里没有这条内容")
    item.caption = caption.strip()[:255]
    return item


# —— 站点授权 ——


def list_grants(db: DbSession, site: Site) -> list[SiteGrant]:
    return list(db.scalars(select(SiteGrant).where(SiteGrant.site_id == site.id)).all())


def add_grant(
    db: DbSession,
    site: Site,
    *,
    prefix: str,
    can_read_unlisted: bool = False,
    actor: User | None = None,
) -> SiteGrant:
    clean = _validate_prefix(prefix)
    grant = SiteGrant(
        id=new_id(),
        site_id=site.id,
        prefix=clean,
        can_read_unlisted=can_read_unlisted,
        created_at=utcnow(),
    )
    db.add(grant)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ApiError.conflict(f"该站点已授权这个前缀：{clean}") from exc
    audit.record(
        db,
        action=audit.SITE_GRANT_ADDED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_SITE,
        target_id=site.id,
        detail={"prefix": clean},
    )
    return grant


def remove_grant(db: DbSession, grant: SiteGrant, *, actor: User | None = None) -> None:
    db.delete(grant)
    audit.record(
        db,
        action=audit.SITE_GRANT_REMOVED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_SITE,
        target_id=grant.site_id,
        detail={"prefix": grant.prefix},
    )


def prefix_matches(prefix: str, slug: str) -> bool:
    """按点号分段匹配：``home`` 覆盖 ``home.hero``，不覆盖 ``homepage``。"""
    return slug == prefix or slug.startswith(f"{prefix}.")


def granted_prefixes(db: DbSession, site: Site) -> list[SiteGrant]:
    return list(db.scalars(select(SiteGrant).where(SiteGrant.site_id == site.id)).all())


def can_read_collection(db: DbSession, collection: Collection, *, site: Site | None) -> bool:
    """集合本身能不能被读。

    归属决定可见性：库级集合（``site_id`` 为空）对所有调用方开放；属于某站点的集合
    只对那个站点开放，匿名看不到。

    站点授权（``site_grants``）不参与这里：它决定的是**内容**可见性——
    已发布且 ``site`` 可见性的内容必须落在该站点已授权的集合里，
    「未列出」内容是否在集合里列出也看授权。两件事分开，语义才不打架。
    """
    if site is None:
        return collection.site_id is None
    return collection.site_id in (None, site.id)


def can_read_unlisted(db: DbSession, collection: Collection, *, site: Site | None) -> bool:
    """该集合是否允许站点看到「未列出」内容。匿名永远不行。"""
    if site is None:
        return False
    return any(
        prefix_matches(grant.prefix, collection.slug) and grant.can_read_unlisted
        for grant in granted_prefixes(db, site)
    )


# —— 内部 ——


def _validate_slug(slug: str) -> str:
    clean = (slug or "").strip().lower()
    if not clean or len(clean) > MAX_SLUG_LENGTH:
        raise ApiError.invalid(f"集合别名需为 1–{MAX_SLUG_LENGTH} 个字符")
    if not SLUG_PATTERN.match(clean):
        raise ApiError.invalid("集合别名只能用小写字母、数字、点和连字符，例如 home.hero")
    return clean


def _validate_prefix(prefix: str) -> str:
    """前缀与别名同规则，但允许更短（例如只写 ``home``）。"""
    clean = (prefix or "").strip().lower()
    if not clean:
        raise ApiError.invalid("授权前缀不能为空")
    if not SLUG_PATTERN.match(clean):
        raise ApiError.invalid("授权前缀只能用小写字母、数字、点和连字符")
    return clean


def _validate_kinds(kinds: Iterable[str]) -> list[str]:
    selected = sorted(set(kinds))
    unknown = [kind for kind in selected if kind not in KINDS]
    if unknown:
        raise ApiError.invalid(f"未知内容类型：{', '.join(unknown)}")
    return selected


def _ensure_kind_allowed(collection: Collection, asset: Asset) -> None:
    allowed = collection.kinds
    if allowed and asset.kind not in allowed:
        raise ApiError.invalid(f"该集合只接受 {', '.join(allowed)}，这条是 {asset.kind}")


def _ensure_existing_items_allowed(collection: Collection, kinds: list[str]) -> None:
    """收紧类型限制时，先确认现有条目都符合，避免集合变成自相矛盾的状态。"""
    if not kinds:
        return
    offending = [row.asset for row in collection.items if row.asset.kind not in kinds]
    if offending:
        names = ", ".join(sorted({asset.kind for asset in offending}))
        raise ApiError.conflict(f"集合里已有 {names} 类型的内容，先移除再收紧限制")


def dumps_kinds(kinds: Iterable[str]) -> str | None:
    selected = sorted(set(kinds))
    return json.dumps(selected) if selected else None
