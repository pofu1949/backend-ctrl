"""公开集合读取 ``GET /api/public/v1/collections/{slug}``。

按可见性过滤条目：匿名只看公开；站点令牌还能看自己已授权集合里的「仅站点」内容；
「未列出」只有在授权显式允许时才出现在列表里（知道完整地址仍可单条读取）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..auth import deps as auth_deps
from ..collections import service as collections
from ..errors import ApiError
from ..models import Asset, CollectionItem
from . import deps, payloads

router = APIRouter(tags=["public"])


@router.get("/collections/{slug}")
def read_collection(
    slug: str,
    request: Request,
    db: DbSession = Depends(auth_deps.get_db),
    site=Depends(deps.site_from_request),
) -> Response:
    """集合 JSON。地址绑定别名，不绑定磁盘路径。"""
    collection = collections.get_by_slug(db, slug.strip().lower())
    if collection is None or not deps.collection_readable(db, collection, site=site):
        raise ApiError.not_found("集合不存在")

    rows = db.scalars(
        select(CollectionItem)
        .where(CollectionItem.collection_id == collection.id)
        .order_by(CollectionItem.position)
    ).all()
    assets = _load_assets(db, [row.asset_id for row in rows])
    visible = [
        row
        for row in rows
        if row.asset_id in assets
        and deps.visible_in_collection(db, collection, assets[row.asset_id], site=site)
    ]

    payload = payloads.collection_payload(
        request,
        collection,
        visible,
        assets=[assets[row.asset_id] for row in visible],
        site=site,
    )
    return deps.json_response(request, site=site, payload=payload)


def _load_assets(db: DbSession, asset_ids: list[str]) -> dict[str, Asset]:
    if not asset_ids:
        return {}
    rows = db.scalars(select(Asset).where(Asset.id.in_(asset_ids))).all()
    return {asset.id: asset for asset in rows}
