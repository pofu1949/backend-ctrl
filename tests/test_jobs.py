"""后台任务队列（规划第 7 部分）。

覆盖：领取的原子语义、租约过期后重新可领、3 次上限、周期任务去重、
失败/完成的状态落点。这些规则决定「worker 被杀之后会怎样」，
所以都用数据库状态断言，不依赖进程内的计数器。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ctrlstore.db import create_engine
from ctrlstore.jobs import service as jobs
from ctrlstore.models import JOB_DONE, JOB_FAILED, JOB_PENDING, JOB_RUNNING, Job, utcnow


@pytest.fixture
def engine(settings, database):
    engine = create_engine(settings)
    yield engine
    engine.dispose()


def _enqueue(db: DbSession, kind: str = jobs.KIND_THUMB) -> Job:
    job = jobs.enqueue(db, kind, {"derivative_id": "x"})
    db.commit()
    return job


def _of_kind(db: DbSession, kind: str) -> list[Job]:
    return list(db.scalars(select(Job).where(Job.kind == kind)).all())


def test_claim_marks_running_with_lease(db: DbSession, engine) -> None:
    job = _enqueue(db)

    claimed = jobs.claim(engine)

    assert claimed == job.id
    db.expire_all()
    stored = db.get(Job, job.id)
    assert stored is not None
    assert stored.status == JOB_RUNNING
    assert stored.attempts == 1
    assert stored.lease_until is not None and stored.lease_until > utcnow()


def test_claim_returns_none_when_queue_is_empty(engine) -> None:
    assert jobs.claim(engine) is None


def test_claim_excludes_kinds_without_changing_fifo(db: DbSession, engine) -> None:
    decode = _enqueue(db)
    decode.attempts = 2
    decode.last_error = "上次执行失败"
    original_times = (decode.created_at, decode.updated_at)
    first = _enqueue(db, jobs.KIND_PUBLISH_DUE)
    second = _enqueue(db, jobs.KIND_CLEANUP)
    # 同一时间入队时仍按 ID 排；过滤不能改变原有 FIFO 规则。
    second.created_at = first.created_at
    db.commit()

    for expected in sorted((first.id, second.id)):
        assert jobs.claim(engine, excluded_kinds=(jobs.KIND_THUMB,)) == expected
    assert jobs.claim(engine, excluded_kinds=(jobs.KIND_THUMB,)) is None

    db.expire_all()
    assert decode.status == JOB_PENDING
    assert decode.attempts == 2
    assert decode.last_error == "上次执行失败"
    assert decode.lease_until is None
    assert (decode.created_at, decode.updated_at) == original_times
    assert jobs.claim(engine) == decode.id
    db.expire_all()
    assert decode.attempts == 3


@pytest.mark.parametrize("attempts", [1, jobs.MAX_ATTEMPTS])
def test_excluded_kind_still_reclaims_expired_lease(db: DbSession, engine, attempts) -> None:
    """低内存只阻止新执行，不屏蔽已中断任务的租约回收和重试上限。"""
    decode = _enqueue(db)
    decode.status = JOB_RUNNING
    decode.attempts = attempts
    decode.lease_until = utcnow() - timedelta(seconds=1)
    due = _enqueue(db, jobs.KIND_PUBLISH_DUE)

    assert jobs.claim(engine, excluded_kinds=(jobs.KIND_THUMB,)) == due.id

    db.expire_all()
    assert decode.status == (JOB_FAILED if attempts == jobs.MAX_ATTEMPTS else JOB_PENDING)
    assert decode.attempts == attempts
    assert decode.lease_until is None


def test_expired_lease_is_claimable_again(db: DbSession, engine) -> None:
    """worker 中途被杀：租约到期后任务回到可领，次数继续累加。"""
    job = _enqueue(db)
    jobs.claim(engine)
    db.expire_all()
    stored = db.get(Job, job.id)
    stored.lease_until = utcnow() - timedelta(seconds=1)
    db.commit()

    claimed = jobs.claim(engine)

    assert claimed == job.id
    db.expire_all()
    assert db.get(Job, job.id).attempts == 2


def test_lease_expiry_stops_at_three_attempts(db: DbSession, engine) -> None:
    """领满 3 次仍没做完就停在 failed，不无限重启。"""
    job = _enqueue(db)
    stored = db.get(Job, job.id)
    stored.status = JOB_RUNNING
    stored.attempts = 3
    stored.lease_until = utcnow() - timedelta(seconds=1)
    db.commit()

    assert jobs.claim(engine) is None
    db.expire_all()
    stored = db.get(Job, job.id)
    assert stored.status == JOB_FAILED
    assert "租约" in (stored.last_error or "")


def test_fail_retries_until_limit_then_stops(db: DbSession, engine) -> None:
    job = _enqueue(db)

    statuses = []
    for _ in range(3):
        jobs.claim(engine)
        db.expire_all()
        statuses.append(jobs.fail(db, job.id, "生成失败"))

    assert statuses == [JOB_PENDING, JOB_PENDING, JOB_FAILED]
    db.expire_all()
    stored = db.get(Job, job.id)
    assert stored.status == JOB_FAILED
    assert stored.attempts == 3
    assert stored.last_error == "生成失败"


def test_complete_clears_lease_and_error(db: DbSession, engine) -> None:
    job = _enqueue(db)
    jobs.claim(engine)
    db.expire_all()

    jobs.complete(db, job.id)

    db.expire_all()
    stored = db.get(Job, job.id)
    assert stored.status == JOB_DONE
    assert stored.lease_until is None
    assert stored.last_error is None


def test_periodic_does_not_stack(db: DbSession) -> None:
    """刚排过一个周期任务，不会因为 worker 空转又来一个。"""
    first = jobs.enqueue_periodic(db, jobs.KIND_PUBLISH_DUE, every=jobs.PUBLISH_INTERVAL)
    second = jobs.enqueue_periodic(db, jobs.KIND_PUBLISH_DUE, every=jobs.PUBLISH_INTERVAL)

    assert first is not None
    assert second is None
    assert len(_of_kind(db, jobs.KIND_PUBLISH_DUE)) == 1


def test_periodic_skips_while_one_is_pending(db: DbSession) -> None:
    jobs.enqueue_periodic(db, jobs.KIND_PUBLISH_DUE, every=jobs.PUBLISH_INTERVAL)
    # 把上一条挪到很久以前：仍因「还有待处理」而不再排新的。
    job = _of_kind(db, jobs.KIND_PUBLISH_DUE)[0]
    job.created_at = utcnow() - timedelta(days=1)
    db.commit()

    assert jobs.enqueue_periodic(db, jobs.KIND_PUBLISH_DUE, every=jobs.PUBLISH_INTERVAL) is None


def test_list_jobs_filters_by_status(db: DbSession) -> None:
    done = _enqueue(db)
    done.status = JOB_DONE
    pending = _enqueue(db, jobs.KIND_CLEANUP)
    db.commit()

    assert [job.id for job in jobs.list_jobs(db, status=JOB_DONE)] == [done.id]
    assert {job.id for job in jobs.list_jobs(db)} == {done.id, pending.id}
