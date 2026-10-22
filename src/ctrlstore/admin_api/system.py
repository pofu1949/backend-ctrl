"""管理端的只读系统视图：后台任务与审计日志。

这两个接口只给所有者。它们不触发 fsck、备份或队列操作——维护动作仍在
服务器上通过命令行执行，避免一个误点就停掉 API 或 worker。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..auth import deps
from ..errors import ApiError
from ..models import JOB_DONE, JOB_FAILED, JOB_PENDING, JOB_RUNNING, AuditLog, Job, User

router = APIRouter(tags=["system"])

JOB_STATUSES = (JOB_PENDING, JOB_RUNNING, JOB_DONE, JOB_FAILED)


class JobOut(BaseModel):
    id: str
    kind: str
    status: str
    attempts: int
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, job: Job) -> JobOut:
        return cls(
            id=job.id,
            kind=job.kind,
            status=job.status,
            attempts=job.attempts,
            last_error=job.last_error,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


class AuditOut(BaseModel):
    id: str
    actor_type: str
    actor_id: str
    actor_name: str
    action: str
    target_type: str | None = None
    target_id: str | None = None
    detail: dict[str, Any] | None = None
    created_at: datetime

    @classmethod
    def of(cls, entry: AuditLog, *, actor_name: str) -> AuditOut:
        detail = None
        if entry.detail:
            try:
                parsed = json.loads(entry.detail)
                detail = parsed if isinstance(parsed, dict) else {"value": parsed}
            except ValueError:
                detail = {"raw": entry.detail}
        return cls(
            id=entry.id,
            actor_type=entry.actor_type,
            actor_id=entry.actor_id,
            actor_name=actor_name,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            detail=detail,
            created_at=entry.created_at,
        )


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=50, ge=1, le=200),
    _: deps.Principal = Depends(deps.owner),
    db: DbSession = Depends(deps.get_db),
) -> list[JobOut]:
    """最近的后台任务，供系统状态页查看失败与积压。"""
    if status is not None and status not in JOB_STATUSES:
        raise ApiError.invalid("未知的任务状态")
    statement = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status is not None:
        statement = statement.where(Job.status == status)
    return [JobOut.of(job) for job in db.scalars(statement).all()]


@router.get("/audit", response_model=list[AuditOut])
def list_audit(
    action: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=300),
    _: deps.Principal = Depends(deps.owner),
    db: DbSession = Depends(deps.get_db),
) -> list[AuditOut]:
    """最近审计记录。``detail`` 已解析成 JSON 对象，供界面展开。"""
    statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        statement = statement.where(AuditLog.action == action)
    rows = list(db.scalars(statement).all())

    user_ids = {
        row.actor_id
        for row in rows
        if row.actor_type == "user" and row.actor_id
    }
    names: dict[str, str] = {}
    if user_ids:
        names = {
            user.id: user.username
            for user in db.scalars(select(User).where(User.id.in_(user_ids))).all()
        }
    result: list[AuditOut] = []
    for row in rows:
        if row.actor_type == "user":
            actor_name = names.get(row.actor_id, "已删除账号")
        elif row.actor_type == "system":
            actor_name = "系统任务"
        elif row.actor_type == "anonymous":
            actor_name = "匿名请求"
        elif row.actor_type == "site":
            actor_name = "站点"
        else:
            actor_name = row.actor_type
        result.append(AuditOut.of(row, actor_name=actor_name))
    return result
