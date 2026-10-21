"""所有者健康接口。

匿名 ``/healthz`` 仍然只返回 ``status``；这里多给磁盘、队列、迁移版本与上次
成功备份，供部署后核对与排错（企划第 9 部分）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session as DbSession

from ..auth import deps
from ..health import admin_report

router = APIRouter(tags=["health"])


@router.get("/health")
def read_health(
    request: Request,
    _: deps.Principal = Depends(deps.owner),
    db: DbSession = Depends(deps.get_db),
) -> dict:
    return admin_report(db, request.app.state.settings)
