"""访问令牌（命令行与站点）。

明文形如 ``<id>.<secret>``：先用 id 定位一行，再比对哈希——不需要在整表上
逐个做哈希比较，也就不会随令牌数量变慢。

库中只存 secret 的 SHA-256。令牌本身是 32 字节随机数，熵已足够，
不需要 Argon2 那种慢哈希（慢哈希是为了抵抗人对密码的猜测）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import ApiToken, Site, User, utcnow

#: 随机 secret 的字节数。
SECRET_BYTES = 32


def hash_secret(secret: str) -> str:
    """令牌哈希。会话令牌也用这一套。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_secret() -> str:
    """新的随机 secret（URL 安全，可直接放请求头）。"""
    return secrets.token_urlsafe(SECRET_BYTES)


def create(
    db: DbSession,
    *,
    name: str,
    subject_type: str,
    subject_id: str,
    expires_at: datetime | None = None,
) -> tuple[str, ApiToken]:
    """签发令牌，返回 (明文, 记录)。明文只在这一刻存在。"""
    secret = new_secret()
    token = ApiToken(
        name=name,
        token_hash=hash_secret(secret),
        subject_type=subject_type,
        subject_id=subject_id,
        expires_at=expires_at,
        created_at=utcnow(),
    )
    db.add(token)
    db.flush()
    return f"{token.id}.{secret}", token


def resolve(db: DbSession, raw: str) -> tuple[ApiToken, User | Site] | None:
    """校验令牌并返回其主体。过期、撤销或主体已停用都返回 ``None``。"""
    token_id, _, secret = raw.partition(".")
    if not token_id or not secret:
        return None

    token = db.get(ApiToken, token_id)
    if token is None or token.revoked_at is not None:
        return None
    if not hmac.compare_digest(token.token_hash, hash_secret(secret)):
        return None

    now = utcnow()
    if token.expires_at is not None and token.expires_at <= now:
        return None

    subject = _load_subject(db, token)
    if subject is None:
        return None

    token.last_used_at = now
    return token, subject


def _load_subject(db: DbSession, token: ApiToken) -> User | Site | None:
    if token.subject_type == "user":
        user = db.get(User, token.subject_id)
        if user is None or not user.is_active:
            return None
        return user
    if token.subject_type == "site":
        return db.get(Site, token.subject_id)
    return None


def revoke(db: DbSession, token: ApiToken) -> None:
    """撤销一个令牌。已撤销的保持原时间，便于追溯。"""
    if token.revoked_at is None:
        token.revoked_at = utcnow()


def revoke_all_for_user(db: DbSession, user_id: str) -> int:
    """撤销某个用户的全部令牌，返回条数。站点令牌不受影响。"""
    tokens = db.scalars(
        select(ApiToken).where(
            ApiToken.subject_type == "user",
            ApiToken.subject_id == user_id,
            ApiToken.revoked_at.is_(None),
        )
    ).all()
    now = utcnow()
    for token in tokens:
        token.revoked_at = now
    return len(tokens)
