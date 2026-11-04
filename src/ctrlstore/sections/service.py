"""区块的草稿、发布与审计。调用方提交事务。"""

from __future__ import annotations

import base64
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from .. import audit
from ..config import Settings
from ..errors import ApiError
from ..models import ASSET_PUBLISHED, Asset, Section, Site, User, utcnow
from ..uploads.inspection import KIND_IMAGE, KIND_VIDEO
from . import access, readers
from .template import TEMPLATES
from .validation import (
    MAX_BODY_BYTES,
    asset_ids,
    empty_body,
    get_template,
    normalize,
    prefix_matches,
    validate_meta,
)

_META = ("slug", "language", "site_id", "title", "template", "visibility")
_MEDIA_KINDS = {KIND_IMAGE, KIND_VIDEO}


def begin_write(db: DbSession) -> None:
    """写修订号和别名唯一性之前先拿 SQLite 写锁，覆盖两个 API 进程同时保存。"""
    connection = db.connection()
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def get(db: DbSession, section_id: str) -> Section:
    section = db.get(Section, section_id, populate_existing=True)
    if section is None:
        raise ApiError.not_found("区块不存在")
    return section


def list_sections(
    db: DbSession,
    *,
    language: str | None = None,
    prefix: str | None = None,
    status: str | None = None,
    template_ids: list[str] | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[Section], str | None]:
    statement = select(Section)
    if template_ids is not None:
        statement = statement.where(Section.template.in_(template_ids))
    if language:
        validate_meta({"language": language})
        statement = statement.where(Section.language == language)
    if status:
        if status not in {"draft", "published"}:
            raise ApiError.invalid("区块状态只能是 draft 或 published")
        statement = statement.where(Section.status == status)
    after = decode_cursor(cursor)
    rows = db.scalars(statement.order_by(Section.slug, Section.id)).all()
    rows = [
        row
        for row in rows
        if (not prefix or prefix_matches(prefix, row.slug))
        and (after is None or (row.slug, row.id) > after)
    ]
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].slug, page[-1].id) if len(rows) > limit else None
    return page, next_cursor


def create(db: DbSession, *, settings: Settings, fields: dict[str, Any], actor: User) -> Section:
    meta = validate_meta({key: fields[key] for key in _META if key in fields})
    template_id = meta.get("template") or "stack"
    get_template_id(template_id)
    raw_body = fields["body"] if fields.get("body") is not None else empty_body(template_id)
    body = normalize(template_id, raw_body, strict=False)
    begin_write(db)
    _enforce_quota(db, settings, template_id)
    _check_site(db, meta.get("site_id"))
    _ensure_slug_free(
        db,
        section_id="",
        slug=meta["slug"],
        language=meta.get("language", "zh-CN"),
        site_id=meta.get("site_id"),
    )
    section = Section(
        slug=meta["slug"],
        language=meta.get("language", "zh-CN"),
        site_id=meta.get("site_id"),
        title=meta.get("title", ""),
        template=template_id,
        visibility=meta.get("visibility", "public"),
        status="draft",
        body=_dump(body),
        revision=1,
    )
    db.add(section)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ApiError.conflict("这个别名在该语言和站点下已经存在") from exc
    if fields.get("reader_ids") is not None:
        readers.replace(db, section.id, fields["reader_ids"], published=False)
    _audit(db, section, actor, audit.SECTION_CREATED)
    return section


def update(
    db: DbSession,
    section_id: str,
    *,
    revision: int,
    fields: dict[str, Any],
    actor: User,
) -> Section:
    meta = validate_meta({key: value for key, value in fields.items() if key in _META})
    begin_write(db)
    section = get(db, section_id)
    _check_revision(section, revision)
    if "template" in meta and meta["template"] != section.template:
        raise ApiError.invalid("区块模板创建后不能更换")
    if "site_id" in meta:
        _check_site(db, meta["site_id"])
    for name, value in meta.items():
        if name != "template":
            setattr(section, name, value)
    if "body" in fields:
        if fields["body"] is None:
            raise ApiError.invalid("区块正文不能为空值")
        section.body = _dump(normalize(section.template, fields["body"], strict=False))
    if "reader_ids" in fields:
        if fields["reader_ids"] is None:
            raise ApiError.invalid("读者名单不能为空值")
        readers.replace(db, section.id, fields["reader_ids"], published=False)
    _ensure_slug_free(
        db,
        section_id=section.id,
        slug=section.slug,
        language=section.language,
        site_id=section.site_id,
    )
    section.revision += 1
    section.updated_at = utcnow()
    try:
        db.flush()
    except IntegrityError as exc:
        raise ApiError.conflict("这个别名在该语言和站点下已经存在") from exc
    _audit(db, section, actor, audit.SECTION_UPDATED)
    return section


def publish(
    db: DbSession,
    section_id: str,
    *,
    revision: int,
    actor: User,
    reader_ids: list[str] | None = None,
    replace_readers: bool = False,
) -> Section:
    begin_write(db)
    section = get(db, section_id)
    _check_revision(section, revision)
    if replace_readers:
        readers.replace(db, section.id, reader_ids or [], published=False)
    body = normalize(section.template, _load(section.body), strict=True)
    encoded = _dump(body)
    _require_published_media(db, section.template, body)
    draft_readers = readers.ids(db, section.id, published=False)
    live_readers = readers.ids(db, section.id, published=True)
    if _snapshot_matches(section, encoded) and set(draft_readers) == set(live_readers):
        return section
    _ensure_published_slug_free(db, section)
    section.body = encoded
    section.published_slug = section.slug
    section.published_language = section.language
    section.published_site_id = section.site_id
    section.published_title = section.title
    section.published_template = section.template
    section.published_visibility = section.visibility
    section.published_body = encoded
    section.status = "published"
    section.revision += 1
    section.updated_at = section.published_at = utcnow()
    readers.copy_draft_to_published(db, section.id)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ApiError.conflict("该站点、语言下已有相同的已发布区块") from exc
    _audit(db, section, actor, audit.SECTION_PUBLISHED)
    return section


def unpublish(db: DbSession, section_id: str, *, revision: int, actor: User) -> Section:
    begin_write(db)
    section = get(db, section_id)
    _check_revision(section, revision)
    if section.status == "draft":
        return section
    section.published_slug = None
    section.published_language = None
    section.published_site_id = None
    section.published_title = None
    section.published_template = None
    section.published_visibility = None
    section.published_body = None
    section.published_at = None
    section.status = "draft"
    section.revision += 1
    section.updated_at = utcnow()
    readers.clear_published(db, section.id)
    db.flush()
    _audit(db, section, actor, audit.SECTION_UNPUBLISHED)
    return section


def delete(db: DbSession, section_id: str, *, actor: User, revision: int | None = None) -> None:
    begin_write(db)
    section = get(db, section_id)
    if revision is not None:
        _check_revision(section, revision)
    if section.status != "draft":
        raise ApiError.conflict("已发布区块请先撤回，再删除")
    _audit(db, section, actor, audit.SECTION_DELETED)
    db.delete(section)
    db.flush()


def asset_briefs(db: DbSession, section: Section) -> dict[str, dict[str, str]]:
    """给管理端预览用的内容摘要。缺了的 ID 不补假记录，界面会显示缺失。"""
    ids = asset_ids(section.template, _load(section.body))
    if section.published_body and section.published_template:
        ids.extend(asset_ids(section.published_template, _load(section.published_body)))
    return _briefs(db, ids)


def templates() -> list[dict[str, Any]]:
    return [item.public_dict() for item in TEMPLATES.values()]


def encode_cursor(slug: str, section_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([slug, section_id]).encode()).decode()


def decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        pair = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
        ):
            raise ValueError
        return tuple(pair)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ApiError.invalid("无效的区块分页游标") from exc


def _require_published_media(db: DbSession, template_id: str, body: dict[str, Any]) -> None:
    """发布时配图必须已经是公开意义上的已发布内容。撤回后的图留到读取时再去掉。"""
    unique = list(dict.fromkeys(asset_ids(template_id, body)))
    if not unique:
        return
    rows = {
        row.id: row for row in db.scalars(select(Asset).where(Asset.id.in_(unique))).all()
    }
    missing = [asset_id for asset_id in unique if asset_id not in rows]
    if missing:
        raise ApiError.invalid("引用了不存在的图片或视频，请先从区块里移除")
    wrong = [rows[asset_id] for asset_id in unique if rows[asset_id].kind not in _MEDIA_KINDS]
    if wrong:
        raise ApiError.invalid("区块只能引用图片或视频")
    pending = [
        rows[asset_id].title or asset_id
        for asset_id in unique
        if rows[asset_id].status != ASSET_PUBLISHED
    ]
    if pending:
        raise ApiError.conflict("配图尚未发布：" + "、".join(pending[:6]))


def _briefs(db: DbSession, ids: list[str]) -> dict[str, dict[str, str]]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        return {}
    rows = db.scalars(select(Asset).where(Asset.id.in_(unique))).all()
    return {
        row.id: {
            "id": row.id,
            "title": row.title,
            "kind": row.kind,
            "status": row.status,
            "visibility": row.visibility,
        }
        for row in rows
    }


def _snapshot_matches(section: Section, encoded: str) -> bool:
    return (
        section.status == "published"
        and section.published_slug == section.slug
        and section.published_language == section.language
        and section.published_site_id == section.site_id
        and (section.published_title or "") == section.title
        and section.published_template == section.template
        and section.published_visibility == section.visibility
        and section.published_body == encoded
    )


def _ensure_slug_free(
    db: DbSession, *, section_id: str, slug: str, language: str, site_id: str | None
) -> None:
    taken = db.scalar(
        select(Section.id).where(
            Section.id != section_id,
            Section.slug == slug,
            Section.language == language,
            Section.site_id == site_id,
        )
    )
    if taken:
        raise ApiError.conflict("这个别名在该语言和站点下已经存在")


def _ensure_published_slug_free(db: DbSession, section: Section) -> None:
    taken = db.scalar(
        select(Section.id).where(
            Section.id != section.id,
            Section.status == "published",
            Section.published_slug == section.slug,
            Section.published_language == section.language,
            Section.published_site_id == section.site_id,
        )
    )
    if taken:
        raise ApiError.conflict("该站点、语言下已有相同的已发布区块")


def _check_site(db: DbSession, site_id: str | None) -> None:
    if site_id is not None and db.get(Site, site_id) is None:
        raise ApiError.invalid("站点不存在")


def _check_revision(section: Section, revision: int) -> None:
    if section.revision != revision:
        raise ApiError.conflict(
            f"内容已被其他人修改（当前修订号 {section.revision}），请重新读取"
        )


def _audit(db: DbSession, section: Section, actor: User, action: str) -> None:
    audit.record_for_user(
        db,
        actor,
        action=action,
        target_type=audit.TARGET_SECTION,
        target_id=section.id,
        detail={"slug": section.slug, "language": section.language, "template": section.template},
    )


def find_by_alias(
    db: DbSession, *, template_id: str, slug: str, language: str, site_id: str | None
) -> Section | None:
    statement = select(Section).where(
        Section.template == template_id,
        Section.slug == slug,
        Section.language == language,
    )
    if site_id is None:
        statement = statement.where(Section.site_id.is_(None))
    else:
        statement = statement.where(Section.site_id == site_id)
    return db.scalar(statement)


def _enforce_quota(db: DbSession, settings: Settings, template_id: str) -> None:
    """文案和页面区块分开计数，避免短句占满页面区块的名额。"""
    kind = get_template(template_id).access
    if kind == "copy":
        limit = settings.entry_limit
        label = "文案"
        selected = access.ids_for("copy")
    else:
        limit = settings.section_limit
        label = "区块"
        selected = access.ids_for("section")
    count = db.scalar(
        select(func.count()).select_from(Section).where(Section.template.in_(selected))
    )
    if count >= limit:
        raise ApiError.conflict(f"已达{label}条数上限（{limit} 条）")


def _dump(body: Any) -> str:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_BODY_BYTES:
        raise ApiError.too_large("区块正文过大")
    return encoded


def _load(raw: str) -> Any:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError.invalid("区块正文已损坏") from exc
    return body


def get_template_id(template_id: str) -> str:
    if template_id not in TEMPLATES:
        raise ApiError.invalid("未知的区块模板")
    return template_id
