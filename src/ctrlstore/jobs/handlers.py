"""任务处理器：任务类型 → 一段业务动作。

每个处理器自己提交事务，worker 只负责「领到、调用、标记完成/失败」。
这样做的好处是：发布、缩略图、清理各自都已经是完整业务动作，重试一次不会
读到半截状态；失败时也只写 ``jobs.last_error``，不改动已经提交的业务结果。

本部分只有四类任务：

| 类型 | 做什么 |
| --- | --- |
| ``image.thumb`` | 生成 480 像素 WebP 缩略图，就绪后按发布状态补进公开区 |
| ``image.display`` | 1600 像素展示图，代码同上一行，默认不入队（企划第 8.5 节 D2） |
| ``publish.due`` | 把到点的定时发布送出去 |
| ``maintenance.cleanup`` | 过期上传、回收站到期清理、旧任务记录 |
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..assets import publish
from ..config import Settings
from ..errors import ApiError
from ..imaging import render_webp
from ..models import (
    ASSET_TRASHED,
    DERIVATIVE_FAILED,
    DERIVATIVE_PENDING,
    DERIVATIVE_READY,
    PURPOSE_DISPLAY,
    PURPOSE_THUMB,
    Asset,
    Derivative,
    utcnow,
)
from ..stats import pageviews
from ..storage import LocalStorage
from ..uploads import service as uploads
from . import service as jobs

logger = logging.getLogger(__name__)


class JobError(RuntimeError):
    """任务无法完成。消息会写进 ``jobs.last_error``，供 ``ctrlstore jobs list`` 查看。"""


def run(*, settings: Settings, storage: LocalStorage, db: DbSession, job) -> None:
    """执行一个任务。未知类型直接失败，不静默略过。"""
    handler = _HANDLERS.get(job.kind)
    if handler is None:
        raise JobError(f"未知任务类型：{job.kind}")
    handler(settings=settings, storage=storage, db=db, payload=job.data)


# —— 图片派生 ——


def _image_derivative(*, settings: Settings, storage: LocalStorage, db: DbSession, payload) -> None:
    """生成一份图片派生文件，成功后就绪并尝试补进公开区。"""
    derivative = db.get(Derivative, str(payload.get("derivative_id", "")))
    if derivative is None:
        # 版本已被保留规则淘汰，任务自然作废；这不是错误。
        return
    if derivative.is_ready:
        return

    version = derivative.version
    if version is None:  # 版本行已被删除：没有源文件可做，任务作废
        return
    source = storage.original_path(version.storage_key)
    if not source.is_file():
        raise JobError("原件不存在，无法生成派生文件")

    _mark(db, derivative, DERIVATIVE_PENDING)
    try:
        render_webp(
            source,
            storage.derivative_path(derivative.storage_key),
            max_side=_max_side(settings, derivative.purpose),
        )
    except Exception as exc:  # 解码失败、内存不足、磁盘错误都在这里
        _mark(db, derivative, DERIVATIVE_FAILED)
        raise JobError(f"{derivative.purpose} 生成失败：{exc}") from exc

    _mark(db, derivative, DERIVATIVE_READY)
    # 只有「现在就应该在公开区」的那一版才补链接；失败留给 fsck，不影响图片可用。
    publish.link_derivative(db=db, storage=storage, derivative=derivative)


# —— 定时发布 ——


def _publish_due(*, settings: Settings, storage: LocalStorage, db: DbSession, payload) -> None:
    published, failed = publish.publish_due(db, storage=storage)
    if failed:
        # 失败的内容已经取消定时并写审计；这里只留一条便于翻日志。
        logger.warning("定时发布有内容未能上线", extra={"published": published, "failed": failed})


# —— 清理 ——


def _cleanup(*, settings: Settings, storage: LocalStorage, db: DbSession, payload) -> None:
    """每小时一次：过期上传、回收站到期、访问明细、旧任务记录。

    清理只删「按已提交状态可以删」的东西：回收站内容仍被封面或集合引用时跳过，
    等所有者先解除引用，不在这里替人做决定。访问明细超过 400 天就删，
    日汇总不动。
    """
    uploads.expire_stale(db, storage=storage)
    pageviews.purge_old(db)
    db.commit()

    _purge_expired_trash(settings=settings, storage=storage, db=db)
    jobs.purge_finished(db)
    db.commit()


def _purge_expired_trash(*, settings: Settings, storage: LocalStorage, db: DbSession) -> int:
    """回收站超过保留天数的内容彻底删除。返回删掉的条数。"""
    cutoff = utcnow() - timedelta(days=settings.trash_retention_days)
    rows = db.scalars(
        select(Asset).where(Asset.status == ASSET_TRASHED, Asset.trashed_at <= cutoff)
    ).all()

    removed = 0
    for asset in rows:
        asset_id = asset.id
        try:
            publish.purge(db, storage=storage, asset=asset)
            db.commit()
            removed += 1
        except ApiError as exc:
            # 仍被引用：跳过并保留，下一次清理还会再试。
            db.rollback()
            logger.info(
                "回收站内容仍被引用，暂不彻底删除",
                extra={"asset_id": asset_id, "reason": exc.message},
            )
    return removed


# —— 内部 ——


def _mark(db: DbSession, derivative: Derivative, status: str) -> None:
    """写下派生状态并提交，让失败与就绪都能被立刻看到。"""
    derivative.status = status
    derivative.updated_at = utcnow()
    db.commit()


def _max_side(settings: Settings, purpose: str) -> int:
    if purpose == PURPOSE_THUMB:
        return settings.thumb_max_side
    if purpose == PURPOSE_DISPLAY:
        return settings.display_max_side
    raise JobError(f"图片派生不支持这个用途：{purpose}")


_HANDLERS = {
    jobs.KIND_THUMB: _image_derivative,
    jobs.KIND_DISPLAY: _image_derivative,
    jobs.KIND_PUBLISH_DUE: _publish_due,
    jobs.KIND_CLEANUP: _cleanup,
}
