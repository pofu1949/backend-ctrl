"""内容接口 ``/api/admin/v1/assets``。

这里只做管理视图：编辑中的字段、全部保留版本、标签。公开读取（只读发布快照）
在第 6 部分实现。

**替换文件不用单独接口**：`POST /uploads` 带 `asset_id` 建会话，完成后自动追加版本。
新建与替换共用一条上传路径，避免出现两套登记规则（企划第 13.1 节的两处入口在这里合并）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session as DbSession

from ..auth import deps
from ..storage import LocalStorage
from . import service
from .schemas import (
    AssetDetailOut,
    AssetListOut,
    AssetOut,
    CreateTextAssetRequest,
    UpdateAssetRequest,
)

router = APIRouter(tags=["assets"])


@router.get("/assets", response_model=AssetListOut)
def list_assets(
    kind: str | None = Query(default=None, max_length=16),
    folder_id: str | None = Query(default=None),
    status: str | None = Query(default=None, max_length=16),
    tag: str | None = Query(default=None, max_length=64, description="标签 slug"),
    q: str | None = Query(default=None, max_length=128, description="标题或别名子串"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
    _: deps.Principal = Depends(deps.require_permission("asset.read")),
    db: DbSession = Depends(deps.get_db),
) -> AssetListOut:
    """按 ``created_at, id`` 的游标分页，默认不含回收站。"""
    items, next_cursor = service.list_assets(
        db,
        kind=kind,
        folder_id=folder_id,
        status=status,
        tag=tag,
        query=q,
        cursor=cursor,
        limit=limit,
    )
    return AssetListOut(
        items=[AssetOut.of(asset, current=service.current_version(asset)) for asset in items],
        next_cursor=next_cursor,
    )


@router.post("/assets", response_model=AssetDetailOut, status_code=201)
def create_text_asset(
    payload: CreateTextAssetRequest,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("asset.upload")),
    db: DbSession = Depends(deps.get_db),
) -> AssetDetailOut:
    """直接创建 Markdown 或纯文本。正文成为 ``v1`` 的文件，与上传走同一套版本结构。"""
    asset, version = service.create_text(
        db,
        settings=request.app.state.settings,
        storage=LocalStorage.from_settings(request.app.state.settings),
        actor=principal.user,
        kind=payload.kind,
        title=payload.title,
        body=payload.body,
        folder_id=payload.folder_id,
        tags=payload.tags,
        slug=payload.slug,
        summary=payload.summary,
    )
    return AssetDetailOut.detailed(asset, current=version)


@router.get("/assets/{asset_id}", response_model=AssetDetailOut)
def read_asset(
    asset_id: str,
    _: deps.Principal = Depends(deps.require_permission("asset.read")),
    db: DbSession = Depends(deps.get_db),
) -> AssetDetailOut:
    asset = service.get_asset(db, asset_id)
    return AssetDetailOut.detailed(asset, current=service.current_version(asset))


@router.patch("/assets/{asset_id}", response_model=AssetDetailOut)
def update_asset(
    asset_id: str,
    payload: UpdateAssetRequest,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("asset.update")),
    db: DbSession = Depends(deps.get_db),
) -> AssetDetailOut:
    """改元数据、标签或正文。``revision`` 不一致返回 409，提示重新读取。

    ``body`` 只对 Markdown 与纯文本有效，会写成新版本（企划第 8.4 节 T1）。
    只改编辑中的内容：线上快照要等发布（第 5 部分），所以有修改权限而没有发布权限的人
    改不动访客看到的内容。
    """
    settings = request.app.state.settings
    asset = service.get_asset(db, asset_id)
    service.apply_update(
        db,
        settings=settings,
        storage=LocalStorage.from_settings(settings),
        asset=asset,
        update=service.Update(changes=payload.changes(), tags=payload.tags, body=payload.body),
        expected_revision=payload.revision,
        actor=principal.user,
    )
    return AssetDetailOut.detailed(asset, current=service.current_version(asset))
