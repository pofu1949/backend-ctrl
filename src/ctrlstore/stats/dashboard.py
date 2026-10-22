"""主面板数据（企划第 8.9 节）。

两块数据来源不同，分开算：

* **访问**来自 ``pageview_daily``（网站上报后才有）。没有上报记录时明确返回
  「尚未收到网站上报」，不把历史填成 0；上报过之后，某天为 0 就是真的没人来。
* **库存**按 ``kind`` 聚合未进回收站的内容，并区分「当前版本字节」与「含历史版本」。

``GET /dashboard`` 与 ``GET /dashboard/day`` 都只给所有者（路由层判断）。
"""

from __future__ import annotations

import calendar
import re
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..config import Settings
from ..errors import ApiError
from ..health import disk_usage
from ..models import (
    ASSET_PUBLISHED,
    ASSET_TRASHED,
    Asset,
    AssetVersion,
    PageviewDaily,
    PageviewPath,
)
from ..uploads.inspection import (
    KIND_DOCX,
    KIND_IMAGE,
    KIND_MARKDOWN,
    KIND_PDF,
    KIND_TEXT,
    KIND_VIDEO,
    KIND_XLSX,
)
from .pageviews import local_day, shift_day

#: 面板上的七类内容，顺序固定（企划第 8.9 节）。
KINDS = (
    KIND_IMAGE,
    KIND_VIDEO,
    KIND_MARKDOWN,
    KIND_PDF,
    KIND_DOCX,
    KIND_XLSX,
    KIND_TEXT,
)

#: 一天最多展示多少条路径。
TOP_PATHS = 10

#: 近 12 个月的窗口长度。
MONTH_WINDOW = 12

_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def build(
    db: DbSession,
    *,
    settings: Settings,
    month: str | None = None,
    today: str | None = None,
) -> dict:
    """主面板 JSON。``month`` 缺省为本月（上海时区）。

    ``today`` 只用于测试；生产按服务器当前时间取上海日期。
    """
    today_day = today or local_day()
    month_value = _validate_month(month) if month else today_day[:7]
    inventory = _inventory(db, settings)

    if not _has_reports(db):
        # 网站还没接上报：数字留空，别让「0」看起来像「今天没人来」。
        return {
            "reported": False,
            "message": "尚未收到网站上报",
            "today": None,
            "month": {"month": month_value, "days": []},
            "months": [],
            "inventory": inventory,
        }

    uv, pv = _day_totals(db, today_day)
    yesterday_day = shift_day(today_day, -1)
    yesterday_uv, yesterday_pv = _day_totals(db, yesterday_day)

    return {
        "reported": True,
        "message": None,
        "today": {
            "day": today_day,
            "uv": uv,
            "pv": pv,
            # 与昨日的差；负数表示比昨天少。
            "uv_delta": uv - yesterday_uv,
            "pv_delta": pv - yesterday_pv,
        },
        "month": {"month": month_value, "days": _month_days(db, month_value)},
        # 每月把当月各日的独立访客相加，不是整月去重后的人数（企划第 8.9 节）。
        "months": _recent_months(db, today_day),
        "inventory": inventory,
    }


def day_detail(db: DbSession, *, day: str) -> dict:
    """某一天的次数与最多 10 条路径。点开日历时用。"""
    value = _validate_day(day)
    uv, pv = _day_totals(db, value)
    rows = db.execute(
        select(PageviewPath.path, PageviewPath.pv)
        .where(PageviewPath.day == value)
        .order_by(PageviewPath.pv.desc(), PageviewPath.path)
        .limit(TOP_PATHS)
    ).all()
    return {
        "day": value,
        "uv": uv,
        "pv": pv,
        "paths": [{"path": path, "pv": int(count)} for path, count in rows],
    }


# —— 访问 ——


def _has_reports(db: DbSession) -> bool:
    return db.scalar(select(PageviewDaily.site_id).limit(1)) is not None


def _day_totals(db: DbSession, day: str) -> tuple[int, int]:
    """一天的人数与次数。多个站点时按站点相加（第一期只有一个站点）。"""
    row = db.execute(
        select(
            func.coalesce(func.sum(PageviewDaily.uv), 0),
            func.coalesce(func.sum(PageviewDaily.pv), 0),
        ).where(PageviewDaily.day == day)
    ).one()
    return int(row[0]), int(row[1])


def _month_days(db: DbSession, month: str) -> list[dict]:
    """整月的每日人数；没有记录的日子填 0（已确认网站正在上报）。"""
    first = date.fromisoformat(f"{month}-01")
    length = calendar.monthrange(first.year, first.month)[1]
    rows = db.execute(
        select(
            PageviewDaily.day,
            func.sum(PageviewDaily.uv),
            func.sum(PageviewDaily.pv),
        )
        .where(PageviewDaily.day.like(f"{month}-%"))
        .group_by(PageviewDaily.day)
    ).all()
    totals = {row[0]: (int(row[1]), int(row[2])) for row in rows}

    days: list[dict] = []
    for number in range(1, length + 1):
        day = f"{month}-{number:02d}"
        uv, pv = totals.get(day, (0, 0))
        days.append({"day": day, "uv": uv, "pv": pv})
    return days


def _recent_months(db: DbSession, today: str) -> list[dict]:
    """近 12 个月（含本月）的「每日独立访客按月相加」。"""
    current = date.fromisoformat(today).replace(day=1)
    # 负偏移才是过去：range(11, -1, -1) 会从“11 个月后”倒着排到今天。
    months = [_shift_month(current, offset) for offset in range(-(MONTH_WINDOW - 1), 1)]
    first = months[0].strftime("%Y-%m")
    rows = db.execute(
        select(func.substr(PageviewDaily.day, 1, 7), func.sum(PageviewDaily.uv))
        .where(PageviewDaily.day >= f"{first}-01")
        .group_by(func.substr(PageviewDaily.day, 1, 7))
    ).all()
    totals = {row[0]: int(row[1]) for row in rows}
    return [
        {"month": month.strftime("%Y-%m"), "uv": totals.get(month.strftime("%Y-%m"), 0)}
        for month in months
    ]


# —— 库存 ——


def _inventory(db: DbSession, settings: Settings) -> dict:
    """七类内容的条数、已发布条数与占用。回收站不计入条数。"""
    alive = Asset.status != ASSET_TRASHED
    counts = dict(
        db.execute(
            select(Asset.kind, func.count()).where(alive).group_by(Asset.kind)
        ).all()
    )
    published = dict(
        db.execute(
            select(Asset.kind, func.count())
            .where(Asset.status == ASSET_PUBLISHED)
            .group_by(Asset.kind)
        ).all()
    )
    current_bytes = dict(
        db.execute(
            select(Asset.kind, func.coalesce(func.sum(AssetVersion.byte_size), 0))
            .join(AssetVersion, AssetVersion.id == Asset.current_version_id)
            .where(alive)
            .group_by(Asset.kind)
        ).all()
    )
    total_bytes = dict(
        db.execute(
            select(Asset.kind, func.coalesce(func.sum(AssetVersion.byte_size), 0))
            .join(AssetVersion, AssetVersion.asset_id == Asset.id)
            .where(alive)
            .group_by(Asset.kind)
        ).all()
    )
    trash_count = db.scalar(
        select(func.count()).select_from(Asset).where(Asset.status == ASSET_TRASHED)
    )
    usage = disk_usage(settings.data_dir)

    kinds = [
        {
            "kind": kind,
            "count": int(counts.get(kind, 0)),
            "published": int(published.get(kind, 0)),
            "current_bytes": int(current_bytes.get(kind, 0)),
            "total_bytes": int(total_bytes.get(kind, 0)),
        }
        for kind in KINDS
    ]
    return {
        "kinds": kinds,
        "total_count": sum(item["count"] for item in kinds),
        "total_bytes": sum(item["total_bytes"] for item in kinds),
        "trash_count": int(trash_count or 0),
        # 逻辑占用与磁盘剩余口径不同：硬链接、临时文件与系统占用不在前者里。
        "disk_free_bytes": usage[0] if usage else None,
    }


# —— 日期 ——


def _validate_month(value: str) -> str:
    if not _MONTH_PATTERN.match(value or ""):
        raise ApiError.invalid("月份格式应为 YYYY-MM")
    try:
        date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise ApiError.invalid("月份格式应为 YYYY-MM") from exc
    return value


def _validate_day(value: str) -> str:
    try:
        return date.fromisoformat(value or "").isoformat()
    except ValueError as exc:
        raise ApiError.invalid("日期格式应为 YYYY-MM-DD") from exc


def _shift_month(first_of_month: date, offset: int) -> date:
    index = first_of_month.year * 12 + first_of_month.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)
