"""任务处理器：任务类型 → 一段业务动作。

每个处理器自己提交事务，worker 只负责「领到、调用、标记完成/失败」。
这样做的好处是：发布、缩略图、清理各自都已经是完整业务动作，重试一次不会
读到半截状态；失败时也只写 ``jobs.last_error``，不改动已经提交的业务结果。

本部分有六类任务：

| 类型 | 做什么 |
| --- | --- |
| ``image.thumb`` | 生成 480 像素 WebP 缩略图，就绪后按发布状态补进公开区 |
| ``image.display`` | 1600 像素展示图，代码同上一行，默认不入队（企划第 8.5 节 D2） |
| ``content.extract`` | 文档/文本抽取、xlsx 预览、PDF 首页图 |
| ``video.inspect`` | ffprobe 判断能否播放，ffmpeg 截封面 |
| ``publish.due`` | 把到点的定时发布送出去 |
| ``maintenance.cleanup`` | 过期上传、回收站到期、访问明细、旧任务记录 |

抽取与探测定性失败（损坏、超限、加密）只记录状态并结束任务，不重试：
重试解决不了这些问题，还会堵住后面的任务。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..assets import derivatives, publish
from ..config import Settings
from ..content import extract as content_extract
from ..content import pdf_text, video
from ..content.limits import ExtractionFailed, ExtractionSkipped
from ..errors import ApiError
from ..imaging import render_webp
from ..models import (
    ASSET_TRASHED,
    DERIVATIVE_FAILED,
    DERIVATIVE_PENDING,
    DERIVATIVE_READY,
    EXTRACT_FAILED,
    EXTRACT_READY,
    EXTRACT_SKIPPED,
    PURPOSE_DISPLAY,
    PURPOSE_POSTER,
    PURPOSE_TABLE_PREVIEW,
    PURPOSE_THUMB,
    Asset,
    AssetVersion,
    Derivative,
    utcnow,
)
from ..search import service as search
from ..stats import pageviews
from ..storage import LocalStorage
from ..uploads import service as uploads
from ..uploads.inspection import KIND_PDF
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
    target = storage.derivative_path(derivative.storage_key)
    try:
        render_webp(
            source,
            target,
            max_side=_max_side(settings, derivative.purpose),
        )
    except Exception as exc:  # 解码失败、内存不足、磁盘错误都在这里
        _mark(db, derivative, DERIVATIVE_FAILED)
        raise JobError(f"{derivative.purpose} 生成失败：{exc}") from exc

    derivative.byte_size = target.stat().st_size
    _mark(db, derivative, DERIVATIVE_READY)
    # 只有「现在就应该在公开区」的那一版才补链接；失败留给 fsck，不影响图片可用。
    publish.link_derivative(db=db, storage=storage, derivative=derivative)


# —— 文本抽取、表格预览与 PDF 首页图 ——


def _content_extract(*, settings: Settings, storage: LocalStorage, db: DbSession, payload) -> None:
    """抽取一个版本的文本；xlsx 另存预览 JSON，PDF 另存首页图。"""
    version = _version(db, payload)
    if version is None:
        return
    asset = db.get(Asset, version.asset_id)
    if asset is None or asset.status == ASSET_TRASHED:
        return
    source = storage.original_path(version.storage_key)
    if not source.is_file():
        raise JobError("原件不存在，无法抽取文本")

    result = None
    try:
        result = content_extract.run(source, kind=asset.kind)
    except ExtractionSkipped as exc:
        _mark_extract(db, asset=asset, version=version, status=EXTRACT_SKIPPED, reason=str(exc))
    except ExtractionFailed as exc:
        _mark_extract(db, asset=asset, version=version, status=EXTRACT_FAILED, reason=str(exc))

    if result is not None:
        version.text_extract = result.text
        version.text_truncated = result.truncated
        if result.page_count is not None:
            version.page_count = result.page_count
        version.extract_status = EXTRACT_READY if result.text else EXTRACT_SKIPPED
        if result.table is not None:
            _store_preview(db, storage=storage, asset=asset, version=version, table=result.table)
        db.commit()
        search.refresh(db, asset)
        db.commit()

    # 文本抽取失败不妨碍给 PDF 做首页图：扫描版 PDF 正是没有文本的那一类。
    if asset.kind == KIND_PDF:
        _store_poster(
            db,
            storage=storage,
            asset=asset,
            version=version,
            renderer=pdf_text.render_first_page,
        )


def _mark_extract(
    db: DbSession,
    *,
    asset: Asset,
    version: AssetVersion,
    status: str,
    reason: str,
) -> None:
    """记下跳过或失败。保留原件，供管理端显示抽取状态。"""
    version.text_extract = None
    version.text_truncated = False
    version.extract_status = status
    db.commit()
    search.refresh(db, asset)
    db.commit()
    logger.info(
        "文本抽取未完成",
        extra={"version_id": version.id, "kind": asset.kind, "status": status, "reason": reason},
    )


def _store_preview(
    db: DbSession,
    *,
    storage: LocalStorage,
    asset: Asset,
    version: AssetVersion,
    table: dict,
) -> None:
    """把 xlsx 预览写成派生 JSON。预览不是公开文件，只在表格接口里读。"""
    data = json.dumps(table, ensure_ascii=False).encode("utf-8")
    derivative = derivatives.ensure(
        db,
        asset=asset,
        version=version,
        purpose=PURPOSE_TABLE_PREVIEW,
        mime="application/json",
    )
    path = storage.write_derivative(data, derivative.storage_key)
    derivative.byte_size = path.stat().st_size
    derivative.status = DERIVATIVE_READY
    derivative.updated_at = utcnow()


# —— 视频 ——


def _video_inspect(*, settings: Settings, storage: LocalStorage, db: DbSession, payload) -> None:
    """探测编码并尝试截封面。没有 ffprobe/ffmpeg 就跳过，不影响播放原片。"""
    version = _version(db, payload)
    if version is None:
        return
    asset = db.get(Asset, version.asset_id)
    if asset is None or asset.status == ASSET_TRASHED:
        return
    source = storage.original_path(version.storage_key)
    if not source.is_file():
        raise JobError("原件不存在，无法探测视频")

    try:
        probe = video.probe(source, container=Path(version.storage_key).suffix)
    except ExtractionFailed as exc:
        version.playback = None
        version.extract_status = EXTRACT_FAILED
        db.commit()
        logger.warning("视频探测失败", extra={"version_id": version.id, "error": str(exc)})
        return

    if probe is None:
        version.extract_status = EXTRACT_SKIPPED
        db.commit()
        return

    version.playback = probe.playback
    version.width = probe.width or version.width
    version.height = probe.height or version.height
    version.duration_ms = probe.duration_ms
    version.extract_status = EXTRACT_READY
    db.commit()

    _store_poster(
        db,
        storage=storage,
        asset=asset,
        version=version,
        renderer=video.render_poster,
    )


def _store_poster(
    db: DbSession,
    *,
    storage: LocalStorage,
    asset: Asset,
    version: AssetVersion,
    renderer,
) -> None:
    """生成封面（视频第一帧或 PDF 首页）。工具缺失时不留记录。"""
    derivative = derivatives.ensure(
        db,
        asset=asset,
        version=version,
        purpose=PURPOSE_POSTER,
        mime="image/jpeg",
    )
    if derivative.is_ready:
        return
    derivative.status = DERIVATIVE_PENDING
    db.commit()

    source = storage.original_path(version.storage_key)
    target = storage.derivative_path(derivative.storage_key)
    try:
        produced = renderer(source, target)
    except Exception as exc:  # 工具自己报错：记失败，不重试
        _fail_derivative(db, derivative, f"封面生成失败：{exc}")
        return

    if not produced:
        # 系统没有对应工具：删掉占位记录，公开 JSON 里自然没有 poster。
        db.delete(derivative)
        db.commit()
        return

    derivative.status = DERIVATIVE_READY
    derivative.byte_size = target.stat().st_size
    derivative.updated_at = utcnow()
    db.commit()
    publish.link_derivative(db=db, storage=storage, derivative=derivative)


def _fail_derivative(db: DbSession, derivative: Derivative, reason: str) -> None:
    derivative.status = DERIVATIVE_FAILED
    derivative.byte_size = 0
    derivative.updated_at = utcnow()
    db.commit()
    logger.warning("派生文件生成失败", extra={"derivative_id": derivative.id, "reason": reason})


def _version(db: DbSession, payload) -> AssetVersion | None:
    version_id = str(payload.get("version_id", ""))
    if not version_id:
        return None
    return db.get(AssetVersion, version_id)


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
    jobs.KIND_EXTRACT: _content_extract,
    jobs.KIND_VIDEO: _video_inspect,
    jobs.KIND_PUBLISH_DUE: _publish_due,
    jobs.KIND_CLEANUP: _cleanup,
}
