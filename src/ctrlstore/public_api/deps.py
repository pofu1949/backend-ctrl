"""公开接口的身份与可见性。

**授权只写在这一个文件里**（企划第 10.5 节）：按 ID、别名、集合、搜索读到的每一条内容
都要经过 :func:`can_read_asset`；集合列表还要经过 :func:`visible_in_collection`。
写散在多处，早晚会漏掉一格。

可见性矩阵（企划第 12.7 节）：

| 状态 + 可见性 | 匿名 | 站点令牌（已授权集合内） |
| --- | --- | --- |
| 已发布 + public | 可读 | 可读 |
| 已发布 + unlisted | 知道地址可读，集合不列出 | 可读，且集合里列出（需 ``can_read_unlisted``） |
| 已发布 + site | 不可读 | 可读 |
| 私有 / 草稿 / 归档 / 回收站 | 不可读 | 不可读 |
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..auth import deps as auth_deps
from ..auth import tokens
from ..collections import service as collections
from ..errors import ApiError
from ..models import (
    ASSET_PUBLISHED,
    VISIBILITY_PUBLIC,
    VISIBILITY_SITE,
    VISIBILITY_UNLISTED,
    Asset,
    Collection,
    CollectionItem,
    Site,
)

#: 匿名公开 JSON 的缓存时间（企划第 12.6 节）。
PUBLIC_MAX_AGE = 60


def site_from_request(request: Request, db: DbSession = Depends(auth_deps.get_db)) -> Site | None:
    """解析站点令牌。没有令牌返回 ``None``；带了但无效直接 401。

    不把无效令牌当匿名：那会让「用错令牌」看起来像「内容不存在」，排错时很难。
    """
    header = request.headers.get(auth_deps.AUTHORIZATION_HEADER, "")
    scheme, _, raw = header.partition(" ")
    if scheme.lower() != "bearer" or not raw.strip():
        return None
    resolved = tokens.resolve(db, raw.strip())
    if resolved is None:
        raise ApiError.unauthorized("站点令牌无效或已过期")
    _, subject = resolved
    if not isinstance(subject, Site):
        raise ApiError.forbidden("这里只接受站点令牌")
    return subject


def can_read_asset(db: DbSession, asset: Asset, *, site: Site | None) -> bool:
    """单条内容能不能被这个调用方读到。公开读取的唯一判据。"""
    if asset.status != ASSET_PUBLISHED:
        return False
    if asset.visibility in (VISIBILITY_PUBLIC, VISIBILITY_UNLISTED):
        # 未列出：知道完整地址就能读，但不会出现在集合与搜索里。
        return True
    if asset.visibility == VISIBILITY_SITE:
        return site is not None and _in_granted_collection(db, asset, site)
    # private 只有管理接口能读。
    return False


def visible_in_collection(
    db: DbSession, collection: Collection, asset: Asset, *, site: Site | None
) -> bool:
    """集合列表里要不要列出这条。

    与 :func:`can_read_asset` 的区别只有「未列出」：单条地址仍可读，
    但集合默认不列出，除非该站点的授权显式允许。
    """
    if not can_read_asset(db, asset, site=site):
        return False
    if asset.visibility == VISIBILITY_UNLISTED:
        return collections.can_read_unlisted(db, collection, site=site)
    return True


def collection_readable(db: DbSession, collection: Collection, *, site: Site | None) -> bool:
    """集合本身能不能被读。库级集合（无站点归属）对所有人开放。"""
    return collections.can_read_collection(db, collection, site=site)


def _in_granted_collection(db: DbSession, asset: Asset, site: Site) -> bool:
    """「仅站点」内容必须出现在该站点已授权的集合里。"""
    slugs = db.scalars(
        select(Collection.slug)
        .join(CollectionItem, CollectionItem.collection_id == Collection.id)
        .where(CollectionItem.asset_id == asset.id)
    ).all()
    if not slugs:
        return False
    grants = collections.granted_prefixes(db, site)
    return any(collections.prefix_matches(grant.prefix, slug) for grant in grants for slug in slugs)


def json_response(request: Request, *, site: Site | None, payload: dict[str, Any]) -> JSONResponse:
    """按调用方身份决定缓存策略。

    匿名可以进共享缓存（60 秒）；带站点令牌的响应与身份有关，不能进任何共享缓存。
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    etag = _etag(body)
    headers = {"ETag": etag, "Vary": "Authorization"}
    if site is None:
        headers["Cache-Control"] = f"public, max-age={PUBLIC_MAX_AGE}"
    else:
        headers["Cache-Control"] = "private, no-store"

    if site is None and request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)


def absolute_url(request: Request, path: str) -> str:
    """用当前基址拼绝对地址。备案后只改配置，内容 ID 不变（企划第 12.1 节）。"""
    return f"{request.app.state.settings.public_base_url.rstrip('/')}{path}"


def _etag(body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
    return f'W/"{digest}"'


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def epoch() -> int:
    return int(time.time())
