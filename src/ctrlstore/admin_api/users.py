"""账号与权限管理。全部只对所有者开放。

接口是薄的：校验形状、调用 ``auth.service``、返回结构。
「谁能做什么」的判断都在 service 里，命令行用的是同一套。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import errors
from ..auth import deps, service
from ..models import User
from .schemas import CreateUserRequest, PermissionsRequest, UpdateUserRequest, UserOut

router = APIRouter(tags=["users"])

#: 返回顺序：所有者在前，编辑按创建时间。
_ORDER = (User.role.desc(), User.created_at)


@router.get("/users", response_model=list[UserOut])
def list_users(
    _: deps.Principal = Depends(deps.owner),
    db: DbSession = Depends(deps.get_db),
) -> list[UserOut]:
    users = db.scalars(select(User).order_by(*_ORDER)).all()
    return [UserOut.of(user) for user in users]


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    payload: CreateUserRequest,
    actor: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> UserOut:
    """创建编辑。新编辑默认没有任何内容权限。"""
    user = service.create_editor(
        db,
        username=payload.username,
        password=payload.password,
        granted=payload.permissions,
        actor=actor.user,
    )
    return UserOut.of(user)


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    actor: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> UserOut:
    """改密码或停用。角色与用户名不可变，因此这里没有对应字段。"""
    user = service.get_user(db, user_id)
    if payload.password is not None:
        service.reset_password(db, user, payload.password, actor=actor.user)
    if payload.disabled is not None:
        if payload.disabled:
            service.disable_user(db, user, actor=actor.user)
        else:
            raise errors.ApiError.invalid("第一期不支持重新启用账号，请重建账号")
    if payload.password is None and payload.disabled is None:
        raise errors.ApiError.invalid("没有需要修改的字段")
    return UserOut.of(user)


@router.put("/users/{user_id}/permissions", response_model=UserOut)
def replace_permissions(
    user_id: str,
    payload: PermissionsRequest,
    actor: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> UserOut:
    """整体替换权限集合。所有者不接受这项操作。"""
    user = service.get_user(db, user_id)
    service.set_permissions(db, user, payload.permissions, actor=actor.user)
    return UserOut.of(user)
