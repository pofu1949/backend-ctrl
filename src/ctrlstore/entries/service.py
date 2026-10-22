"""文案的草稿、发布与审计。调用方提交事务；不写文件或资产表。"""

import base64
import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from .. import audit
from ..collections.service import prefix_matches
from ..config import Settings
from ..errors import ApiError
from ..models import Entry, Site, User, utcnow
from .validation import validate

EDIT_FIELDS = ("key", "language", "value", "visibility", "site_id")


def begin_write(db: DbSession) -> None:
    """在读修订号、条数和幂等键之前串行化写入，覆盖 API 多进程与 CLI。"""
    connection = db.connection()
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def get(db: DbSession, entry_id: str) -> Entry:
    entry = db.get(Entry, entry_id, populate_existing=True)
    if entry is None:
        raise ApiError.not_found("文案不存在")
    return entry


def list_entries(
    db: DbSession,
    *,
    language: str | None = None,
    prefix: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[Entry], str | None]:
    statement = select(Entry)
    if language:
        validate({"language": language})
        statement = statement.where(Entry.language == language)
    if status:
        if status not in {"draft", "published"}:
            raise ApiError.invalid("文案状态只能是 draft 或 published")
        statement = statement.where(Entry.status == status)
    after = decode_cursor(cursor)
    rows = db.scalars(statement.order_by(Entry.key, Entry.id)).all()
    rows = [
        row
        for row in rows
        if (
            (not prefix or prefix_matches(prefix, row.key))
            and (after is None or (row.key, row.id) > after)
        )
    ]
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].key, page[-1].id) if len(rows) > limit else None
    return page, next_cursor


def create(db: DbSession, *, settings: Settings, fields: dict, actor: User) -> Entry:
    fields = validate(fields)
    begin_write(db)
    if db.scalar(select(func.count()).select_from(Entry)) >= settings.entry_limit:
        raise ApiError.conflict(f"已达文案条数上限（{settings.entry_limit} 条）")
    _check_site(db, fields.get("site_id"))
    entry = Entry(**fields, revision=1, status="draft")
    db.add(entry)
    db.flush()
    _audit(db, entry, actor, audit.ENTRY_CREATED)
    return entry


def update(db: DbSession, entry_id: str, *, revision: int, fields: dict, actor: User) -> Entry:
    fields = validate(fields)
    begin_write(db)
    entry = get(db, entry_id)
    _check_revision(entry, revision)
    if "site_id" in fields:
        _check_site(db, fields["site_id"])
    for name, value in fields.items():
        setattr(entry, name, value)
    entry.revision += 1
    entry.updated_at = utcnow()
    db.flush()
    _audit(db, entry, actor, audit.ENTRY_UPDATED)
    return entry


def publish(db: DbSession, entry_id: str, *, revision: int, actor: User) -> Entry:
    begin_write(db)
    entry = get(db, entry_id)
    _check_revision(entry, revision)
    snapshot = {f"published_{name}": getattr(entry, name) for name in EDIT_FIELDS}
    if entry.status == "published" and all(
        getattr(entry, name) == value for name, value in snapshot.items()
    ):
        return entry
    duplicate = db.scalar(
        select(Entry.id).where(
            Entry.id != entry.id,
            Entry.status == "published",
            Entry.published_site_id == entry.site_id,
            Entry.published_language == entry.language,
            Entry.published_key == entry.key,
        )
    )
    if duplicate:
        raise ApiError.conflict("该站点、语言下已有相同的已发布文案键")
    for name, value in snapshot.items():
        setattr(entry, name, value)
    entry.status = "published"
    entry.revision += 1
    entry.updated_at = entry.published_at = utcnow()
    try:
        db.flush()
    except IntegrityError as exc:
        raise ApiError.conflict("该站点、语言下已有相同的已发布文案键") from exc
    _audit(db, entry, actor, audit.ENTRY_PUBLISHED)
    return entry


def unpublish(db: DbSession, entry_id: str, *, revision: int, actor: User) -> Entry:
    begin_write(db)
    entry = get(db, entry_id)
    _check_revision(entry, revision)
    if entry.status == "draft":
        return entry
    for name in EDIT_FIELDS:
        setattr(entry, f"published_{name}", None)
    entry.published_at = None
    entry.status = "draft"
    entry.revision += 1
    entry.updated_at = utcnow()
    db.flush()
    _audit(db, entry, actor, audit.ENTRY_UNPUBLISHED)
    return entry


def delete(db: DbSession, entry_id: str, *, actor: User, revision: int | None = None) -> None:
    begin_write(db)
    entry = get(db, entry_id)
    if revision is not None:
        _check_revision(entry, revision)
    if entry.status != "draft":
        raise ApiError.conflict("已发布文案请先撤回，再删除")
    _audit(db, entry, actor, audit.ENTRY_DELETED)
    db.delete(entry)
    db.flush()


def find_by_key(db: DbSession, key: str, *, language: str, site_id: str | None) -> Entry | None:
    """CLI 同时认当前键与已发布键。草稿允许重名，命中多条时要求用管理端 ID 处理。"""
    validate({"key": key, "language": language})
    rows = db.scalars(
        select(Entry).where(
            ((Entry.key == key) & (Entry.language == language) & (Entry.site_id == site_id))
            | (
                (Entry.published_key == key)
                & (Entry.published_language == language)
                & (Entry.published_site_id == site_id)
                & (Entry.status == "published")
            )
        )
    ).all()
    if len(rows) > 1:
        raise ApiError.conflict("键匹配多条文案，请在管理端按 ID 选择条目")
    return rows[0] if rows else None


def encode_cursor(key: str, entry_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([key, entry_id]).encode()).decode()


def decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        pair = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(x, str) for x in pair)
        ):
            raise ValueError
        return tuple(pair)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ApiError.invalid("无效的文案分页游标") from exc


def _check_site(db: DbSession, site_id: str | None) -> None:
    if site_id is not None and db.get(Site, site_id) is None:
        raise ApiError.invalid("站点不存在")


def _check_revision(entry: Entry, revision: int) -> None:
    if entry.revision != revision:
        raise ApiError.conflict(f"内容已被其他人修改（当前修订号 {entry.revision}），请重新读取")


def _audit(db: DbSession, entry: Entry, actor: User, action: str) -> None:
    audit.record_for_user(
        db,
        actor,
        action=action,
        target_type=audit.TARGET_ENTRY,
        target_id=entry.id,
        detail={"key": entry.key, "language": entry.language, "characters": len(entry.value)},
    )
