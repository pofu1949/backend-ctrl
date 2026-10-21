"""内容接口 ``/api/admin/v1/assets``。

管理视图：编辑中的字段、全部保留版本、标签，以及发布/撤回/归档/回收站。
公开读取（只读发布快照）在第 6 部分实现。

**替换文件不用单独接口**：`POST /uploads` 带 `asset_id` 建会话，完成后自动追加版本。
新建与替换共用一条上传路径，避免出现两套登记规则（企划第 13.1 节的两处入口在这里合并）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DbSession

from .. import idempotency
from ..auth import deps
from ..errors import ApiError
from ..models import ASSET_PUBLISHED
from ..storage import LocalStorage
from . import download, publish, service, signing
from .schemas import (
    AssetDetailOut,
    AssetListOut,
    AssetOut,
    CreateTextAssetRequest,
    SignedUrlOut,
    UpdateAssetRequest,
    VersionActionRequest,
)

router = APIRouter(tags=["assets"])


def _settings(request: Request):
    return request.app.state.settings


def _storage(request: Request) -> LocalStorage:
    return LocalStorage.from_settings(_settings(request))


def _detail(asset) -> AssetDetailOut:
    return AssetDetailOut.detailed(asset, current=service.current_version(asset))


def _replayed(
    request: Request, principal: deps.Principal, payload: VersionActionRequest, db: DbSession
) -> AssetDetailOut | None:
    """带 Idempotency-Key 的重试：返回第一次的结果，不再动状态（企划第 13.3 节）。"""
    key = request.headers.get(idempotency.HEADER)
    if not key or principal.user is None:
        return None
    stored = idempotency.lookup(
        db,
        actor_id=principal.user.id,
        method="POST",
        path=request.url.path,
        key=key,
        request_hash=idempotency.hash_request(payload.model_dump_json()),
    )
    if stored is None:
        return None
    return AssetDetailOut(**stored.body)


def _remember(
    request: Request,
    principal: deps.Principal,
    payload: VersionActionRequest,
    result: AssetDetailOut,
    db: DbSession,
) -> None:
    """记下这次结果。调用方随后统一提交。"""
    key = request.headers.get(idempotency.HEADER)
    if not key or principal.user is None:
        return
    idempotency.remember(
        db,
        actor_id=principal.user.id,
        method="POST",
        path=request.url.path,
        key=key,
        request_hash=idempotency.hash_request(payload.model_dump_json()),
        status=200,
        body=result.model_dump(mode="json"),
    )


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
    return _detail(service.get_asset(db, asset_id))


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
    只改编辑中的内容：线上快照要等发布，所以有修改权限而没有发布权限的人
    改不动访客看到的内容。

    已发布内容改可见性会改变「是否对外」，因此额外要求 ``asset.publish``。
    """
    settings = _settings(request)
    storage = _storage(request)
    asset = service.get_asset(db, asset_id)
    if payload.visibility is not None and asset.status == ASSET_PUBLISHED:
        deps.ensure_permission(principal, "asset.publish")

    service.apply_update(
        db,
        settings=settings,
        storage=storage,
        asset=asset,
        update=service.Update(changes=payload.changes(), tags=payload.tags, body=payload.body),
        expected_revision=payload.revision,
        actor=principal.user,
    )
    if payload.visibility is not None and asset.status == ASSET_PUBLISHED:
        # 公开 ↔ 私有/仅站点 的切换要立刻反映到公开区，且顺序与发布一致：
        # 先落库再动链接。反过来（先删链接后提交）一旦提交失败，链接已经没了，
        # 状态却还是「已发布」，读的人会看到 404 而库里说在线。
        db.commit()
        publish.sync_links(storage, asset)
    return _detail(asset)


# —— 发布与生命周期 ——


@router.post("/assets/{asset_id}/publish", response_model=AssetDetailOut)
def publish_asset(
    asset_id: str,
    payload: VersionActionRequest,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("asset.publish")),
    db: DbSession = Depends(deps.get_db),
) -> AssetDetailOut:
    """把当前版本发布成线上版本，并复制标题、摘要、封面与标签快照。

    带未来的 ``publish_at`` 时只登记计划：到点前不写快照、不进公开区，
    由 worker 的每分钟任务执行（企划第 8.6 节 P0-7）。
    """
    asset = service.get_asset(db, asset_id)
    replayed = _replayed(request, principal, payload, db)
    if replayed is not None:
        return replayed

    if payload.publish_at is not None and publish.is_future(payload.publish_at):
        publish.schedule(
            db,
            asset=asset,
            revision=payload.revision,
            actor=principal.user,
            when=payload.publish_at,
        )
    else:
        publish.publish(
            db,
            storage=_storage(request),
            asset=asset,
            revision=payload.revision,
            actor=principal.user,
        )
    result = _detail(asset)
    _remember(request, principal, payload, result, db)
    return result


@router.post("/assets/{asset_id}/unpublish", response_model=AssetDetailOut)
def unpublish_asset(
    asset_id: str,
    payload: VersionActionRequest,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("asset.publish")),
    db: DbSession = Depends(deps.get_db),
) -> AssetDetailOut:
    """撤回发布：公开地址立即 404，管理端仍能下载原件。"""
    asset = service.get_asset(db, asset_id)
    replayed = _replayed(request, principal, payload, db)
    if replayed is not None:
        return replayed

    publish.unpublish(
        db, storage=_storage(request), asset=asset, revision=payload.revision, actor=principal.user
    )
    result = _detail(asset)
    _remember(request, principal, payload, result, db)
    return result


@router.post("/assets/{asset_id}/archive", response_model=AssetDetailOut)
def archive_asset(
    asset_id: str,
    payload: VersionActionRequest,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("asset.publish")),
    db: DbSession = Depends(deps.get_db),
) -> AssetDetailOut:
    asset = service.get_asset(db, asset_id)
    publish.archive(
        db, storage=_storage(request), asset=asset, revision=payload.revision, actor=principal.user
    )
    return _detail(asset)


@router.post("/assets/{asset_id}/trash", response_model=AssetDetailOut)
def trash_asset(
    asset_id: str,
    payload: VersionActionRequest,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("asset.trash")),
    db: DbSession = Depends(deps.get_db),
) -> AssetDetailOut:
    """放入回收站。公开链接先撤下，再改状态。"""
    asset = service.get_asset(db, asset_id)
    publish.trash(
        db, storage=_storage(request), asset=asset, revision=payload.revision, actor=principal.user
    )
    return _detail(asset)


@router.post("/assets/{asset_id}/restore", response_model=AssetDetailOut)
def restore_asset(
    asset_id: str,
    payload: VersionActionRequest,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("asset.trash")),
    db: DbSession = Depends(deps.get_db),
) -> AssetDetailOut:
    """从回收站恢复。不自动重新公开。"""
    asset = service.get_asset(db, asset_id)
    publish.restore(
        db, storage=_storage(request), asset=asset, revision=payload.revision, actor=principal.user
    )
    return _detail(asset)


@router.delete("/assets/{asset_id}", status_code=204, response_model=None)
def purge_asset(
    asset_id: str,
    request: Request,
    principal: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> None:
    """彻底删除（仅所有者）。必须已在回收站，且没有被别的内容当封面引用。"""
    asset = service.get_asset(db, asset_id)
    publish.purge(db, storage=_storage(request), asset=asset, actor=principal.user)


# —— 文件 ——


@router.get("/assets/{asset_id}/file")
def download_asset(
    asset_id: str,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("asset.read")),
    db: DbSession = Depends(deps.get_db),
) -> Response:
    """管理下载：任意状态都能取原件，鉴权后交给 Nginx 内部跳转（支持 Range）。"""
    asset = service.get_asset(db, asset_id)
    version = service.current_version(asset)
    if version is None:
        raise ApiError.not_found("内容还没有版本")
    return download.send_file(
        request,
        settings=_settings(request),
        storage=_storage(request),
        asset=asset,
        version=version,
    )


@router.get("/assets/{asset_id}/signed-url", response_model=SignedUrlOut)
def signed_url(
    asset_id: str,
    request: Request,
    ttl: int = Query(default=signing.DEFAULT_TTL, ge=1, le=signing.MAX_TTL),
    principal: deps.Principal = Depends(deps.require_permission("asset.read")),
    db: DbSession = Depends(deps.get_db),
) -> SignedUrlOut:
    """给「仅站点」内容签一个短时地址。地址绑定已发布版本，撤回后自动失效。"""
    settings = _settings(request)
    asset = service.get_asset(db, asset_id)
    # 签名必须绑定线上那一版：待发布的替换不该改变已发出的地址。
    version = publish.published_version(asset)
    if asset.status != ASSET_PUBLISHED or version is None:
        raise ApiError.conflict("只有已发布的内容可以签短时地址")
    signed = signing.sign(
        settings.pageview_hmac_secret,
        asset_id=asset.id,
        version_id=version.id,
        ttl=ttl,
    )
    query = "&".join(f"{key}={value}" for key, value in signed.query().items())
    return SignedUrlOut(
        url=f"{settings.public_base_url}/api/public/v1/files/{asset.id}?{query}",
        expires_at=signed.expires,
    )
