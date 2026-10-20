"""数据模型（企划第 10 章）。

时间一律以 UTC 存成不带时区的值：SQLite 没有原生时区类型，若允许两种写法混存，
同一列就会有两种含义。约定「读出来的 datetime 都当 UTC」，展示层再本地化。

主键用 UUID 字符串：SQLite 没有 UUID 类型，字符串在库内可读、可核对。
后续部分继续在同一个 ``Base`` 上加表。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: 角色。只有一行可以是 owner，由部分唯一索引强制（企划第 10.1 节）。
ROLE_OWNER = "owner"
ROLE_EDITOR = "editor"

#: 令牌主体类型。
SUBJECT_USER = "user"
SUBJECT_SITE = "site"


def new_id() -> str:
    """新的主键。"""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """当前 UTC 时间，去掉时区信息，与库内存法一致。"""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """所有表的基类。迁移的 ``target_metadata`` 指向它的 metadata。"""


class User(Base):
    """账号。所有者与编辑各一行，角色不能通过接口修改。"""

    __tablename__ = "users"
    __table_args__ = (
        # SQLite 的普通 UNIQUE 会把多个 NULL 视为不同值，这里要的是「至多一个 owner」。
        Index("ux_users_single_owner", "role", unique=True, sqlite_where=text("role = 'owner'")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    permissions: Mapped[list[UserPermission]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    @property
    def is_active(self) -> bool:
        return self.disabled_at is None


class UserPermission(Base):
    """授予编辑的单项能力。所有者不读这张表，代码里视为拥有全部。"""

    __tablename__ = "user_permissions"
    __table_args__ = (UniqueConstraint("user_id", "permission", name="ux_user_permission"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    permission: Mapped[str] = mapped_column(String(32))

    user: Mapped[User] = relationship(back_populates="permissions")


class Session(Base):
    """管理会话。库里只有令牌哈希，客户端只拿 Cookie 里的原文。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    #: 绝对有效期。到期后无论是否活跃都必须重新登录。
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(lazy="joined")


class ApiToken(Base):
    """访问令牌。明文只在创建时返回一次，库里只存哈希。"""

    __tablename__ = "api_tokens"
    __table_args__ = (Index("ix_api_tokens_subject", "subject_type", "subject_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Site(Base):
    """网站。第一期只接一个 Next.js 站，但数据模型按多站设计。"""

    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """审计记录。只追加，不修改；登录失败也写在这里。"""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(32), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(36))
    #: 补充信息用 JSON 文本，不参与查询；含 IP 之类不能进正式列的内容。
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
