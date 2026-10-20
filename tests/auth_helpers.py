"""身份测试共用的小工具。

夹具在 ``conftest.py``；这里只放常量与普通函数，避免夹具定义散落。
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession

from ctrlstore.auth import service
from ctrlstore.models import User

#: 测试用密码，长度满足 12 位下限。
OWNER_PASSWORD = "owner-password-0001"
EDITOR_PASSWORD = "editor-password-0001"

API = "/api/admin/v1"


def login(client: TestClient, username: str, password: str) -> dict:
    """登录并断言成功，返回响应体。"""
    response = client.post(f"{API}/session", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def csrf_of(client: TestClient) -> str:
    """取当前会话的 CSRF 令牌。"""
    return client.get(f"{API}/me").json()["csrf_token"]


def admin_headers(client: TestClient) -> dict[str, str]:
    """会话客户端的写请求头。"""
    return {"X-CSRF-Token": csrf_of(client)}


def add_editor(
    db: DbSession,
    *,
    username: str,
    permissions: Iterable[str] = (),
    password: str = EDITOR_PASSWORD,
) -> User:
    """建一个编辑并提交，供需要“另一个人”的用例使用。"""
    owner = service.get_owner(db)
    user = service.create_editor(
        db, username=username, password=password, granted=permissions, actor=owner
    )
    db.commit()
    return user
