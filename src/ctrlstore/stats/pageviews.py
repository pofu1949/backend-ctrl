"""访问事件记录（企划第 8.9 节）。

三件事只有这一个地方做：

1. **归日**：按服务器**收到请求**的时间归入 ``Asia/Shanghai``，不接受补报；
2. **去重**：同一站点 24 小时内同一 ``event_id`` 只计一次；同一人当天换页只加次数；
3. **隐私**：访客标识只以 HMAC 入库，不存原始标识、IP 和查询串。

明细表（事件、访客哈希、路径）保留 400 天，由 worker 清理；日汇总长期保留。
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiError
from ..models import (
    MAX_PAGEVIEW_PATHS,
    PAGEVIEW_DETAIL_RETENTION_DAYS,
    PAGEVIEW_OTHER_PATH,
    PageviewDaily,
    PageviewEvent,
    PageviewMergedPath,
    PageviewPath,
    PageviewVisitor,
    utcnow,
)

#: 统计口径的时区。库内时间仍是 UTC，只在归日与展示时换算。
TIMEZONE = ZoneInfo("Asia/Shanghai")

#: ``event_id`` 去重窗口。
EVENT_RETENTION_HOURS = 24

#: 路径长度上限，与列宽一致。
MAX_PATH_LENGTH = 512

#: 这些前缀下的请求不计：接口、公开文件、管理端与内部跳转。
IGNORED_PREFIXES = ("/api", "/p", "/admin", "/healthz", "/_internal")

#: 预取/预渲染请求的标记头。命中任意一个就不计数。
PREFETCH_HEADERS = ("Sec-Purpose", "Purpose", "X-Purpose", "X-Moz")

#: 常见爬虫、社交预览与合成监控的 UA 片段。盖不住全部机器人，面板上也写明了这一点。
BOT_MARKERS = (
    "bot",
    "crawler",
    "spider",
    "slurp",
    "archiver",
    "facebookexternalhit",
    "headlesschrome",
    "lighthouse",
    "pingdom",
    "uptime",
    "preview",
)

#: 跳过计数的原因。稳定代号，便于网站侧排错。
REASON_DUPLICATE = "duplicate"
REASON_PREFETCH = "prefetch"
REASON_BOT = "bot"
REASON_IGNORED_PATH = "ignored_path"


@dataclass(frozen=True)
class Recorded:
    """一次上报的处理结果。``counted`` 为假时 ``reason`` 说明为什么没计。"""

    counted: bool
    reason: str | None = None


def record(
    db: DbSession,
    *,
    site_id: str,
    secret: str,
    event_id: str,
    visitor_id: str,
    path: str,
    now: datetime | None = None,
) -> Recorded:
    """记录一次页面打开。调用方负责提交事务。

    ``now`` 只用于测试归日边界；生产不传，取服务器当前时间。
    """
    day = local_day(now)

    # 事件先去重：重试到达时不再写任何计数，人数与次数都不动。
    if not _remember_event(db, site_id=site_id, event_id=event_id):
        return Recorded(counted=False, reason=REASON_DUPLICATE)

    new_visitor = _remember_visitor(
        db,
        site_id=site_id,
        day=day,
        digest=visitor_hash(secret, visitor_id),
    )
    _bump_day(db, site_id=site_id, day=day, new_visitor=new_visitor)
    _bump_path(db, site_id=site_id, day=day, path=path)
    _compact_paths(db, site_id=site_id, day=day)
    return Recorded(counted=True)


def local_day(now: datetime | None = None) -> str:
    """把某一刻换算成 ``Asia/Shanghai`` 的日期。

    UTC 15:59 与 16:00 落在相邻的两个日历日；不带时区的值按 UTC 理解。
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(TIMEZONE).date().isoformat()


def visitor_hash(secret: str, visitor_id: str) -> str:
    """访客标识的 HMAC。派生标签把统计用途与文件签名隔开。"""
    message = f"ctrlstore/pageview/v1|{visitor_id}".encode()
    return hmac.new(secret.encode("utf-8"), message, sha256).hexdigest()


def validate_path(path: str) -> str:
    """入库前的路径检查。格式错误是要修代码的错误，直接 422。"""
    value = (path or "").strip()
    if not value.startswith("/") or len(value) > MAX_PATH_LENGTH:
        raise ApiError.invalid(f"路径必须是以 / 开头、最长 {MAX_PATH_LENGTH} 字符的相对路径")
    if ".." in value or "\\" in value or "://" in value or "\x00" in value:
        raise ApiError.invalid("路径含非法片段")
    return value


def ignored_reason(
    *, path: str, user_agent: str | None, headers: Mapping[str, str]
) -> str | None:
    """这次请求要不要计数。返回 ``None`` 才计数。"""
    if _is_prefetch(headers):
        return REASON_PREFETCH
    if _is_bot(user_agent):
        return REASON_BOT
    if _is_technical_path(path) or "?" in path or "#" in path:
        return REASON_IGNORED_PATH
    return None


def purge_old(
    db: DbSession,
    *,
    today: str | None = None,
    detail_days: int = PAGEVIEW_DETAIL_RETENTION_DAYS,
    event_hours: int = EVENT_RETENTION_HOURS,
) -> dict[str, int]:
    """删掉过期的明细。日汇总不动——它是日历的历史。

    明细保留「含今天的最近 400 天」；事件只留 24 小时。
    """
    cutoff = shift_day(today or local_day(), -(detail_days - 1))
    visitors = db.execute(delete(PageviewVisitor).where(PageviewVisitor.day < cutoff)).rowcount
    paths = db.execute(delete(PageviewPath).where(PageviewPath.day < cutoff)).rowcount
    merged = db.execute(
        delete(PageviewMergedPath).where(PageviewMergedPath.day < cutoff)
    ).rowcount
    events = db.execute(
        delete(PageviewEvent).where(
            PageviewEvent.received_at < utcnow() - timedelta(hours=event_hours)
        )
    ).rowcount
    return {
        "visitors": visitors or 0,
        "paths": paths or 0,
        "merged_paths": merged or 0,
        "events": events or 0,
    }


# —— 内部 ——


def _remember_event(db: DbSession, *, site_id: str, event_id: str) -> bool:
    """登记事件；已存在返回假（这次是重试）。"""
    result = db.execute(
        sqlite_insert(PageviewEvent)
        .values(site_id=site_id, event_id=event_id, received_at=utcnow())
        .on_conflict_do_nothing(index_elements=["site_id", "event_id"])
    )
    return (result.rowcount or 0) > 0


def _remember_visitor(db: DbSession, *, site_id: str, day: str, digest: str) -> bool:
    """登记当日访客；已存在返回假（人数不再加）。"""
    result = db.execute(
        sqlite_insert(PageviewVisitor)
        .values(site_id=site_id, day=day, visitor_hash=digest)
        .on_conflict_do_nothing(index_elements=["site_id", "day", "visitor_hash"])
    )
    return (result.rowcount or 0) > 0


def _bump_day(db: DbSession, *, site_id: str, day: str, new_visitor: bool) -> None:
    """同日汇总加一次浏览；新访客再加一个人。"""
    uv_increment = 1 if new_visitor else 0
    statement = sqlite_insert(PageviewDaily).values(
        site_id=site_id, day=day, uv=uv_increment, pv=1
    )
    db.execute(
        statement.on_conflict_do_update(
            index_elements=["site_id", "day"],
            set_={"uv": PageviewDaily.uv + uv_increment, "pv": PageviewDaily.pv + 1},
        )
    )


def _bump_path(db: DbSession, *, site_id: str, day: str, path: str) -> None:
    """给路径加一次。被并入「其他」的路径回来时先恢复成独立一行。"""
    merged = db.scalar(
        select(PageviewMergedPath).where(
            PageviewMergedPath.site_id == site_id,
            PageviewMergedPath.day == day,
            PageviewMergedPath.path == path,
        )
    )
    if merged is not None:
        _restore_merged_path(db, site_id=site_id, day=day, path=path, merged=merged)
        return

    statement = sqlite_insert(PageviewPath).values(site_id=site_id, day=day, path=path, pv=1)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=["site_id", "day", "path"],
            set_={"pv": PageviewPath.pv + 1},
        )
    )


def _restore_merged_path(
    db: DbSession, *, site_id: str, day: str, path: str, merged: PageviewMergedPath
) -> None:
    """把「其他」里的历史次数还给这条路径，再加这一次。

    不这么做的话，一条路径一旦被并入「其他」就永远回不到独立计数。
    """
    statement = sqlite_insert(PageviewPath).values(
        site_id=site_id, day=day, path=path, pv=merged.pv + 1
    )
    db.execute(
        statement.on_conflict_do_update(
            index_elements=["site_id", "day", "path"],
            set_={"pv": PageviewPath.pv + merged.pv + 1},
        )
    )
    db.execute(
        update(PageviewPath)
        .where(
            PageviewPath.site_id == site_id,
            PageviewPath.day == day,
            PageviewPath.path == PAGEVIEW_OTHER_PATH,
        )
        .values(pv=PageviewPath.pv - merged.pv)
    )
    db.execute(
        delete(PageviewPath).where(
            PageviewPath.site_id == site_id,
            PageviewPath.day == day,
            PageviewPath.path == PAGEVIEW_OTHER_PATH,
            PageviewPath.pv <= 0,
        )
    )
    db.execute(
        delete(PageviewMergedPath).where(
            PageviewMergedPath.site_id == site_id,
            PageviewMergedPath.day == day,
            PageviewMergedPath.path == path,
        )
    )


def _compact_paths(db: DbSession, *, site_id: str, day: str) -> None:
    """一天的不同路径超过上限时，留下次数最多的，其余并入「其他」。

    被并掉的路径把次数记进账本；它下次出现时可以由
    :func:`_restore_merged_path` 恢复，而不是永久沉在「其他」里。
    """
    where = (PageviewPath.site_id == site_id, PageviewPath.day == day)
    total = db.scalar(select(func.count()).select_from(PageviewPath).where(*where)) or 0
    if total <= MAX_PAGEVIEW_PATHS:
        return

    kept = db.scalars(
        select(PageviewPath.path)
        .where(*where, PageviewPath.path != PAGEVIEW_OTHER_PATH)
        .order_by(PageviewPath.pv.desc(), PageviewPath.path)
        .limit(MAX_PAGEVIEW_PATHS - 1)
    ).all()
    overflow = db.execute(
        select(PageviewPath.path, PageviewPath.pv).where(
            *where,
            PageviewPath.path.notin_(kept),
            PageviewPath.path != PAGEVIEW_OTHER_PATH,
        )
    ).all()
    if not overflow:
        return

    merged_total = 0
    for path, pv in overflow:
        merged_total += int(pv)
        statement = sqlite_insert(PageviewMergedPath).values(
            site_id=site_id, day=day, path=path, pv=int(pv)
        )
        db.execute(
            statement.on_conflict_do_update(
                index_elements=["site_id", "day", "path"],
                set_={"pv": PageviewMergedPath.pv + int(pv)},
            )
        )

    db.execute(
        delete(PageviewPath).where(
            *where,
            PageviewPath.path.notin_(kept),
            PageviewPath.path != PAGEVIEW_OTHER_PATH,
        )
    )
    statement = sqlite_insert(PageviewPath).values(
        site_id=site_id, day=day, path=PAGEVIEW_OTHER_PATH, pv=merged_total
    )
    db.execute(
        statement.on_conflict_do_update(
            index_elements=["site_id", "day", "path"],
            set_={"pv": PageviewPath.pv + merged_total},
        )
    )


def _is_prefetch(headers: Mapping[str, str]) -> bool:
    for name in PREFETCH_HEADERS:
        value = (headers.get(name) or "").lower()
        if "prefetch" in value or "preview" in value:
            return True
    return False


def _is_bot(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    lowered = user_agent.lower()
    return any(marker in lowered for marker in BOT_MARKERS)


def _is_technical_path(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in IGNORED_PREFIXES)


def shift_day(day: str, offset: int) -> str:
    """``YYYY-MM-DD`` 加减天数。归日与清理共用这一处。"""
    return (date.fromisoformat(day) + timedelta(days=offset)).isoformat()
