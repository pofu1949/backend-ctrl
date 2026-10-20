"""管理接口的请求与响应结构。

字段名与企划第 13 章的示例保持一致；时间统一按 UTC 的 ISO 8601 输出。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..auth import permissions as permission_rules
from ..models import ApiToken, User


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class UserOut(BaseModel):
    """账号的对外形态。绝不包含密码哈希。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    role: str
    disabled: bool
    created_at: datetime
    permissions: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, user: User) -> UserOut:
        granted = permission_rules.effective(
            user.role, (item.permission for item in user.permissions)
        )
        return cls(
            id=user.id,
            username=user.username,
            role=user.role,
            disabled=not user.is_active,
            created_at=user.created_at,
            permissions=sorted(granted),
        )


class SiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    key: str
    name: str
    created_at: datetime


class MeOut(BaseModel):
    """当前身份。``csrf_token`` 只对 Cookie 会话返回。"""

    kind: str
    user: UserOut | None = None
    site: SiteOut | None = None
    csrf_token: str | None = None

    @classmethod
    def of(cls, principal, csrf_token: str | None) -> MeOut:
        return cls(
            kind=principal.kind,
            user=UserOut.of(principal.user) if principal.user else None,
            site=SiteOut.model_validate(principal.site) if principal.site else None,
            csrf_token=csrf_token,
        )


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)
    permissions: list[str] = Field(default_factory=list)


class UpdateUserRequest(BaseModel):
    """只允许改密码和停用状态。角色与用户名不可变。"""

    password: str | None = None
    disabled: bool | None = None


class PermissionsRequest(BaseModel):
    permissions: list[str] = Field(default_factory=list)


class CreateSiteRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=128)


class TokenOut(BaseModel):
    """令牌的对外形态。明文只在创建响应里出现一次。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    subject_type: str
    subject_id: str
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked: bool

    @classmethod
    def of(cls, token: ApiToken) -> TokenOut:
        return cls(
            id=token.id,
            name=token.name,
            subject_type=token.subject_type,
            subject_id=token.subject_id,
            created_at=token.created_at,
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            revoked=token.revoked_at is not None,
        )


class IssuedTokenOut(TokenOut):
    """签发结果，多一个只能看一次的 ``token``。"""

    token: str


class CreateTokenRequest(BaseModel):
    name: str = Field(default="", max_length=64)
    subject_type: str = Field(pattern="^(site|user)$")
    subject_key: str = Field(min_length=1, description="站点标识或用户名")
    expires_at: datetime | None = None
