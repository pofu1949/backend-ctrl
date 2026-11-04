"""上传接口 ``/api/admin/v1/uploads``。

小文件一次 multipart，大文件分片；两条路径共用 ``uploads.service`` 的同一套
校验与落盘逻辑。所有接口都要求 ``asset.upload``，且会话只能由创建者（或所有者）操作。

分片接口收原始字节（``application/octet-stream``），并在异步端点里边读边写：
视频动辄上 GB，不能先整个读进内存。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, File, Form, Path, Query, Request, UploadFile
from sqlalchemy.orm import Session as DbSession

from .. import idempotency
from ..auth import deps
from ..config import Settings
from ..errors import ApiError
from ..storage import LocalStorage, TooLarge
from . import service
from .schemas import CreateUploadRequest, UploadSessionOut

router = APIRouter(tags=["uploads"])

#: 读取上传流的块大小。
CHUNK = 1024 * 1024


@router.post("/uploads", response_model=UploadSessionOut, status_code=201)
def create_upload(
    request: Request,
    payload: CreateUploadRequest,
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> UploadSessionOut:
    """开一个分片会话。返回的 ``byte_limit`` 是该类型的单文件上限。

    带 ``asset_id`` 时是替换：要 ``asset.update``，完成后追加版本，线上版本不变。
    不带则是新建内容，要 ``asset.upload``。
    """
    deps.ensure_permission(
        principal, service.REPLACE_PERMISSION if payload.asset_id else service.CREATE_PERMISSION
    )
    settings = _settings(request)
    session = service.create_session(
        db,
        settings=settings,
        storage=LocalStorage.from_settings(settings),
        actor=principal.user,
        filename=payload.filename,
        declared_type=payload.content_type,
        target_asset_id=payload.asset_id,
    )
    return UploadSessionOut.of(session)


@router.get("/uploads/{upload_id}", response_model=UploadSessionOut)
def read_upload(
    request: Request,
    upload_id: str,
    principal: deps.Principal = Depends(deps.current_principal),
    db: DbSession = Depends(deps.get_db),
) -> UploadSessionOut:
    """查看会话状态，用于断点续传前确认已收到哪些分片。

    到期在这里就结算成 ``expired``，客户端不必等到写片失败才发现。
    """
    if not principal.can("asset.upload"):
        raise ApiError.forbidden("缺少权限：asset.upload")
    storage = LocalStorage.from_settings(_settings(request))
    session = service.get_session(db, upload_id, actor=principal.user)
    service.reconcile_expiry(db, storage=storage, session=session)
    return UploadSessionOut.of(session, duplicate_of=service.duplicate_of(db, session))


@router.put("/uploads/{upload_id}/parts/{index}", response_model=UploadSessionOut)
async def put_part(
    request: Request,
    upload_id: str,
    index: int = Path(ge=1, le=100_000, description="分片序号，从 1 开始连续"),
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> UploadSessionOut:
    """写入一片。同一序号重复提交就覆盖，因此断点续传不需要额外协议。

    请求体按块读取并直接写盘：视频动辄上 GB，不能先整个读进内存。
    文件操作是本地顺序写（每块 1 MB），不额外切线程。
    """
    settings = _settings(request)
    storage = LocalStorage.from_settings(settings)
    session = service.get_session(db, upload_id, actor=principal.user)
    deps.ensure_permission(principal, service.required_permission(session))
    service.ensure_writable(db, storage=storage, session=session)

    try:
        with storage.open_part(session.id, index, session.byte_limit) as writer:
            async for chunk in request.stream():
                writer.write(chunk)
    except TooLarge as exc:
        raise service.part_too_large(session) from exc

    service.record_part(db, storage=storage, session=session, index=index)
    return UploadSessionOut.of(session)


@router.post("/uploads/{upload_id}/complete", response_model=UploadSessionOut)
def complete_upload(
    request: Request,
    upload_id: str,
    folder_id: str | None = Query(default=None, max_length=36),
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> UploadSessionOut:
    """合并并校验。支持 ``Idempotency-Key``：网络重试不会产生第二份原件。

    权限在这里再查一次：会话建好之后被收回权限，也不能靠「已经拿到会话」完成替换。
    ``folder_id`` 为空时新建内容落在根目录；替换已有内容时这个参数不起作用。
    """
    settings = _settings(request)
    session = service.get_session(db, upload_id, actor=principal.user)
    deps.ensure_permission(principal, service.required_permission(session))
    if folder_id == "":
        folder_id = None

    key = request.headers.get(idempotency.HEADER)
    request_hash = idempotency.hash_request(f"{upload_id}:{session.byte_limit}:{folder_id or ''}")
    if key:
        replayed = idempotency.lookup(
            db,
            actor_id=principal.user.id,
            method="POST",
            path=request.url.path,
            key=key,
            request_hash=request_hash,
        )
        if replayed is not None:
            return UploadSessionOut(**replayed.body)

    service.complete(
        db,
        settings=settings,
        storage=LocalStorage.from_settings(settings),
        session=session,
        folder_id=folder_id,
    )
    result = UploadSessionOut.of(session, duplicate_of=service.duplicate_of(db, session))
    if key:
        idempotency.remember(
            db,
            actor_id=principal.user.id,
            method="POST",
            path=request.url.path,
            key=key,
            request_hash=request_hash,
            status=200,
            body=result.model_dump(mode="json"),
        )
    return result


@router.delete("/uploads/{upload_id}", status_code=204, response_model=None)
def cancel_upload(
    request: Request,
    upload_id: str,
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> None:
    """放弃上传并删除临时文件。"""
    settings = _settings(request)
    session = service.get_session(db, upload_id, actor=principal.user)
    deps.ensure_permission(principal, service.required_permission(session))
    service.cancel(db, storage=LocalStorage.from_settings(settings), session=session)


@router.post("/uploads/direct", response_model=UploadSessionOut, status_code=201)
def direct_upload(
    request: Request,
    file: UploadFile = File(..., description="要入库的文件"),
    asset_id: str | None = Form(default=None, description="要替换的内容 ID"),
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> UploadSessionOut:
    """一次性上传。内部走会话 + 单分片 + 完成的同一条路径，不另写登记规则。

    带 ``asset_id`` 是替换，要 ``asset.update``；否则是新建，要 ``asset.upload``。
    """
    deps.ensure_permission(
        principal, service.REPLACE_PERMISSION if asset_id else service.CREATE_PERMISSION
    )
    settings = _settings(request)
    session = service.direct_upload(
        db,
        settings=settings,
        storage=LocalStorage.from_settings(settings),
        actor=principal.user,
        filename=file.filename or "",
        declared_type=file.content_type,
        chunks=_file_chunks(file),
        target_asset_id=asset_id,
    )
    return UploadSessionOut.of(session, duplicate_of=service.duplicate_of(db, session))


# —— 内部辅助 ——


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _file_chunks(file: UploadFile) -> Iterator[bytes]:
    while True:
        chunk = file.file.read(CHUNK)
        if not chunk:
            return
        yield chunk
