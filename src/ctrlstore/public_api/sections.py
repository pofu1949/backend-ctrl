"""公开区块：只读发布快照。一张页面一次取整棵字段树。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..auth import deps as auth_deps
from ..collections.service import prefix_matches
from ..errors import ApiError
from ..models import Asset, Section, Site
from ..sections.service import decode_cursor, encode_cursor
from ..sections.template import FieldSpec
from ..sections.validation import asset_ids, get_template, validate_meta
from . import deps, payloads

router = APIRouter(tags=["public sections"])


def can_read_section(section: Section, *, site: Site | None) -> bool:
    """区块唯一的公开读取判断：范围与可见性全部取发布快照。"""
    if section.status != "published" or not section.published_body:
        return False
    if section.published_site_id is not None:
        return site is not None and site.id == section.published_site_id
    return section.published_visibility == "public" or (
        section.published_visibility == "site" and site is not None
    )


def _visible(db: DbSession, *, site: Site | None, language: str) -> dict[str, Section]:
    validate_meta({"language": language})
    rows = db.scalars(
        select(Section).where(
            Section.status == "published", Section.published_language == language
        )
    ).all()
    result: dict[str, Section] = {}
    for row in rows:
        # 同别名同时存在时，本站专属覆盖库级。
        if can_read_section(row, site=site) and (
            row.published_slug not in result or row.published_site_id is not None
        ):
            result[row.published_slug] = row
    return result


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds") + "Z"


def _load_assets(db: DbSession, ids: list[str]) -> dict[str, Asset]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        return {}
    rows = db.scalars(select(Asset).where(Asset.id.in_(unique))).all()
    return {row.id: row for row in rows}


def _render_fields(
    request: Request,
    db: DbSession,
    template_id: str,
    body: dict[str, Any],
    assets: dict[str, Asset],
    site: Site | None,
) -> dict[str, Any]:
    return _render_object(request, db, get_template(template_id).fields, body, assets, site)


def _render_object(
    request: Request,
    db: DbSession,
    fields: tuple[FieldSpec, ...],
    body: dict[str, Any],
    assets: dict[str, Asset],
    site: Site | None,
) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for field in fields:
        value = body.get(field.key)
        if field.type == "assets":
            items = []
            for asset_id in value or []:
                asset = assets.get(asset_id)
                # 发布后又撤回的图从这棵树里消失，其余文字保持可读。
                if asset is not None and deps.can_read_asset(db, asset, site=site):
                    items.append(payloads.asset_summary(request, asset, site=site))
            rendered[field.key] = items
        elif field.type == "repeat":
            rendered[field.key] = [
                _render_object(request, db, field.fields, item, assets, site)
                for item in value or []
                if isinstance(item, dict)
            ]
        else:
            rendered[field.key] = value if isinstance(value, str) else ""
    return rendered


def _payload(
    request: Request,
    db: DbSession,
    section: Section,
    *,
    site: Site | None,
    assets: dict[str, Asset],
) -> dict[str, Any]:
    body = json.loads(section.published_body or "{}")
    return {
        "id": section.id,
        "slug": section.published_slug,
        "language": section.published_language,
        "template": section.published_template,
        "title": section.published_title or "",
        "updated_at": _timestamp(section.published_at),
        "fields": _render_fields(request, db, section.published_template, body, assets, site),
    }


def _payloads(
    request: Request, db: DbSession, rows: list[Section], *, site: Site | None
) -> list[dict[str, Any]]:
    parsed = [(row, json.loads(row.published_body or "{}")) for row in rows]
    ids: list[str] = []
    for row, body in parsed:
        ids.extend(asset_ids(row.published_template, body))
    assets = _load_assets(db, ids)
    return [_payload(request, db, row, site=site, assets=assets) for row, _body in parsed]


@router.get("/sections")
def list_sections(
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
        if (not prefix or prefix_matches(prefix, key)) and (after is None or key > after[0])
    ]
    page = rows[:limit]
    items = _payloads(request, db, page, site=site)
    next_cursor = (
        encode_cursor(page[-1].published_slug, page[-1].id) if len(page) == limit else None
    )
    return deps.json_response(
        request,
        site=site,
        payload={
            "items": items,
            "next_cursor": next_cursor,
            "updated_at": max((item["updated_at"] for item in items), default=None),
        },
    )


@router.get("/sections/{slug}")
def read_section(
    slug: str,
    request: Request,
    language: str = Query(default="zh-CN", max_length=32),
    db: DbSession = Depends(auth_deps.get_db),
    site=Depends(deps.site_from_request),
) -> Response:
    section = _visible(db, site=site, language=language).get(slug)
    if section is None:
        raise ApiError.not_found("区块不存在")
    payload = _payloads(request, db, [section], site=site)[0]
    return deps.json_response(request, site=site, payload=payload)
