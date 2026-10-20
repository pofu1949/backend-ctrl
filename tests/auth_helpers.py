"""身份测试共用的小工具。

夹具在 ``conftest.py``；这里只放常量与普通函数，避免夹具定义散落。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

#: 测试用密码，长度满足 12 位下限。
OWNER_PASSWORD = "owner-password-0001"
EDITOR_PASSWORD = "editor-password-0001"

API = "/api/admin/v1"


def csrf_of(client: TestClient) -> str:
    """取当前会话的 CSRF 令牌。"""
    return client.get(f"{API}/me").json()["csrf_token"]


def admin_headers(client: TestClient) -> dict[str, str]:
    """会话客户端的写请求头。"""
    return {"X-CSRF-Token": csrf_of(client)}
