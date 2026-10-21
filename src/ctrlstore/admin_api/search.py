"""管理端检索 ``GET /api/admin/v1/search``（企划第 8.7 节）。

三字及以上查全文（FTS5 trigram）；一两个字只查标题、文件名与标签，最多 50 条。
回收站默认不返回——要看待删除内容走列表接口的 ``status=trashed``。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from ..assets import service as asset_service
from ..auth import deps
from ..models import Asset
from ..search import service as search

router = APIRouter(tags=["search"])


class SearchHitOut(BaseModel):
    """检索结果的一行。``filename`` 是当前版本的文件名，便于认出同名内容。"""

    id: str
    kind: str
    title: str
    filename: str
    status: str
    visibility: str
    folder_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    updated_at: datetime

    @classmethod
    def of(cls, asset: Asset) -> SearchHitOut:
        version = asset_service.current_version(asset)
        return cls(
            id=asset.id,
            kind=asset.kind,
            title=asset.title,
            filename=version.original_filename if version else "",
            status=asset.status,
            visibility=asset.visibility,
            folder_id=asset.folder_id,
            tags=sorted(tag.name for tag in asset.tags),
            updated_at=asset.updated_at,
        )


class SearchOut(BaseModel):
    """``mode`` 告诉界面这次走的是全文还是标题匹配。"""

    mode: str
    items: list[SearchHitOut]


@router.get("/search")
def search_assets(
    q: str = Query(min_length=1, max_length=search.MAX_QUERY_LENGTH),
    limit: int = Query(default=search.MAX_RESULTS, ge=1, le=search.MAX_RESULTS),
    _: deps.Principal = Depends(deps.require_permission("asset.read")),
    db: DbSession = Depends(deps.get_db),
) -> SearchOut:
    mode, assets = search.admin(db, query=q, limit=limit)
    return SearchOut(mode=mode, items=[SearchHitOut.of(asset) for asset in assets])
