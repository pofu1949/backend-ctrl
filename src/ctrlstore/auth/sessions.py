"""Cookie 会话。

会话存在 SQLite 里，不放进程内存：两个 API 进程必须看到同一份失效状态，
否则「退出登录」只在其中一个进程生效。

两条时限同时生效（企划第 8.1 节 A2）：

* 绝对有效期 12 小时，从登录算起，到期必须重新登录；
* 空闲 2 小时，每次访问刷新。

库里只存令牌的 SHA-256，拖库不能直接拿去冒充。Cookie 原文只发给浏览器。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import Session, User, utcnow
from . import tokens

#: Cookie 名。改动会让所有已登录用户掉线，非必要不要改。
COOKIE_NAME = "cs_session"

#: ``last_seen_at`` 的最短刷新间隔。每个请求都写库会白白增加写锁竞争。
TOUCH_INTERVAL = timedelta(seconds=60)

#: CSRF 令牌长度（字节随机数）。
CSRF_BYTES = 32


@dataclass(frozen=True)
class Issued:
    """一次登录的结果。原文只出现这一次。"""

    raw_token: str
    csrf_token: str
    record: Session


def start(db: DbSession, user: User, *, absolute_hours: int) -> Issued:
    """为用户开一个新会话。**不** 撤销该用户的其他会话（允许多端同时登录）。"""
    raw = tokens.new_secret()
    now = utcnow()
    record = Session(
        user_id=user.id,
        token_hash=tokens.hash_secret(raw),
        csrf_token=tokens.new_secret(),
        created_at=now,
        last_seen_at=now,
        absolute_expires_at=now + timedelta(hours=absolute_hours),
    )
    db.add(record)
    db.flush()
    return Issued(raw_token=raw, csrf_token=record.csrf_token, record=record)


def resolve(db: DbSession, raw: str, *, idle_hours: int) -> Session | None:
    """按 Cookie 原文取会话。过期即撤销，返回 ``None``。"""
    record = db.scalar(select(Session).where(Session.token_hash == tokens.hash_secret(raw)))
    if record is None or record.revoked_at is not None:
        return None

    now = utcnow()
    if is_expired(record, now, idle_hours=idle_hours):
        revoke(db, record, now=now)
        return None

    if now - record.last_seen_at >= TOUCH_INTERVAL:
        record.last_seen_at = now
    return record


def is_expired(record: Session, now: datetime, *, idle_hours: int) -> bool:
    """绝对到期或空闲超时。"""
    if record.absolute_expires_at <= now:
        return True
    return now - record.last_seen_at > timedelta(hours=idle_hours)


def revoke(db: DbSession, record: Session, *, now: datetime | None = None) -> None:
    """撤销单个会话。"""
    if record.revoked_at is None:
        record.revoked_at = now or utcnow()


def revoke_all_for_user(db: DbSession, user_id: str, *, now: datetime | None = None) -> int:
    """撤销某用户的全部会话，返回条数。改密码和停用账号时使用。"""
    sessions = db.scalars(
        select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
    ).all()
    stamp = now or utcnow()
    for record in sessions:
        record.revoked_at = stamp
    return len(sessions)


def csrf_matches(record: Session, provided: str | None) -> bool:
    """常数时间比较 CSRF 令牌。"""
    if not provided:
        return False
    return hmac.compare_digest(record.csrf_token, provided)
