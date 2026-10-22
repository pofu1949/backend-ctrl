"""公开文案：只读发布快照，权限独立于资产和集合授权。"""

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..auth import deps as auth_deps
from ..collections.service import prefix_matches
from ..entries.service import decode_cursor, encode_cursor
from ..entries.validation import validate
from ..errors import ApiError
from ..models import Entry, Site
from . import deps

router = APIRouter(tags=["public entries"])


def can_read_entry(entry: Entry, *, site: Site | None) -> bool:
    """文案唯一的公开读取判断：范围与可见性全部取发布快照。"""
    if entry.status != "published":
        return False
    if entry.published_site_id is not None:
        return site is not None and site.id == entry.published_site_id
    return entry.published_visibility == "public" or (
        entry.published_visibility == "site" and site is not None
    )


def _visible(db: DbSession, *, site: Site | None, language: str) -> dict[str, Entry]:
    validate({"language": language})
    rows = db.scalars(
        select(Entry).where(Entry.status == "published", Entry.published_language == language)
    ).all()
    result = {}
    for row in rows:
        # 同键同时存在时，本站专属覆盖库级；其他站点的条目不能参与覆盖。
        if can_read_entry(row, site=site) and (
            row.published_key not in result or row.published_site_id is not None
        ):
            result[row.published_key] = row
    return result


def _payload(entry: Entry) -> dict:
    return {
        "key": entry.published_key,
        "language": entry.published_language,
        "value": entry.published_value,
        "updated_at": entry.published_at.isoformat(timespec="microseconds") + "Z",
    }


@router.get("/entries")
def list_entries(
    request: Request,
    language: str = Query(default="zh-CN", max_length=32),
    prefix: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1024),
    db: DbSession = Depends(auth_deps.get_db),
    site=Depends(deps.site_from_request),
) -> Response:
    visible = _visible(db, site=site, language=language)
    after = decode_cursor(cursor)
    rows = [
        row
        for key, row in sorted(visible.items())
        if ((not prefix or prefix_matches(prefix, key)) and (after is None or key > after[0]))
    ]
    page = rows[:limit]
    items = [_payload(row) for row in page]
    # 满页始终给继续游标，下一页允许为空。否则页外新增记录也会改变本页缓存。
    next_cursor = encode_cursor(page[-1].published_key, page[-1].id) if len(page) == limit else None
    return deps.json_response(
        request,
        site=site,
        payload={
            "items": items,
            "next_cursor": next_cursor,
            "updated_at": max((item["updated_at"] for item in items), default=None),
        },
    )


@router.get("/entries/{key}")
def read_entry(
    key: str,
    request: Request,
    language: str = Query(default="zh-CN", max_length=32),
    db: DbSession = Depends(auth_deps.get_db),
    site=Depends(deps.site_from_request),
) -> Response:
    entry = _visible(db, site=site, language=language).get(key)
    if entry is None:
        raise ApiError.not_found("文案不存在")
    return deps.json_response(request, site=site, payload=_payload(entry))
