"""后台 worker（企划第 17.1 节）。

一个进程、同时只做一件事：生成缩略图、执行定时发布、清理过期数据。
部署上与 API 分成两个 systemd 单元，缩略图卡住时不会挡住登录和发布。

循环本身很短：补周期任务 → 领取一个任务 → 执行，队列空时睡一会。
``run_until_idle`` 把当前能做完的任务做完即退出，供测试与手动排障使用。
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from .config import Settings
from .db import create_engine
from .jobs import handlers
from .jobs import service as jobs
from .models import Job
from .storage import LocalStorage

logger = logging.getLogger(__name__)

#: 空转休眠秒数。定时发布是分钟级，2 秒足够及时，也不会空转烧 CPU。
POLL_SECONDS = 2.0

#: 一次 ``run_until_idle`` 最多处理的任务数，避免异常数据把循环变成死循环。
MAX_JOBS = 1000


def run_forever(settings: Settings, *, poll_seconds: float = POLL_SECONDS) -> None:
    """生产入口：一直处理任务，队列空时休眠。"""
    engine, factory = _open(settings)
    try:
        while True:
            if not process_next(settings, engine=engine, session_factory=factory):
                time.sleep(poll_seconds)
    finally:
        engine.dispose()


def run_until_idle(settings: Settings, *, max_jobs: int = MAX_JOBS) -> int:
    """把当前排队的任务做完就返回，处理条数供调用方记录。"""
    engine, factory = _open(settings)
    processed = 0
    try:
        while processed < max_jobs and process_next(
            settings, engine=engine, session_factory=factory
        ):
            processed += 1
    finally:
        engine.dispose()
    return processed


def process_next(settings: Settings, *, engine: Engine, session_factory: sessionmaker) -> bool:
    """处理一个任务。没有可做的返回 ``False``。

    先补周期任务再领取：定时发布与清理本身就是队列里的普通任务，
    这样周期动作与一次性动作共用同一套租约、重试与可观测性。
    """
    _schedule_periodic(session_factory)
    job_id = jobs.claim(engine)
    if job_id is None:
        return False
    _execute(settings, session_factory=session_factory, job_id=job_id)
    return True


def _schedule_periodic(session_factory: sessionmaker) -> None:
    """按间隔补 ``publish.due`` 与 ``maintenance.cleanup``。"""
    with session_factory() as db:
        created = [
            job
            for job in (
                jobs.enqueue_periodic(db, jobs.KIND_PUBLISH_DUE, every=jobs.PUBLISH_INTERVAL),
                jobs.enqueue_periodic(db, jobs.KIND_CLEANUP, every=jobs.CLEANUP_INTERVAL),
            )
            if job is not None
        ]
        if created:
            db.commit()


def _execute(settings: Settings, *, session_factory: sessionmaker, job_id: str) -> None:
    """执行一个已领取的任务，并把结果记回 ``jobs``。

    处理器自己提交业务结果；这里只写「完成」或「失败 + 错误」。
    失败时重试或停在 ``failed`` 由 ``jobs.fail`` 决定。
    """
    storage = LocalStorage.from_settings(settings)
    with session_factory() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        kind = job.kind
        try:
            handlers.run(settings=settings, storage=storage, db=db, job=job)
        except Exception as exc:
            db.rollback()
            status = jobs.fail(db, job_id, exc)
            logger.error(
                "任务失败",
                extra={"job_id": job_id, "kind": kind, "status": status, "error": str(exc)},
            )
        else:
            jobs.complete(db, job_id)


def _open(settings: Settings) -> tuple[Engine, sessionmaker]:
    """worker 自己的引擎。与 API 一样只保留一条连接。"""
    engine = create_engine(settings)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)
