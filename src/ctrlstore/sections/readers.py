"""区块读者名单。

名单是区块上的独立关系，不写进任何模板的正文。
已发布的那一份才参与「仅站点」素材的读取判断。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiError
from ..models import Section, SectionReader, Site
from .validation import asset_ids

_MAX_READERS = 200


def ids(db: DbSession, section_id: str, *, published: bool) -> list[str]:
    rows = db.scalars(
        select(SectionReader.site_id)
        .where(SectionReader.section_id == section_id, SectionReader.published.is_(published))
        .order_by(SectionReader.site_id)
    ).all()
    return list(rows)


def replace(db: DbSession, section_id: str, site_ids: list[str], *, published: bool) -> None:
    """用给定站点整份替换某一侧名单。空列表表示清空。"""
    unique = _clean(db, site_ids)
    existing = list(
        db.scalars(
            select(SectionReader).where(
                SectionReader.section_id == section_id,
                SectionReader.published.is_(published),
            )
        ).all()
    )
    keep = set(unique)
    for row in existing:
        if row.site_id not in keep:
            db.delete(row)
    have = {row.site_id for row in existing}
    for site_id in unique:
        if site_id not in have:
            db.add(SectionReader(section_id=section_id, site_id=site_id, published=published))
    db.flush()


def copy_draft_to_published(db: DbSession, section_id: str) -> None:
    replace(db, section_id, ids(db, section_id, published=False), published=True)


def clear_published(db: DbSession, section_id: str) -> None:
    replace(db, section_id, [], published=True)


def authorizes_asset(db: DbSession, asset_id: str, site_id: str) -> bool:
    """已发布、且读者名单包含该站点的区块，有没有引用这条素材。"""
    rows = db.scalars(
        select(Section)
        .join(SectionReader, SectionReader.section_id == Section.id)
        .where(
            Section.status == "published",
            SectionReader.published.is_(True),
            SectionReader.site_id == site_id,
        )
    ).all()
    for section in rows:
        if not section.published_body or not section.published_template:
            continue
        try:
            body = json.loads(section.published_body)
            referenced = asset_ids(section.published_template, body)
        except (json.JSONDecodeError, ApiError, TypeError, ValueError):
            continue
        if asset_id in referenced:
            return True
    return False


def _clean(db: DbSession, site_ids: list[str]) -> list[str]:
    if not isinstance(site_ids, list) or any(
        not isinstance(item, str) or not item for item in site_ids
    ):
        raise ApiError.invalid("读者名单必须是站点 ID 列表")
    unique = list(dict.fromkeys(site_ids))
    if len(unique) > _MAX_READERS:
        raise ApiError.too_large(f"读者名单最多 {_MAX_READERS} 个站点")
    missing = [site_id for site_id in unique if db.get(Site, site_id) is None]
    if missing:
        raise ApiError.invalid("读者名单包含不存在的站点")
    return unique
