"""上传会话的业务逻辑。

分片上传与一次性上传走同一条路径：**建会话 → 写分片 → 完成**。
一次性上传只是「建会话 + 写第 1 片 + 完成」的包装，所以类型判断、上限、
哈希和落盘只有一份实现，不会出现两套登记规则（企划第 13.1 节）。

完成的含义：字节已经在校验后落到 ``originals/``，会话里留下登记内容所需的一切
（``asset_id``、``storage_key``、``kind``、``mime``、``sha256``、尺寸）。
第 3 部分到此为止，不建 ``assets``——那是第 4 部分的事。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import audit, idempotency, quota
from ..assets import service as assets
from ..config import Settings
from ..errors import ApiError
from ..health import disk_usage, has_available_memory
from ..models import (
    UPLOAD_CREATED,
    UPLOAD_EXPIRED,
    UPLOAD_FAILED,
    UPLOAD_MERGING,
    UPLOAD_OPEN_STATUSES,
    UPLOAD_READY,
    UPLOAD_RECEIVING,
    UploadSession,
    User,
    utcnow,
)
from ..storage import LocalStorage, TooLarge
from . import inspection

#: 未完成会话的存活时间（企划第 8.3 节 U3）。
SESSION_TTL_HOURS = 24

#: 同时未完成的会话数上限：每名用户 2 个，全库 4 个。
MAX_OPEN_PER_USER = 2
MAX_OPEN_GLOBAL = 4

#: 新建内容需要的能力。
CREATE_PERMISSION = "asset.upload"
#: 替换已有文件属于修改（企划第 8.6 节），不是上传。
REPLACE_PERMISSION = "asset.update"


def required_permission(session: UploadSession) -> str:
    """这次上传需要哪项能力。

    会话带 ``asset_id`` 就是替换：它写的是已有内容的新版本，因此要 ``asset.update``。
    只有上传权限的编辑不能借替换改掉别人的内容。
    """
    return REPLACE_PERMISSION if session.asset_id else CREATE_PERMISSION


def create_session(
    db: DbSession,
    *,
    settings: Settings,
    storage: LocalStorage,
    actor: User,
    filename: str,
    declared_type: str | None,
    target_asset_id: str | None = None,
) -> UploadSession:
    """开一个上传会话。

    ``target_asset_id`` 为空表示上传一条新内容；有值表示给那条内容替换文件，
    完成后由 ``assets.service`` 追加成新版本。
    """
    # 清理先落库：后面若因磁盘或并发上限被拒，这些已作废的会话不该跟着回滚。
    if expire_stale(db, storage=storage):
        db.commit()
    _ensure_capacity(settings)

    kind, _ = _kind_or_invalid(filename)
    # 早拒绝：按该类型的单文件上限预估，省掉一整段传输（企划第 8.3 节 U6）。
    quota.ensure_capacity(
        db,
        settings=settings,
        owner_id=_content_owner_id(db, target_asset_id=target_asset_id, fallback=actor.id),
        extra_bytes=settings.limits.for_kind(kind),
    )
    _ensure_slot_available(db, actor)
    if target_asset_id:
        _ensure_target_replaceable(db, target_asset_id, kind=kind)
    else:
        # 替换不增加条数；新建内容才查条数配额。
        quota.ensure_item_capacity(db, settings=settings, owner_id=actor.id)

    session = UploadSession(
        actor_id=actor.id,
        asset_id=target_asset_id,
        original_filename=_trim_filename(filename),
        content_type=(declared_type or "").strip() or None,
        kind=kind,
        byte_limit=settings.limits.for_kind(kind),
        status=UPLOAD_CREATED,
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(hours=SESSION_TTL_HOURS),
    )
    session.parts = []
    db.add(session)
    db.flush()
    storage.prepare_session(session.id)
    audit.record_for_user(
        db,
        actor,
        action=audit.UPLOAD_CREATED,
        target_type=audit.TARGET_UPLOAD,
        target_id=session.id,
        detail={
            "filename": session.original_filename,
            "kind": kind,
            "target_asset_id": target_asset_id,
        },
    )
    return session


def _ensure_target_replaceable(db: DbSession, asset_id: str, *, kind: str) -> None:
    """替换目标要存在、可用，且类型一致——图片不能替换成视频。"""
    from ..models import ASSET_TRASHED, Asset

    asset = db.get(Asset, asset_id)
    if asset is None:
        raise ApiError.not_found("要替换的内容不存在")
    if asset.status == ASSET_TRASHED:
        raise ApiError.conflict("回收站中的内容不能替换文件")
    if asset.kind != kind:
        raise ApiError.invalid(f"新文件是 {kind}，与内容的 {asset.kind} 不一致")


def add_part(
    db: DbSession,
    *,
    storage: LocalStorage,
    session: UploadSession,
    index: int,
    chunks: Iterable[bytes],
) -> UploadSession:
    """写入一片。同一序号重传直接覆盖（断点续传的常见做法）。"""
    ensure_writable(db, storage=storage, session=session)
    _ensure_index(index)

    try:
        with storage.open_part(session.id, index, session.byte_limit) as writer:
            for chunk in chunks:
                writer.write(chunk)
    except TooLarge as exc:
        raise part_too_large(session) from exc

    return record_part(db, storage=storage, session=session, index=index)


def record_part(
    db: DbSession, *, storage: LocalStorage, session: UploadSession, index: int
) -> UploadSession:
    """把「这一片已收到」写进会话。

    异步端点自己驱动文件写入（请求体是异步流），写完调这个函数，
    数据库侧的规则仍然只有一份。
    """
    session.parts = [*session.parts, index]
    session.byte_size = storage.parts_size(session.id)
    session.status = UPLOAD_RECEIVING
    return session


def part_too_large(session: UploadSession) -> ApiError:
    """超限的统一说法，供同步与异步两条写入路径共用。"""
    return ApiError.too_large(f"分片累计超过单文件上限（{session.byte_limit} 字节）")


def _ensure_index(index: int) -> None:
    if index < 1:
        raise ApiError.invalid("分片序号从 1 起")


def complete(
    db: DbSession,
    *,
    settings: Settings,
    storage: LocalStorage,
    session: UploadSession,
    folder_id: str | None = None,
) -> UploadSession:
    """合并、校验、落盘。任一步失败都清掉临时目录并把会话标成失败。

    ``folder_id`` 只作用于新建内容。替换已有内容时忽略它，避免换文件顺便改目录。
    目录在合并前先核对，不存在就停在当前会话，不把已经收齐的分片标成失败。
    """
    ensure_writable(db, storage=storage, session=session)
    placed_in = None if session.asset_id else assets._folder_id(db, folder_id)
    indices = _contiguous_parts(session)
    _ensure_capacity(settings)

    session.status = UPLOAD_MERGING
    db.flush()
    try:
        merged, sha256, size = storage.merge_parts(session.id, indices)
        result = inspection.inspect(
            merged,
            filename=session.original_filename,
            declared_type=session.content_type,
            settings=settings,
            sha256=sha256,
            byte_size=size,
        )
        _ensure_capacity(settings)
        # 合并后再按真实字节数检查一次：开会话时用的是上限预估。
        quota.ensure_capacity(
            db,
            settings=settings,
            owner_id=_content_owner_id(
                db, target_asset_id=session.asset_id, fallback=session.actor_id
            ),
            extra_bytes=result.byte_size,
        )

        # 登记与落盘一起完成：新内容或该内容的新版本，规则都在 assets.service。
        asset, version = assets.register_upload(
            db,
            settings=settings,
            storage=storage,
            session=session,
            merged=merged,
            inspection=result,
            folder_id=placed_in,
        )
    except inspection.RejectedFile as exc:
        _fail(db, storage, session, message=str(exc))
        raise ApiError.invalid(str(exc)) from exc
    except ApiError:
        _fail(db, storage, session, message="登记内容失败")
        raise
    except Exception as exc:  # 合并或落盘失败：留下可追溯的失败状态
        _fail(db, storage, session, message=f"上传处理失败：{exc}")
        raise

    session.asset_id = asset.id
    session.kind = result.kind
    session.mime = result.mime
    session.sha256 = result.sha256
    session.byte_size = result.byte_size
    session.storage_key = version.storage_key
    session.width = result.width
    session.height = result.height
    session.status = UPLOAD_READY
    session.completed_at = utcnow()
    session.error = None
    storage.discard_session(session.id)
    audit.record(
        db,
        action=audit.UPLOAD_COMPLETED,
        actor_type=audit.ACTOR_USER,
        actor_id=session.actor_id,
        target_type=audit.TARGET_UPLOAD,
        target_id=session.id,
        detail={"kind": result.kind, "sha256": result.sha256, "bytes": result.byte_size},
    )
    return session


def direct_upload(
    db: DbSession,
    *,
    settings: Settings,
    storage: LocalStorage,
    actor: User,
    filename: str,
    declared_type: str | None,
    chunks: Iterable[bytes],
    target_asset_id: str | None = None,
) -> UploadSession:
    """一次性上传：复用会话与完成逻辑，不另写一套登记规则。"""
    session = create_session(
        db,
        settings=settings,
        storage=storage,
        actor=actor,
        filename=filename,
        declared_type=declared_type,
        target_asset_id=target_asset_id,
    )
    try:
        add_part(db, storage=storage, session=session, index=1, chunks=chunks)
        return complete(db, settings=settings, storage=storage, session=session)
    except Exception:
        # complete 失败时已自行标记；add_part 失败（超限）在这里收尾。
        if session.is_open:
            _fail(db, storage, session, message=session.error or "上传中断")
        raise


def get_session(db: DbSession, session_id: str, *, actor: User) -> UploadSession:
    """按 ID 取会话。编辑只能看自己的；所有者可以看全部。"""
    session = db.get(UploadSession, session_id)
    if session is None:
        raise ApiError.not_found("上传会话不存在")
    if not actor.is_owner and session.actor_id != actor.id:
        raise ApiError.not_found("上传会话不存在")
    return session


def cancel(db: DbSession, *, storage: LocalStorage, session: UploadSession) -> None:
    """放弃上传，删掉临时文件。

    已经到期的会话按「已作废」处理并正常返回：调用方想要的是「别再占着」，
    给他一个 409 只会让客户端多做一次无意义的判断。
    """
    if reconcile_expiry(db, storage=storage, session=session):
        return
    if not session.is_open:
        raise ApiError.conflict("该上传已经结束")
    storage.discard_session(session.id)
    session.status = UPLOAD_FAILED
    session.error = "已被取消"
    audit.record(
        db,
        action=audit.UPLOAD_CANCELLED,
        actor_type=audit.ACTOR_USER,
        actor_id=session.actor_id,
        target_type=audit.TARGET_UPLOAD,
        target_id=session.id,
    )


def expire_stale(db: DbSession, *, storage: LocalStorage) -> int:
    """批量清理超过 24 小时仍未结束的会话，顺带清理过期的幂等键。

    worker 的定时清理在第 7 部分；这里在每次建会话时顺手做一次。
    单个会话的到期判断见 :func:`reconcile_expiry`——那条路径不依赖这里是否被调用。
    """
    rows = db.scalars(
        select(UploadSession).where(
            UploadSession.status.in_(UPLOAD_OPEN_STATUSES),
            UploadSession.expires_at <= utcnow(),
        )
    ).all()
    for session in rows:
        _mark_expired(storage, session)
    idempotency.purge_expired(db)
    return len(rows)


def import_file(
    db: DbSession,
    *,
    settings: Settings,
    storage: LocalStorage,
    actor: User,
    path: Path,
    target_asset_id: str | None = None,
) -> UploadSession:
    """从服务器本地路径导入（企划第 8.3 节 U8）。

    免去「先下载到个人电脑再上传」，但仍然走同一套会话、校验与落盘规则。
    ``target_asset_id`` 有值时作为该内容的新版本导入。
    """
    if not path.is_file():
        raise ApiError.not_found(f"文件不存在：{path}")
    kind, _ = _kind_or_invalid(path.name)
    limit = settings.limits.for_kind(kind)
    file_size = path.stat().st_size
    if file_size > limit:
        raise ApiError.too_large(f"文件为 {file_size} 字节，超过 {kind} 的上限 {limit} 字节")

    session = create_session(
        db,
        settings=settings,
        storage=storage,
        actor=actor,
        filename=path.name,
        declared_type=None,
        target_asset_id=target_asset_id,
    )
    try:
        add_part(db, storage=storage, session=session, index=1, chunks=_file_chunks(path))
        return complete(db, settings=settings, storage=storage, session=session)
    except Exception:
        if session.is_open:
            _fail(db, storage, session, message=session.error or "导入中断")
        raise


def _file_chunks(path: Path) -> Iterable[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            yield chunk


def duplicate_of(db: DbSession, session: UploadSession) -> str | None:
    """同字节的最近一次已完成上传所对应的**内容 ID**。

    编辑要决定的是「是否改引用」，引用的是内容而不是某次上传会话。
    """
    if not session.sha256:
        return None
    return db.scalar(
        select(UploadSession.asset_id)
        .where(
            UploadSession.sha256 == session.sha256,
            UploadSession.status == UPLOAD_READY,
            UploadSession.asset_id.is_not(None),
            UploadSession.id != session.id,
        )
        .order_by(UploadSession.completed_at.desc())
        .limit(1)
    )


# —— 内部检查 ——


def _kind_or_invalid(filename: str) -> tuple[str, str]:
    try:
        return inspection.kind_for_filename(filename)
    except inspection.RejectedFile as exc:
        raise ApiError.invalid(str(exc)) from exc


def _content_owner_id(db: DbSession, *, target_asset_id: str | None, fallback: str) -> str:
    """配额的计账主体是内容所有者，不是本次操作者。

    新建内容时二者相同；编辑替换他人内容时，新版本的字节记在原所有者名下（企划第 11.4 节）。
    """
    if not target_asset_id:
        return fallback
    from ..models import Asset

    asset = db.get(Asset, target_asset_id)
    return asset.owner_id if asset is not None else fallback


def _trim_filename(filename: str) -> str:
    """只保留末段名字：原始文件名用于下载头，不该带路径。"""
    name = filename.replace("\\", "/").split("/")[-1].strip()
    if not name:
        raise ApiError.invalid("文件名不能为空")
    return name[:255]


def _ensure_capacity(settings: Settings) -> None:
    """磁盘与内存阈值：任一不足都拒绝新上传（企划第 11.4、17.2 节）。

    已发布文件的读取与撤回不受这里的限制；只挡新工作。
    """
    if not has_available_memory(settings):
        raise ApiError.insufficient_storage("可用内存不足，暂不接受新上传")
    usage = disk_usage(settings.data_dir)
    if usage is None:
        raise ApiError.insufficient_storage("无法确认磁盘剩余空间")
    free, total = usage
    if free < settings.min_free_bytes or (total and free / total < settings.min_free_ratio):
        raise ApiError.insufficient_storage("磁盘剩余空间不足，暂不接受新上传")


def _ensure_slot_available(db: DbSession, actor: User) -> None:
    open_states = list(UPLOAD_OPEN_STATUSES)
    per_user = db.scalar(
        select(func.count())
        .select_from(UploadSession)
        .where(UploadSession.actor_id == actor.id, UploadSession.status.in_(open_states))
    )
    if (per_user or 0) >= MAX_OPEN_PER_USER:
        raise ApiError.conflict(f"同时未完成的上传不能超过 {MAX_OPEN_PER_USER} 个")
    total = db.scalar(
        select(func.count()).select_from(UploadSession).where(UploadSession.status.in_(open_states))
    )
    if (total or 0) >= MAX_OPEN_GLOBAL:
        raise ApiError.conflict(f"全库同时未完成的上传不能超过 {MAX_OPEN_GLOBAL} 个，请稍后再试")


def reconcile_expiry(db: DbSession, *, storage: LocalStorage, session: UploadSession) -> bool:
    """会话过了 24 小时就当场作废：清临时文件、标 ``expired``。返回是否刚刚过期。

    到期判断必须落在使用会话的每一处（读、写片、合并、取消）。只靠建新会话时
    顺手清理是不够的——没有新上传时，旧会话会一直可写。
    """
    if not session.is_open or session.expires_at > utcnow():
        return False
    _mark_expired(storage, session)
    return True


def ensure_writable(db: DbSession, *, storage: LocalStorage, session: UploadSession) -> None:
    """写入或合并前的状态检查：已结束、已到期都不许继续。"""
    if reconcile_expiry(db, storage=storage, session=session):
        # 调用方随后要抛 409，请求事务会回滚；先把「已过期」提交，否则下次仍然可写。
        db.commit()
        raise ApiError.conflict("上传会话已过期，请重新创建")
    if session.status == UPLOAD_EXPIRED:
        raise ApiError.conflict("上传会话已过期，请重新创建")
    if not session.is_open:
        raise ApiError.conflict(f"上传会话当前状态为 {session.status}，不能继续写入")


def _mark_expired(storage: LocalStorage, session: UploadSession) -> None:
    storage.discard_session(session.id)
    session.status = UPLOAD_EXPIRED
    session.error = f"超过 {SESSION_TTL_HOURS} 小时未完成"


def _contiguous_parts(session: UploadSession) -> list[int]:
    """分片必须从 1 开始连续。缺片直接拒绝，不做「先合并再说」。"""
    indices = sorted(set(session.parts))
    if not indices:
        raise ApiError.invalid("还没有收到任何分片")
    expected = list(range(1, indices[-1] + 1))
    missing = sorted(set(expected) - set(indices))
    if missing:
        raise ApiError.conflict(f"缺少分片：{', '.join(str(item) for item in missing)}")
    return indices


def _fail(db: DbSession, storage: LocalStorage, session: UploadSession, *, message: str) -> None:
    """记录失败并清掉临时文件。

    这里必须自己提交：调用方随后要抛错，请求事务会回滚；不先落库的话，
    会话会永远停在 ``merging``，谁也看不出这次上传已经失败。
    """
    storage.discard_session(session.id)
    session.status = UPLOAD_FAILED
    session.error = message[:255]
    audit.record(
        db,
        action=audit.UPLOAD_FAILED,
        actor_type=audit.ACTOR_USER,
        actor_id=session.actor_id,
        target_type=audit.TARGET_UPLOAD,
        target_id=session.id,
        detail={"reason": message[:200]},
    )
    db.commit()
