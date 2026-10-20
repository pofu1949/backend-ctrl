"""短时签名下载（企划场景 E）。

「仅站点」内容不进入 ``/p/``。网站后端拿签名地址来取字节，
这里只做三件事：验签、确认内容仍处于可读状态、把文件发出去。

签名绑定「内容 ID + 已发布版本 ID + 过期时间」，所以撤回或替换之后旧地址自然失效，
不需要维护吊销表。签名地址只能由持有站点令牌或管理会话的一方获得。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DbSession

from ..assets import download, publish, service, signing
from ..auth import deps
from ..errors import ApiError
from ..models import ASSET_PUBLISHED, VISIBILITY_PRIVATE, VISIBILITY_SITE, Asset
from ..storage import LocalStorage

router = APIRouter(tags=["public"])

#: 可以用签名地址读取的可见性。私有内容只有所有者能通过管理接口下载。
SIGNED_VISIBILITIES = frozenset(publish.PUBLIC_VISIBILITIES | {VISIBILITY_SITE})


@router.get("/files/{asset_id}")
def download_signed(
    asset_id: str,
    request: Request,
    version: str = Query(description="已发布版本 ID"),
    expires: int = Query(description="过期时间（Unix 秒）"),
    sig: str = Query(description="签名"),
    db: DbSession = Depends(deps.get_db),
) -> Response:
    """签名下载。失败一律 404：不告诉对方是签名错还是内容已下架。"""
    settings = request.app.state.settings
    if not signing.verify(
        settings.pageview_hmac_secret,
        asset_id=asset_id,
        version_id=version,
        expires=expires,
        signature=sig,
    ):
        raise ApiError.not_found("文件不可用")

    asset = db.get(Asset, asset_id)
    if asset is None or not _readable(asset) or asset.published_version_id != version:
        raise ApiError.not_found("文件不可用")

    record = service.get_version(db, version)
    return download.send_file(
        request,
        settings=settings,
        storage=LocalStorage.from_settings(settings),
        asset=asset,
        version=record,
    )


def _readable(asset: Asset) -> bool:
    """已发布，且可见性允许被网站读取。"""
    return asset.status == ASSET_PUBLISHED and asset.visibility in SIGNED_VISIBILITIES


assert VISIBILITY_PRIVATE not in SIGNED_VISIBILITIES  # 私有内容永远不走签名地址
