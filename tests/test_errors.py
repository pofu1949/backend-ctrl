"""统一错误结构：所有失败都长成 ``{"error": {"code", "message"}}``。"""

from __future__ import annotations

from fastapi.testclient import TestClient

API = "/api/admin/v1"


def test_unknown_route_uses_the_same_envelope(client: TestClient) -> None:
    response = client.get(f"{API}/no-such-endpoint")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_wrong_method_uses_the_same_envelope(client: TestClient, owner) -> None:
    response = client.get(f"{API}/session")  # 登录只接受 POST

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "invalid"


def test_validation_error_is_reported_in_chinese(client: TestClient, owner) -> None:
    response = client.post(f"{API}/session", json={"username": "owner"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid"
    assert "password" in error["message"]


def test_unauthorized_response_is_not_cacheable(client: TestClient) -> None:
    response = client.get(f"{API}/me")

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
