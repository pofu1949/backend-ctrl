"""身份相关的业务操作。

接口和命令行都调用这里，保证「谁能做什么」只有一份实现：
命令行不是后门，它只是另一种调用方式。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from .. import audit
from ..errors import ApiError
from ..models import (
    ROLE_EDITOR,
    ROLE_OWNER,
    SUBJECT_USER,
    ApiToken,
    Site,
    User,
    UserPermission,
    utcnow,
)
from . import passwords, permissions, sessions, tokens

#: 用户名允许的字符数。允许中文，但去掉首尾空白后不能为空。
USERNAME_MAX_LENGTH = 64


def find_user(db: DbSession, username: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))


def list_users(db: DbSession) -> list[User]:
    """所有者在前，其余按创建时间。"""
    return list(db.scalars(select(User).order_by(User.role.desc(), User.created_at)).all())


def find_site(db: DbSession, key: str) -> Site | None:
    return db.scalar(select(Site).where(Site.key == key.strip()))


def list_sites(db: DbSession) -> list[Site]:
    return list(db.scalars(select(Site).order_by(Site.key)).all())


def list_tokens(db: DbSession) -> list[ApiToken]:
    """最近签发的在前。"""
    return list(db.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())).all())


def get_token(db: DbSession, token_id: str) -> ApiToken:
    token = db.get(ApiToken, token_id)
    if token is None:
        raise ApiError.not_found("令牌不存在")
    return token


def get_user(db: DbSession, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise ApiError.not_found("账号不存在")
    return user


def get_owner(db: DbSession) -> User | None:
    return db.scalar(select(User).where(User.role == ROLE_OWNER))


def create_owner(db: DbSession, *, username: str, password: str) -> User:
    """初始化时创建唯一所有者。已存在就拒绝——不允许出现第二个所有者。"""
    if get_owner(db) is not None:
        raise ApiError.conflict("所有者已存在，不能再创建")
    user = _create_user(db, username=username, password=password, role=ROLE_OWNER)
    audit.record_for_user(
        db,
        user,
        action=audit.USER_CREATED,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        detail={"role": ROLE_OWNER},
    )
    return user


def create_editor(
    db: DbSession,
    *,
    username: str,
    password: str,
    granted: Iterable[str] = (),
    actor: User | None = None,
) -> User:
    """创建编辑。新编辑默认没有任何内容权限（企划第 5.1 节）。"""
    selected = _validated_permissions(granted)
    user = _create_user(db, username=username, password=password, role=ROLE_EDITOR)
    _replace_permissions(db, user, selected)
    audit.record(
        db,
        action=audit.USER_CREATED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_USER,
        target_id=user.id,
        detail={"role": ROLE_EDITOR, "permissions": sorted(selected)},
    )
    return user


def set_permissions(db: DbSession, user: User, names: Iterable[str], *, actor: User) -> User:
    """整体替换编辑的权限集合。所有者不接受这项操作。"""
    _reject_owner(user, "所有者的权限不能修改")
    selected = _validated_permissions(names)
    before = {item.permission for item in user.permissions}
    _replace_permissions(db, user, selected)
    audit.record_for_user(
        db,
        actor,
        action=audit.PERMISSIONS_CHANGED,
        target_type=audit.TARGET_USER,
        target_id=user.id,
        detail={"before": sorted(before), "after": sorted(selected)},
    )
    return user


def reset_password(db: DbSession, user: User, password: str, *, actor: User | None = None) -> None:
    """改密码后，该用户的所有会话与个人令牌立即失效。"""
    _validated_password(password)
    user.password_hash = passwords.hash_password(password)
    sessions.revoke_all_for_user(db, user.id)
    tokens.revoke_all_for_user(db, user.id)
    audit.record(
        db,
        action=audit.PASSWORD_RESET,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else user.id,
        target_type=audit.TARGET_USER,
        target_id=user.id,
    )


def disable_user(db: DbSession, user: User, *, actor: User) -> None:
    """停用编辑：会话与个人令牌立即失效。所有者不能停用。"""
    _reject_owner(user, "所有者不能停用")
    if not user.is_active:
        return
    user.disabled_at = utcnow()
    sessions.revoke_all_for_user(db, user.id)
    tokens.revoke_all_for_user(db, user.id)
    audit.record_for_user(
        db, actor, action=audit.USER_DISABLED, target_type=audit.TARGET_USER, target_id=user.id
    )


def create_site(db: DbSession, *, key: str, name: str, actor: User | None = None) -> Site:
    """创建网站。``key`` 全局唯一，用于配置和日志辨认。"""
    normalized = key.strip()
    if not normalized:
        raise ApiError.invalid("站点标识不能为空")
    site = Site(key=normalized, name=name.strip() or normalized)
    db.add(site)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ApiError.conflict(f"站点标识已存在：{normalized}") from exc
    audit.record(
        db,
        action=audit.SITE_CREATED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_SITE,
        target_id=site.id,
        detail={"key": site.key},
    )
    return site


def issue_site_token(
    db: DbSession, *, site: Site, name: str, expires_at: datetime | None, actor: User | None
) -> tuple[str, ApiToken]:
    """为站点签发只读令牌。明文只在返回值里出现一次。"""
    plaintext, token = tokens.create(
        db,
        name=name.strip() or site.key,
        subject_type="site",
        subject_id=site.id,
        expires_at=expires_at,
    )
    audit.record(
        db,
        action=audit.TOKEN_CREATED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_TOKEN,
        target_id=token.id,
        detail={"subject_type": "site", "subject": site.key, "name": token.name},
    )
    return plaintext, token


def issue_user_token(
    db: DbSession, *, user: User, name: str, expires_at: datetime | None, actor: User | None
) -> tuple[str, ApiToken]:
    """为用户签发命令行令牌。"""
    plaintext, token = tokens.create(
        db,
        name=name.strip() or "cli",
        subject_type=SUBJECT_USER,
        subject_id=user.id,
        expires_at=expires_at,
    )
    audit.record(
        db,
        action=audit.TOKEN_CREATED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_TOKEN,
        target_id=token.id,
        detail={"subject_type": "user", "subject": user.username, "name": token.name},
    )
    return plaintext, token


def revoke_token(db: DbSession, token: ApiToken, *, actor: User) -> None:
    tokens.revoke(db, token)
    audit.record_for_user(
        db,
        actor,
        action=audit.TOKEN_REVOKED,
        target_type=audit.TARGET_TOKEN,
        target_id=token.id,
        detail={"name": token.name},
    )


def _create_user(db: DbSession, *, username: str, password: str, role: str) -> User:
    name = username.strip()
    if not name or len(name) > USERNAME_MAX_LENGTH:
        raise ApiError.invalid(f"用户名需为 1–{USERNAME_MAX_LENGTH} 个字符")
    _validated_password(password)
    if find_user(db, name) is not None:
        raise ApiError.conflict(f"用户名已存在：{name}")

    user = User(
        username=name,
        password_hash=passwords.hash_password(password),
        role=role,
        created_at=utcnow(),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        # 并发创建同名账号，或试图插入第二个所有者：两个索引都会拦下来。
        raise ApiError.conflict("用户名已存在，或所有者已存在") from exc
    return user


def _replace_permissions(db: DbSession, user: User, selected: set[str]) -> None:
    """整体替换：先删后插，避免做集合差集。"""
    for item in list(user.permissions):
        db.delete(item)
    db.flush()
    for name in sorted(selected):
        user.permissions.append(UserPermission(permission=name))
    db.flush()


def _reject_owner(user: User, message: str) -> None:
    if user.is_owner:
        raise ApiError.forbidden(message)


def _validated_permissions(names: Iterable[str]) -> set[str]:
    """把「未知权限名」翻译成 422，命令行与接口看到同一种错误。"""
    try:
        return permissions.normalize(names)
    except permissions.UnknownPermissionError as exc:
        raise ApiError.invalid(str(exc)) from exc


def _validated_password(password: str) -> None:
    try:
        passwords.validate_password(password)
    except passwords.WeakPasswordError as exc:
        raise ApiError.invalid(str(exc)) from exc
