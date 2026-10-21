"""后台任务队列（企划第 10.3 节）。

只有一个 worker 进程、同时只做一件事。本模块只管「排队、领取、记账」，
不关心任务做什么——处理器在 :mod:`ctrlstore.jobs.handlers`。

三条规则写在这里，也由这一处保证：

1. **领取是原子的。** 取出 ``pending`` 并改成 ``running`` 在同一个
   ``BEGIN IMMEDIATE`` 里完成；即使误起了两个 worker，也只有一个能领到同一行。
2. **租约 15 分钟，最多 3 次。** worker 中途被杀时，租约过期后任务重新可领；
   领满 3 次仍失败就停在 ``failed``，等所有者查看，不无限重启。
3. **失败不掩盖结果。** 任务失败只写 ``jobs.last_error``；缩略图、发布这些
   业务结果已经按各自的规则提交，重试不会把它们改回去。
"""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, case, delete, func, or_, select, update
from sqlalchemy.orm import Session as DbSession

from ..models import (
    JOB_DONE,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
    Job,
    utcnow,
)

#: 任务类型。新增解码类型时也要加入 MEMORY_REQUIRED_KINDS，并在 handlers 注册。
KIND_THUMB = "image.thumb"
#: 展示图：1600 像素的可选预览，与缩略图一起入队，不阻塞原图发布。
KIND_DISPLAY = "image.display"
#: 文本抽取与表格预览（第 9 部分）。
KIND_EXTRACT = "content.extract"
#: 视频探测与封面（第 9 部分）。
KIND_VIDEO = "video.inspect"
#: 每分钟一次：把到点的定时发布送出去。
KIND_PUBLISH_DUE = "publish.due"
#: 每小时一次：过期上传、回收站到期、旧任务记录。
KIND_CLEANUP = "maintenance.cleanup"

#: 领取前筛选与执行前复查共用此分类；发布与清理不受解码内存阈值限制。
MEMORY_REQUIRED_KINDS = frozenset({KIND_THUMB, KIND_DISPLAY, KIND_EXTRACT, KIND_VIDEO})

#: 租约时长与重试上限（企划第 10.3 节）。
LEASE_MINUTES = 15
MAX_ATTEMPTS = 3

#: 周期任务的补队间隔。定时发布要按分钟到点，清理按小时足够。
PUBLISH_INTERVAL = timedelta(minutes=1)
CLEANUP_INTERVAL = timedelta(hours=1)

#: 已完成任务的保留天数。失败任务不删：它们要留着给所有者看。
FINISHED_RETENTION_DAYS = 30


def enqueue(db: DbSession, kind: str, payload: dict[str, Any] | None = None) -> Job:
    """入队。与应用数据在同一事务里提交：业务成功而任务丢了，比反过来更难查。"""
    job = Job(
        kind=kind,
        payload=json.dumps(payload or {}, ensure_ascii=False),
        status=JOB_PENDING,
        attempts=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(job)
    db.flush()
    return job


def enqueue_periodic(
    db: DbSession, kind: str, *, every: timedelta, payload: dict[str, Any] | None = None
) -> Job | None:
    """周期任务：同类既没有待处理/处理中的，上次创建也已超过间隔，才补一个。

    没有这一步，worker 每次空转都会排一个定时发布，队列很快被自己淹没。
    """
    now = utcnow()
    recent = db.scalar(
        select(Job.id)
        .where(
            Job.kind == kind,
            or_(
                Job.status.in_((JOB_PENDING, JOB_RUNNING)),
                Job.created_at > now - every,
            ),
        )
        .limit(1)
    )
    if recent is not None:
        return None
    return enqueue(db, kind, payload)


def claim(engine: Engine, *, excluded_kinds: Collection[str] = ()) -> str | None:
    """领取一个任务，返回任务 ID；没有可领的返回 ``None``。

    顺序是「回收过期租约 → 取出最早的可执行任务 → 标记为处理中」，全在一个
    写事务里。用 ``BEGIN IMMEDIATE`` 而不是默认的延迟事务，是为了在这一步
    就拿到写锁：否则两个进程可以同时读到同一行 ``pending``。

    ``excluded_kinds`` 只过滤本次领取，不改待处理任务的顺序、次数或时间。
    资源恢复后省略过滤，即可继续按 ``created_at, id`` 领取；租约回收仍覆盖所有类型。
    """
    candidate = select(Job.id).where(Job.status == JOB_PENDING)
    if excluded_kinds:
        candidate = candidate.where(Job.kind.not_in(excluded_kinds))
    candidate = candidate.order_by(Job.created_at, Job.id).limit(1)

    now = utcnow()
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _reclaim_expired(connection, now=now)
            job_id = connection.execute(candidate).scalar()
            if job_id is None:
                connection.exec_driver_sql("COMMIT")
                return None
            connection.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JOB_PENDING)
                .values(
                    status=JOB_RUNNING,
                    attempts=Job.attempts + 1,
                    lease_until=now + timedelta(minutes=LEASE_MINUTES),
                    updated_at=now,
                )
            )
            connection.exec_driver_sql("COMMIT")
            return str(job_id)
        except Exception:
            connection.exec_driver_sql("ROLLBACK")
            raise


def complete(db: DbSession, job_id: str) -> None:
    """标记完成。调用方负责提交。"""
    job = db.get(Job, job_id)
    if job is None:
        return
    job.status = JOB_DONE
    job.lease_until = None
    job.last_error = None
    job.updated_at = utcnow()
    db.commit()


def fail(db: DbSession, job_id: str, error: BaseException | str) -> str:
    """记录一次失败。次数没用完就退回 ``pending``，用完了停在 ``failed``。

    返回最终状态，方便调用方决定要不要打日志。
    """
    job = db.get(Job, job_id)
    if job is None:
        return JOB_FAILED
    job.last_error = str(error)[:2000]
    job.lease_until = None
    job.updated_at = utcnow()
    job.status = JOB_FAILED if job.attempts >= MAX_ATTEMPTS else JOB_PENDING
    db.commit()
    return job.status


def defer(db: DbSession, job_id: str) -> None:
    """把任务退回待处理，且不消耗尝试次数。

    用于领取后资源才变得不足的情况：解码任务不开始执行，也不记成失败。
    worker 随后排除解码类型，继续尝试发布和清理，不让这条任务堵住队首。
    """
    job = db.get(Job, job_id)
    if job is None:
        return
    job.status = JOB_PENDING
    job.lease_until = None
    # 领取时加过 1；这里退回来，保证重试次数只统计真正执行过的尝试。
    job.attempts = max(job.attempts - 1, 0)
    job.updated_at = utcnow()
    db.commit()


def list_jobs(db: DbSession, *, status: str | None = None, limit: int = 50) -> list[Job]:
    """按创建时间倒序列出任务，供 ``ctrlstore jobs list`` 与后续管理端使用。"""
    statement = select(Job).order_by(Job.created_at.desc(), Job.id)
    if status:
        statement = statement.where(Job.status == status)
    return list(db.scalars(statement.limit(max(1, min(limit, 200)))).all())


def counts(db: DbSession) -> dict[str, int]:
    """按状态统计队列。所有者健康接口用它看有没有卡住的任务。"""
    result = {JOB_PENDING: 0, JOB_RUNNING: 0, JOB_DONE: 0, JOB_FAILED: 0}
    rows = db.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    result.update({status: count for status, count in rows})
    return result


def purge_finished(db: DbSession, *, days: int = FINISHED_RETENTION_DAYS) -> int:
    """删掉过期的已完成任务。失败任务留着，它们是排错的线索。"""
    result = db.execute(
        delete(Job).where(
            Job.status == JOB_DONE,
            Job.updated_at < utcnow() - timedelta(days=days),
        )
    )
    return result.rowcount or 0


def _reclaim_expired(connection: Connection, *, now: datetime) -> None:
    """把租约过期的 ``running`` 收回来。

    一条语句处理两种情况：还有重试次数的退回 ``pending``；已经领满 3 次的
    直接停成 ``failed``，并留下说明——worker 反复被杀时，队列不会无限转。
    """
    exhausted = Job.attempts >= MAX_ATTEMPTS
    connection.execute(
        update(Job)
        .where(Job.status == JOB_RUNNING, Job.lease_until.is_not(None), Job.lease_until < now)
        .values(
            status=case((exhausted, JOB_FAILED), else_=JOB_PENDING),
            lease_until=None,
            last_error=case(
                (
                    exhausted,
                    f"租约超过 {LEASE_MINUTES} 分钟未续，已领满 {MAX_ATTEMPTS} 次",
                ),
                else_=Job.last_error,
            ),
            updated_at=now,
        )
    )
