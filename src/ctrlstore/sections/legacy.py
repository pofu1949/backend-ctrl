"""把旧文案和集合授权补进区块。

只插入还没有的行。不修改、不删除 ``entries``、``collections``、``site_grants``。
同一别名已经有区块时跳过，避免盖掉页面区块。可以重复执行。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..collections.service import prefix_matches
from ..models import Entry, Section, SectionReader, SiteGrant, new_id, utcnow
from .template import COPY


def import_legacy(db: DbSession) -> dict[str, int]:
    """返回这次新补上的行数，以及因别名冲突而跳过的文案数。"""
    entries_added = _import_entries(db)
    readers_added = _import_grants(db)
    return {
        "entries_added": entries_added[0],
        "entries_skipped": entries_added[1],
        "readers_added": readers_added,
    }


def _import_entries(db: DbSession) -> tuple[int, int]:
    added = 0
    skipped = 0
    entries = db.scalars(select(Entry).order_by(Entry.created_at, Entry.id)).all()
    for entry in entries:
        if _section_exists(db, slug=entry.key, language=entry.language, site_id=entry.site_id):
            skipped += 1
            continue
        section = Section(
            id=new_id(),
            slug=entry.key,
            language=entry.language,
            site_id=entry.site_id,
            title=(entry.key or "")[:255],
            template=COPY.id,
            visibility=entry.visibility if entry.visibility in {"public", "site"} else "public",
            status=entry.status if entry.status in {"draft", "published"} else "draft",
            body=_text(entry.value),
            revision=entry.revision or 1,
            created_at=entry.created_at or utcnow(),
            updated_at=entry.updated_at or utcnow(),
        )
        if section.status == "published":
            section.published_slug = entry.published_key or entry.key
            section.published_language = entry.published_language or entry.language
            section.published_site_id = entry.published_site_id
            section.published_title = (section.published_slug or entry.key)[:255]
            section.published_template = COPY.id
            section.published_visibility = entry.published_visibility or section.visibility
            section.published_body = _text(
                entry.published_value if entry.published_value is not None else entry.value
            )
            section.published_at = entry.published_at or section.updated_at
        try:
            with db.begin_nested():
                db.add(section)
                db.flush()
        except IntegrityError:
            skipped += 1
            continue
        added += 1
    return added, skipped


def _import_grants(db: DbSession) -> int:
    """前缀能对上区块别名时，把站点补进读者名单。对不上的授权留在原表。"""
    grants = db.scalars(select(SiteGrant)).all()
    if not grants:
        return 0
    sections = db.scalars(select(Section)).all()
    added = 0
    for grant in grants:
        for section in sections:
            if prefix_matches(grant.prefix, section.slug):
                added += _add_reader(db, section.id, grant.site_id, published=False)
            published_slug = section.published_slug
            if (
                section.status == "published"
                and published_slug
                and prefix_matches(grant.prefix, published_slug)
            ):
                added += _add_reader(db, section.id, grant.site_id, published=True)
    return added


def _section_exists(db: DbSession, *, slug: str, language: str, site_id: str | None) -> bool:
    statement = select(Section.id).where(Section.slug == slug, Section.language == language)
    if site_id is None:
        statement = statement.where(Section.site_id.is_(None))
    else:
        statement = statement.where(Section.site_id == site_id)
    return db.scalar(statement) is not None


def _add_reader(db: DbSession, section_id: str, site_id: str, *, published: bool) -> int:
    exists = db.scalar(
        select(SectionReader.id).where(
            SectionReader.section_id == section_id,
            SectionReader.site_id == site_id,
            SectionReader.published.is_(published),
        )
    )
    if exists:
        return 0
    try:
        with db.begin_nested():
            db.add(
                SectionReader(
                    id=new_id(),
                    section_id=section_id,
                    site_id=site_id,
                    published=published,
                )
            )
            db.flush()
    except IntegrityError:
        return 0
    return 1


def _text(value: str | None) -> str:
    return json.dumps(value or "", ensure_ascii=False, separators=(",", ":"))
