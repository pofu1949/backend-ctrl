"""标签。

名称唯一，``slug`` 由名称派生。中文标签直接保留原字符，只把空白和标点折叠成 ``-``，
这样 URL 与筛选条件都不需要额外的编码表。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from .. import audit
from ..errors import ApiError
from ..models import Asset, AssetTag, Tag, User, utcnow
from ..search import service as search

#: 标签名长度上限，与列宽一致。
MAX_NAME_LENGTH = 64

#: 折叠成 ``-`` 的字符：空白与常见标点。
_SEPARATORS = re.compile(r"[\s\-_./\\]+")


def list_tags(db: DbSession, *, query: str | None = None) -> list[Tag]:
    """按名称排序；``query`` 做子串匹配，覆盖中文。"""
    statement = select(Tag).order_by(Tag.name)
    if query:
        statement = statement.where(Tag.name.contains(query.strip()))
    return list(db.scalars(statement).all())


def get_tag(db: DbSession, tag_id: str) -> Tag:
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise ApiError.not_found("标签不存在")
    return tag


def ensure_tag(db: DbSession, name: str, *, actor: User | None = None) -> Tag:
    """按名称取标签，不存在就建。内容打标签走这里，避免先建后关联的两步。"""
    clean = _validate_name(name)
    existing = db.scalar(select(Tag).where(Tag.name == clean))
    if existing is not None:
        return existing

    tag = Tag(name=clean, slug=_unique_slug(db, clean), created_at=utcnow())
    db.add(tag)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ApiError.conflict(f"标签已存在：{clean}") from exc
    audit.record(
        db,
        action=audit.TAG_CREATED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_TAG,
        target_id=tag.id,
        detail={"name": clean},
    )
    return tag


def ensure_tags(db: DbSession, names: Iterable[str], *, actor: User | None = None) -> list[Tag]:
    """批量取或建。重复名称只保留一个，顺序保持稳定。"""
    seen: dict[str, Tag] = {}
    for name in names:
        clean = _validate_name(name)
        if clean in seen:
            continue
        seen[clean] = ensure_tag(db, clean, actor=actor)
    return list(seen.values())


def merge_tags(db: DbSession, *, source: Tag, target: Tag, actor: User) -> Tag:
    """把所有用 ``source`` 的内容改挂到 ``target``，然后删除 ``source``。

    名称本身已经唯一，重名来自「两个不同写法指同一件事」。合并后重建受影响
    内容的检索索引：关键词列里存的是标签名，不重建会留着旧名字。
    """
    if source.id == target.id:
        raise ApiError.invalid("不能把标签合并到它自己")

    rows = list(db.scalars(select(AssetTag).where(AssetTag.tag_id == source.id)).all())
    asset_ids = {row.asset_id for row in rows}
    for row in rows:
        duplicate = db.scalar(
            select(AssetTag.id).where(
                AssetTag.asset_id == row.asset_id, AssetTag.tag_id == target.id
            )
        )
        if duplicate is not None:
            db.delete(row)
        else:
            row.tag_id = target.id
    source_name = source.name
    db.delete(source)
    db.flush()

    for asset_id in asset_ids:
        asset = db.get(Asset, asset_id)
        if asset is not None:
            search.refresh(db, asset)

    audit.record_for_user(
        db,
        actor,
        action=audit.TAG_MERGED,
        target_type=audit.TARGET_TAG,
        target_id=target.id,
        detail={"merged_tag": source.id, "merged_name": source_name, "assets": len(asset_ids)},
    )
    return target


def slugify(name: str) -> str:
    """名称转 slug：折叠分隔符、转小写。全空时退化成下划线，交给唯一化处理。"""
    slug = _SEPARATORS.sub("-", name.strip()).strip("-").lower()
    return slug or "tag"


def _unique_slug(db: DbSession, name: str) -> str:
    """同名标签已由名称唯一约束挡住，这里处理「不同名字折叠成同一个 slug」。"""
    base = slugify(name)[:MAX_NAME_LENGTH]
    candidate = base
    suffix = 2
    while db.scalar(select(Tag.id).where(Tag.slug == candidate)) is not None:
        tail = f"-{suffix}"
        candidate = f"{base[: MAX_NAME_LENGTH - len(tail)]}{tail}"
        suffix += 1
    return candidate


def _validate_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        raise ApiError.invalid("标签名不能为空")
    if len(clean) > MAX_NAME_LENGTH:
        raise ApiError.invalid(f"标签名不能超过 {MAX_NAME_LENGTH} 个字符")
    return clean
