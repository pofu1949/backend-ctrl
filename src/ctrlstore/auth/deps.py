"""请求身份与权限依赖。

这里是「这是谁、能做什么」的唯一入口：

* :func:`current_principal` 认证，失败 401；
* :func:`writer` 在认证之上要求会话写操作带 CSRF 令牌（命令行令牌不需要）；
* :func:`owner` / :func:`owner_writer` 再要求所有者身份；
* :func:`require_permission` 供后续内容接口按能力放行。

把 CSRF 放进依赖而不是各端点自己判断，是为了不会再漏掉某个写接口。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session as DbSession

from .. import errors
from ..auth import permissions as permission_rules
from ..auth import sessions, tokens
from ..models import ROLE_OWNER, Site, User
from ..models import Session as SessionRecord

#: 不改变服务端状态的方法。只有这些方法对会话免 CSRF。
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: CSRF 令牌的请求头名。
CSRF_HEADER = "X-CSRF-Token"

#: 访问令牌的请求头：``Authorization: Bearer <token>``。
AUTHORIZATION_HEADER = "Authorization"


@dataclass(frozen=True)
class Principal:
    """一次请求的调用方。会话与令牌只会是其中之一。"""

    kind: str  # "session" | "token"
    user: User | None = None
    site: Site | None = None
    granted: frozenset[str] = frozenset()
    csrf_token: str | None = None
    session_record: SessionRecord | None = None

    @property
    def is_owner(self) -> bool:
        return self.user is not None and self.user.role == ROLE_OWNER

    @property
    def is_session(self) -> bool:
        return self.kind == "session"

    @property
    def permissions(self) -> frozenset[str]:
        """有效能力。所有者不受权限表限制。"""
        if self.user is None:
            return self.granted
        return permission_rules.effective(self.user.role, self.granted)

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def get_db(request: Request) -> Iterator[DbSession]:
    """一个请求一个事务：成功提交，异常回滚。

    端点里若要在抛错前保住已写入的记录（例如登录失败的审计），
    先自行 ``commit()``；这里随后的回滚只作用于空事务。
    """
    factory = request.app.state.session_factory
    with factory() as db:
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()


def current_principal(request: Request, db: DbSession = Depends(get_db)) -> Principal:
    """Cookie 会话或访问令牌，二选一。"""
    principal = _from_session(request, db) or _from_token(request, db)
    if principal is None:
        raise errors.ApiError.unauthorized()
    return principal


def writer(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
    """写操作的身份。Cookie 会话必须带 CSRF 令牌；访问令牌不需要。"""
    if principal.is_session and request.method not in SAFE_METHODS:
        provided = request.headers.get(CSRF_HEADER)
        record = principal.session_record
        if record is None or not sessions.csrf_matches(record, provided):
            raise errors.ApiError.forbidden("CSRF 令牌缺失或不正确")
    return principal


def owner(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_owner:
        raise errors.ApiError.forbidden("该接口只对所有者开放")
    return principal


def owner_writer(principal: Principal = Depends(writer)) -> Principal:
    if not principal.is_owner:
        raise errors.ApiError.forbidden("该接口只对所有者开放")
    return principal


def ensure_permission(principal: Principal, name: str) -> Principal:
    """按能力放行。用于「权限取决于请求内容」的端点。

    依赖工厂 ``require_permission`` 只能表达固定权限；替换文件要 ``asset.update``、
    新建内容要 ``asset.upload``，这类判断在端点里用本函数，写仍走 ``writer``。
    """
    if not principal.can(name):
        raise errors.ApiError.forbidden(f"缺少权限：{name}")
    return principal


def require_permission(name: str):
    """按能力放行的依赖工厂，供内容接口使用。"""

    def dependency(principal: Principal = Depends(writer)) -> Principal:
        return ensure_permission(principal, name)

    return dependency


def client_ip(request: Request) -> str:
    """来源 IP。Nginx 在 127.0.0.1 反代，``X-Forwarded-For`` 的第一段即客户端地址。"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _from_session(request: Request, db: DbSession) -> Principal | None:
    raw = request.cookies.get(sessions.COOKIE_NAME)
    if not raw:
        return None
    settings = request.app.state.settings
    record = sessions.resolve(db, raw, idle_hours=settings.session_idle_hours)
    if record is None or not record.user.is_active:
        return None
    user = record.user
    return Principal(
        kind="session",
        user=user,
        granted=frozenset(item.permission for item in user.permissions),
        csrf_token=record.csrf_token,
        session_record=record,
    )


def _from_token(request: Request, db: DbSession) -> Principal | None:
    header = request.headers.get(AUTHORIZATION_HEADER, "")
    scheme, _, raw = header.partition(" ")
    if scheme.lower() != "bearer" or not raw.strip():
        return None
    resolved = tokens.resolve(db, raw.strip(), ip=client_ip(request))
    if resolved is None:
        return None
    _, subject = resolved
    if isinstance(subject, Site):
        return Principal(kind="token", site=subject)
    return Principal(
        kind="token",
        user=subject,
        granted=frozenset(item.permission for item in subject.permissions),
    )
