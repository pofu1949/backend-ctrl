"""主面板 ``GET /api/admin/v1/dashboard``（企划第 8.9 节）。

只对所有者开放：编辑拿不到访问数据，也不该看到全库占用。
数据来自 ``stats.dashboard``，这里只负责取参数、校验与返回。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session as DbSession

from ..auth import deps
from ..stats import dashboard

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def read_dashboard(
    request: Request,
    month: str | None = Query(default=None, max_length=7, description="YYYY-MM，缺省本月"),
    _: deps.Principal = Depends(deps.owner),
    db: DbSession = Depends(deps.get_db),
) -> dict:
    """今日人数与次数、月份每日人数、近 12 个月相加、七类库存与磁盘剩余。"""
    return dashboard.build(db, settings=request.app.state.settings, month=month)


@router.get("/dashboard/day")
def read_dashboard_day(
    date: str = Query(description="YYYY-MM-DD", max_length=10),
    _: deps.Principal = Depends(deps.owner),
    db: DbSession = Depends(deps.get_db),
) -> dict:
    """某一天的次数与最多 10 条路径。点开日历时用。"""
    return dashboard.day_detail(db, day=date)
