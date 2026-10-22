"""幂等键（企划第 10.3、13.3 节）。

网络重试不该产生两份内容。客户端带 ``Idempotency-Key`` 时：

* 24 小时内同一键、同一请求体 → 返回第一次的状态码和响应体；
* 同一键、不同请求体 → 409，让调用方知道键被复用了。

记录与应用数据在同一个事务里提交：要么两者都在，要么都不在。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from .errors import ApiError
from .models import IdempotencyKey, utcnow

#: 幂等键的有效期（小时）。
RETENTION_HOURS = 24

#: 请求头名。
HEADER = "Idempotency-Key"


@dataclass(frozen=True)
class Stored:
    """第一次执行的结果。"""

    status: int
    body: dict[str, Any]


def lookup(
    db: DbSession, *, actor_id: str, method: str, path: str, key: str, request_hash: str
) -> Stored | None:
    """命中就返回第一次的结果；键被复用到别的请求体上则 409。"""
    row = db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.actor_id == actor_id,
            IdempotencyKey.method == method,
            IdempotencyKey.path == path,
            IdempotencyKey.key == key,
        )
    )
    if row is None:
        return None
    if row.created_at < utcnow() - timedelta(hours=RETENTION_HOURS):
        return None
    if row.request_hash != request_hash:
        raise ApiError.conflict("该幂等键已用于另一个请求")
    return Stored(status=row.response_status, body=json.loads(row.response_body))


def remember(
    db: DbSession,
    *,
    actor_id: str,
    method: str,
    path: str,
    key: str,
    request_hash: str,
    status: int,
    body: dict[str, Any],
) -> None:
    """记下这次结果。同一键重复写入说明并发重试，直接以先到的为准。"""
    existing = db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.actor_id == actor_id,
            IdempotencyKey.method == method,
            IdempotencyKey.path == path,
            IdempotencyKey.key == key,
        )
    )
    if existing is not None:
        if existing.created_at >= utcnow() - timedelta(hours=RETENTION_HOURS):
            return
        db.delete(existing)
        db.flush()
    db.add(
        IdempotencyKey(
            actor_id=actor_id,
            method=method,
            path=path,
            key=key,
            request_hash=request_hash,
            response_status=status,
            response_body=json.dumps(body, ensure_ascii=False),
            created_at=utcnow(),
        )
    )


def hash_request(payload: bytes | str | None) -> str:
    """请求体哈希。空体用空串的哈希，语义仍然是「同一请求」。"""
    from hashlib import sha256

    if payload is None:
        payload = b""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return sha256(payload).hexdigest()


def purge_expired(db: DbSession) -> int:
    """清掉过期的键。由上传会话清理顺带调用，不单独起任务。"""
    result = db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.created_at < utcnow() - timedelta(hours=RETENTION_HOURS)
        )
    )
    return result.rowcount or 0
