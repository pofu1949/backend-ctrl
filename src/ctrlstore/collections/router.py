"""集合与站点授权接口 ``/api/admin/v1/collections``。

排序用一次 JSON 请求把整组顺序提交（企划第 13.1 节），服务端在一个事务里重排：
顺序要么全对，要么完全不变，不会出现"改了一半"的集合。

站点授权挂在 ``/sites/{id}/grants`` 下：前缀按点号分段，``home`` 覆盖 ``home.hero``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from ..auth import deps
from ..errors import ApiError
from ..models import Site
from . import service
from .schemas import (
    AddItemRequest,
    CaptionRequest,
    CollectionItemOut,
    CollectionOut,
    CreateCollectionRequest,
    CreateGrantRequest,
    GrantOut,
    ReorderRequest,
    UpdateCollectionRequest,
)

router = APIRouter(tags=["collections"])


@router.get("/collections", response_model=list[CollectionOut])
def list_collections(
    _: deps.Principal = Depends(deps.require_permission("asset.read")),
    db: DbSession = Depends(deps.get_db),
) -> list[CollectionOut]:
    """按别名排序，便于在界面里直接分组。"""
    return [CollectionOut.of(row) for row in service.list_collections(db)]


@router.post("/collections", response_model=CollectionOut, status_code=201)
def create_collection(
    payload: CreateCollectionRequest,
    principal: deps.Principal = Depends(deps.require_permission("collection.manage")),
    db: DbSession = Depends(deps.get_db),
) -> CollectionOut:
    collection = service.create_collection(
        db,
        slug=payload.slug,
        title=payload.title,
        site_id=payload.site_id,
        kinds=payload.allowed_kinds,
        actor=principal.user,
    )
    return CollectionOut.of(collection)


@router.patch("/collections/{collection_id}", response_model=CollectionOut)
def update_collection(
    collection_id: str,
    payload: UpdateCollectionRequest,
    principal: deps.Principal = Depends(deps.require_permission("collection.manage")),
    db: DbSession = Depends(deps.get_db),
) -> CollectionOut:
    """改标题或类型限制。收紧限制会先确认现有条目都符合。"""
    collection = service.update_collection(
        db,
        service.get_collection(db, collection_id),
        title=payload.title,
        kinds=payload.allowed_kinds,
        actor=principal.user,
    )
    return CollectionOut.of(collection)


@router.get("/collections/{collection_id}/items", response_model=list[CollectionItemOut])
def list_items(
    collection_id: str,
    _: deps.Principal = Depends(deps.require_permission("asset.read")),
    db: DbSession = Depends(deps.get_db),
) -> list[CollectionItemOut]:
    collection = service.get_collection(db, collection_id)
    return [CollectionItemOut.of(item) for item in collection.items]


@router.post(
    "/collections/{collection_id}/items", response_model=CollectionItemOut, status_code=201
)
def add_item(
    collection_id: str,
    payload: AddItemRequest,
    principal: deps.Principal = Depends(deps.require_permission("collection.manage")),
    db: DbSession = Depends(deps.get_db),
) -> CollectionItemOut:
    collection = service.get_collection(db, collection_id)
    item = service.add_item(
        db,
        collection,
        asset_id=payload.asset_id,
        caption=payload.caption,
        position=payload.position,
        actor=principal.user,
    )
    return CollectionItemOut.of(item)


@router.delete(
    "/collections/{collection_id}/items/{asset_id}", status_code=204, response_model=None
)
def remove_item(
    collection_id: str,
    asset_id: str,
    principal: deps.Principal = Depends(deps.require_permission("collection.manage")),
    db: DbSession = Depends(deps.get_db),
) -> None:
    service.remove_item(
        db, service.get_collection(db, collection_id), asset_id=asset_id, actor=principal.user
    )


@router.patch("/collections/{collection_id}/items/{asset_id}", response_model=CollectionItemOut)
def set_caption(
    collection_id: str,
    asset_id: str,
    payload: CaptionRequest,
    _: deps.Principal = Depends(deps.require_permission("collection.manage")),
    db: DbSession = Depends(deps.get_db),
) -> CollectionItemOut:
    """改这一处的展示标题（不影响内容自己的标题）。"""
    item = service.set_caption(
        db, service.get_collection(db, collection_id), asset_id=asset_id, caption=payload.caption
    )
    return CollectionItemOut.of(item)


@router.put("/collections/{collection_id}/order", response_model=list[CollectionItemOut])
def reorder(
    collection_id: str,
    payload: ReorderRequest,
    principal: deps.Principal = Depends(deps.require_permission("collection.manage")),
    db: DbSession = Depends(deps.get_db),
) -> list[CollectionItemOut]:
    """整体重排。任一位置冲突则整次失败，集合保持原顺序。"""
    collection = service.get_collection(db, collection_id)
    service.reorder(db, collection, order=payload.asset_ids)
    return [CollectionItemOut.of(item) for item in collection.items]


# —— 站点授权 ——


@router.get("/sites/{site_id}/grants", response_model=list[GrantOut])
def list_grants(
    site_id: str,
    _: deps.Principal = Depends(deps.owner),
    db: DbSession = Depends(deps.get_db),
) -> list[GrantOut]:
    site = _get_site(db, site_id)
    return [GrantOut.model_validate(grant) for grant in service.list_grants(db, site)]


@router.post("/sites/{site_id}/grants", response_model=GrantOut, status_code=201)
def add_grant(
    site_id: str,
    payload: CreateGrantRequest,
    principal: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> GrantOut:
    """授权站点读取某个前缀下的集合。``home`` 覆盖 ``home.hero``，不覆盖 ``homepage``。"""
    site = _get_site(db, site_id)
    grant = service.add_grant(
        db,
        site,
        prefix=payload.prefix,
        can_read_unlisted=payload.can_read_unlisted,
        actor=principal.user,
    )
    return GrantOut.model_validate(grant)


@router.delete("/sites/{site_id}/grants/{grant_id}", status_code=204, response_model=None)
def remove_grant(
    site_id: str,
    grant_id: str,
    principal: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> None:
    site = _get_site(db, site_id)
    grant = next((row for row in service.list_grants(db, site) if row.id == grant_id), None)
    if grant is None:
        raise ApiError.not_found("授权不存在")
    service.remove_grant(db, grant, actor=principal.user)


def _get_site(db: DbSession, site_id: str) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise ApiError.not_found("站点不存在")
    return site
