"""低内存队列调度：不反复领取解码任务，保留执行前复查，恢复后继续。"""

from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest
from sqlalchemy import select

from ctrlstore import worker
from ctrlstore.jobs import service as jobs
from ctrlstore.models import JOB_DONE, JOB_PENDING, Job

DECODE_KINDS = (jobs.KIND_THUMB, jobs.KIND_DISPLAY, jobs.KIND_EXTRACT, jobs.KIND_VIDEO)


def test_decode_only_queue_sleeps_without_reclaiming_and_recovers(
    db, settings, monkeypatch
) -> None:
    """连续低内存轮询不改任务记录；同一 worker 在内存恢复后自动执行。"""
    memory = SimpleNamespace(available=settings.min_available_memory_bytes - 1)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: memory)
    # 周期任务刚做过，只留下解码任务；生产循环此时必须真正休眠。
    for kind in (jobs.KIND_PUBLISH_DUE, jobs.KIND_CLEANUP):
        jobs.enqueue(db, kind).status = JOB_DONE
    decodes = [jobs.enqueue(db, kind) for kind in DECODE_KINDS]
    db.commit()
    timestamps = {job.id: (job.created_at, job.updated_at) for job in decodes}
    sleeps = []

    class StopWorker(Exception):
        pass

    def sleep(seconds):
        sleeps.append(seconds)
        db.expire_all()
        if len(sleeps) <= 3:
            for job in decodes:
                assert job.status == JOB_PENDING
                assert job.attempts == 0
                assert job.lease_until is None
                assert job.last_error is None
                assert (job.created_at, job.updated_at) == timestamps[job.id]
            if len(sleeps) == 3:
                memory.available = settings.min_available_memory_bytes
        else:
            # 空 payload 代表版本/派生已不存在，恢复后按现有规则正常结束任务。
            assert all(job.status == JOB_DONE and job.attempts == 1 for job in decodes)
            raise StopWorker

    monkeypatch.setattr(worker.time, "sleep", sleep)
    with pytest.raises(StopWorker):
        worker.run_forever(settings)
    assert sleeps == [worker.POLL_SECONDS] * 4
    assert len(list(db.scalars(select(Job)))) == len(DECODE_KINDS) + 2


@pytest.mark.parametrize("kind", DECODE_KINDS)
def test_memory_drop_after_claim_still_runs_next_eligible_job(
    db, settings, monkeypatch, kind
) -> None:
    """领取时内存充足，处理器执行前才不足：还原尝试次数，同轮让周期任务前进。"""
    decode = jobs.enqueue(db, kind)
    due = jobs.enqueue(db, jobs.KIND_PUBLISH_DUE)
    db.commit()
    checks = 0

    def virtual_memory():
        nonlocal checks
        checks += 1
        return SimpleNamespace(
            available=settings.min_available_memory_bytes if checks == 1 else 0
        )

    monkeypatch.setattr(psutil, "virtual_memory", virtual_memory)

    assert worker.run_until_idle(settings, max_jobs=1) == 1

    db.expire_all()
    assert checks >= 2  # 确认执行前的内存复查确实发生。
    assert decode.status == JOB_PENDING
    assert decode.attempts == 0
    assert decode.lease_until is None
    assert decode.last_error is None
    assert due.status == JOB_DONE
