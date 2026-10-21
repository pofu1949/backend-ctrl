"""站点检索 ``GET /api/public/v1/search``（企划第 8.7 节 S3）。

默认关闭；打开后也只搜该站点能读到的已发布内容：

1. 索引里只有 ``published`` 行——发布快照与已发布版本正文，草稿与待发布替换不在其中；
2. 取到结果后仍逐个过 ``searchable``：公开可读，仅站点必须在已授权集合里，
   「未列出」不出现在发现型结果里。

所以即使索引里有某个词，别的站点或匿名调用方也拿不到不该看的内容。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DbSession

from ..auth import deps as auth_deps
from ..errors import ApiError
from ..search import service as search
from . import deps, payloads

router = APIRouter(tags=["public"])


@router.get("/search")
def search_public(
    request: Request,
    q: str = Query(min_length=1, max_length=search.MAX_QUERY_LENGTH),
    limit: int = Query(default=search.MAX_RESULTS, ge=1, le=search.MAX_RESULTS),
    db: DbSession = Depends(auth_deps.get_db),
    site=Depends(deps.site_from_request),
) -> Response:
    """站内搜索。默认关闭；关闭时按不存在处理，不对外说明这个能力。"""
    if not request.app.state.settings.site_search_enabled:
        raise ApiError.not_found("站点搜索未开启")
    if site is None:
        raise ApiError.unauthorized("站点搜索需要站点令牌")

    mode, assets = search.published(db, query=q, limit=limit)
    items = [
        payloads.asset_summary(request, asset, site=site)
        for asset in assets
        if deps.searchable(db, asset, site=site)
    ]
    return deps.json_response(request, site=site, payload={"mode": mode, "items": items})
