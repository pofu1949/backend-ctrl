"""会话、权限与审计的接口测试（规划第 2 部分验收项）。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import (
    EDITOR_PASSWORD,
    OWNER_PASSWORD,
    admin_headers,
)

from ctrlstore import audit
from ctrlstore.auth import deps, permissions, service
from ctrlstore.models import ROLE_OWNER, AuditLog, Session, User, utcnow

API = "/api/admin/v1"


# —— 登录、Cookie 与限速 ——


def test_login_sets_http_only_lax_cookie(client: TestClient, owner) -> None:
    response = client.post(f"{API}/session", json={"username": "owner", "password": OWNER_PASSWORD})

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie  # 未启用 HTTPS 时不能带 Secure，否则浏览器不会回传
    body = response.json()
    assert body["kind"] == "session"
    assert body["user"]["username"] == "owner"
    assert body["csrf_token"]


def test_wrong_password_is_401_and_audited(client: TestClient, owner, database) -> None:
    response = client.post(
        f"{API}/session", json={"username": "owner", "password": "wrong-password"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    with database() as check:
        failed = check.scalars(select(AuditLog).where(AuditLog.action == audit.LOGIN_FAILED)).all()
    assert len(failed) == 1
    assert "wrong-password" not in (failed[0].detail or "")


def test_disabled_account_cannot_log_in(client: TestClient, owner, db: DbSession) -> None:
    editor = service.create_editor(
        db, username="editor", password=EDITOR_PASSWORD, granted=[], actor=owner
    )
    service.disable_user(db, editor, actor=owner)
    db.commit()

    response = client.post(
        f"{API}/session", json={"username": "editor", "password": EDITOR_PASSWORD}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_login_is_throttled_after_repeated_failures(client: TestClient, owner) -> None:
    for _ in range(5):
        assert (
            client.post(
                f"{API}/session", json={"username": "owner", "password": "wrong-password"}
            ).status_code
            == 401
        )

    response = client.post(f"{API}/session", json={"username": "owner", "password": OWNER_PASSWORD})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"


def test_throttle_key_ignores_username_case(client: TestClient, owner) -> None:
    for _ in range(5):
        client.post(f"{API}/session", json={"username": "OWNER", "password": "wrong-password"})

    response = client.post(f"{API}/session", json={"username": "Owner", "password": "wrong"})

    assert response.status_code == 429


# —— 身份读取与退出 ——


def test_me_requires_login(client: TestClient, owner) -> None:
    assert client.get(f"{API}/me").status_code == 401


def test_me_reports_owner_permissions(owner_client: TestClient) -> None:
    body = owner_client.get(f"{API}/me").json()

    assert body["user"]["role"] == "owner"
    assert set(body["user"]["permissions"]) == set(permissions.PERMISSIONS)


def test_logout_revokes_the_session(owner_client: TestClient) -> None:
    headers = admin_headers(owner_client)

    assert owner_client.delete(f"{API}/session", headers=headers).status_code == 204
    assert owner_client.get(f"{API}/me").status_code == 401


def test_absolute_expiry_forces_relogin(owner_client: TestClient, database) -> None:
    with database() as db:
        record = db.scalars(select(Session)).one()
        record.absolute_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

    assert owner_client.get(f"{API}/me").status_code == 401


def test_idle_expiry_forces_relogin(owner_client: TestClient, database, settings) -> None:
    with database() as db:
        record = db.scalars(select(Session)).one()
        record.last_seen_at = utcnow() - timedelta(hours=settings.session_idle_hours + 1)
        db.commit()

    assert owner_client.get(f"{API}/me").status_code == 401


# —— CSRF ——


def test_cookie_write_without_csrf_token_is_rejected(owner_client: TestClient) -> None:
    response = owner_client.post(
        f"{API}/users",
        json={"username": "editor", "password": EDITOR_PASSWORD, "permissions": []},
    )

    assert response.status_code == 403
    assert "CSRF" in response.json()["error"]["message"]


def test_cookie_write_with_wrong_csrf_token_is_rejected(owner_client: TestClient) -> None:
    response = owner_client.post(
        f"{API}/users",
        json={"username": "editor", "password": EDITOR_PASSWORD, "permissions": []},
        headers={"X-CSRF-Token": "forged-token"},
    )

    assert response.status_code == 403


def test_reads_need_no_csrf_token(owner_client: TestClient) -> None:
    assert owner_client.get(f"{API}/users").status_code == 200


def test_bearer_token_skips_csrf(client: TestClient, owner, db: DbSession) -> None:
    plaintext, _ = service.issue_user_token(
        db, user=owner, name="cli", expires_at=None, actor=owner
    )
    db.commit()

    response = client.post(
        f"{API}/users",
        json={"username": "editor", "password": EDITOR_PASSWORD, "permissions": ["asset.read"]},
        headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert response.status_code == 201
    assert response.json()["permissions"] == ["asset.read"]


# —— 账号与权限管理 ——


def test_create_editor_starts_without_permissions(owner_client: TestClient) -> None:
    response = owner_client.post(
        f"{API}/users",
        json={"username": "editor", "password": EDITOR_PASSWORD, "permissions": []},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 201
    assert response.json()["permissions"] == []
    assert response.json()["role"] == "editor"


def test_editor_cannot_reach_owner_endpoints(client: TestClient, owner, db: DbSession) -> None:
    service.create_editor(
        db, username="editor", password=EDITOR_PASSWORD, granted=["asset.read"], actor=owner
    )
    db.commit()
    client.post(f"{API}/session", json={"username": "editor", "password": EDITOR_PASSWORD})

    assert client.get(f"{API}/users").status_code == 403
    assert client.get(f"{API}/tokens").status_code == 403


def test_grant_and_revoke_permissions(owner_client: TestClient) -> None:
    created = owner_client.post(
        f"{API}/users",
        json={"username": "editor", "password": EDITOR_PASSWORD, "permissions": ["asset.read"]},
        headers=admin_headers(owner_client),
    ).json()

    granted = owner_client.put(
        f"{API}/users/{created['id']}/permissions",
        json={"permissions": ["asset.read", "asset.upload"]},
        headers=admin_headers(owner_client),
    )

    assert granted.status_code == 200
    assert sorted(granted.json()["permissions"]) == ["asset.read", "asset.upload"]


def test_unknown_permission_is_rejected(owner_client: TestClient) -> None:
    created = owner_client.post(
        f"{API}/users",
        json={"username": "editor", "password": EDITOR_PASSWORD, "permissions": []},
        headers=admin_headers(owner_client),
    ).json()

    response = owner_client.put(
        f"{API}/users/{created['id']}/permissions",
        json={"permissions": ["asset.delete"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid"


def test_owner_permissions_cannot_be_rewritten(owner_client: TestClient, owner) -> None:
    response = owner_client.put(
        f"{API}/users/{owner.id}/permissions",
        json={"permissions": []},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 403


# —— 唯一所有者、停用与重置 ——


def test_second_owner_is_rejected_by_service(db: DbSession, owner) -> None:
    with pytest.raises(Exception) as excinfo:
        service.create_owner(db, username="second", password=OWNER_PASSWORD)

    assert "所有者" in str(excinfo.value)


def test_database_rejects_a_second_owner_row(db: DbSession, owner) -> None:
    db.add(User(username="intruder", password_hash="x", role=ROLE_OWNER, created_at=utcnow()))

    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_owner_cannot_be_disabled_via_api(owner_client: TestClient, owner) -> None:
    response = owner_client.patch(
        f"{API}/users/{owner.id}",
        json={"disabled": True},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 403


def test_disabling_editor_kills_sessions_and_tokens(
    client: TestClient, owner, db: DbSession
) -> None:
    editor = service.create_editor(
        db, username="editor", password=EDITOR_PASSWORD, granted=["asset.read"], actor=owner
    )
    plaintext, _ = service.issue_user_token(
        db, user=editor, name="cli", expires_at=None, actor=owner
    )
    db.commit()
    client.post(f"{API}/session", json={"username": "editor", "password": EDITOR_PASSWORD})
    assert client.get(f"{API}/me").status_code == 200

    service.disable_user(db, editor, actor=owner)
    db.commit()

    assert client.get(f"{API}/me").status_code == 401
    assert (
        client.get(f"{API}/me", headers={"Authorization": f"Bearer {plaintext}"}).status_code == 401
    )


def test_reset_password_invalidates_old_session(client: TestClient, owner, db: DbSession) -> None:
    editor = service.create_editor(
        db, username="editor", password=EDITOR_PASSWORD, granted=["asset.read"], actor=owner
    )
    db.commit()
    client.post(f"{API}/session", json={"username": "editor", "password": EDITOR_PASSWORD})
    assert client.get(f"{API}/me").status_code == 200

    service.reset_password(db, editor, "new-editor-password-01", actor=owner)
    db.commit()

    assert client.get(f"{API}/me").status_code == 401
    assert (
        client.post(
            f"{API}/session", json={"username": "editor", "password": EDITOR_PASSWORD}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"{API}/session", json={"username": "editor", "password": "new-editor-password-01"}
        ).status_code
        == 200
    )


def test_owner_stays_a_single_row_after_reset(database) -> None:
    with database() as db:
        owner = service.create_owner(db, username="owner", password=OWNER_PASSWORD)
        db.commit()
        service.reset_password(db, owner, "another-owner-password", actor=owner)
        db.commit()

    with database() as db:
        count = db.scalar(select(func.count()).select_from(User).where(User.role == ROLE_OWNER))

    assert count == 1


# —— 站点与令牌 ——


def test_site_and_token_lifecycle(owner_client: TestClient, client: TestClient) -> None:
    headers = admin_headers(owner_client)
    site = owner_client.post(
        f"{API}/sites", json={"key": "main-site", "name": "主站"}, headers=headers
    )
    assert site.status_code == 201

    issued = owner_client.post(
        f"{API}/tokens",
        json={"name": "site-token", "subject_type": "site", "subject_key": "main-site"},
        headers=headers,
    )
    assert issued.status_code == 201
    body = issued.json()
    assert body["token"].startswith(f"{body['id']}.")

    listed = owner_client.get(f"{API}/tokens").json()
    assert listed[0]["revoked"] is False
    assert "token" not in listed[0]  # 明文不会再次出现

    # client 上还带着所有者的会话 Cookie；要按站点令牌验证，先清掉它。
    client.cookies.clear()
    me = client.get(f"{API}/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json()["kind"] == "token"
    assert me.json()["site"]["key"] == "main-site"
    assert me.json()["csrf_token"] is None


def test_revoked_site_token_stops_working(owner_client: TestClient, client: TestClient) -> None:
    headers = admin_headers(owner_client)
    owner_client.post(f"{API}/sites", json={"key": "main-site"}, headers=headers)
    issued = owner_client.post(
        f"{API}/tokens",
        json={"subject_type": "site", "subject_key": "main-site"},
        headers=headers,
    ).json()

    owner_client.delete(f"{API}/tokens/{issued['id']}", headers=headers)

    client.cookies.clear()  # 否则请求会先用所有者会话通过认证
    assert (
        client.get(f"{API}/me", headers={"Authorization": f"Bearer {issued['token']}"}).status_code
        == 401
    )


def test_duplicate_site_key_is_conflict(owner_client: TestClient) -> None:
    headers = admin_headers(owner_client)
    owner_client.post(f"{API}/sites", json={"key": "main-site"}, headers=headers)

    response = owner_client.post(f"{API}/sites", json={"key": "main-site"}, headers=headers)

    assert response.status_code == 409


# —— 权限依赖 ——


def test_require_permission_blocks_editor_without_it(
    client: TestClient, owner, db: DbSession
) -> None:
    """探针路由验证依赖本身：没有 asset.upload 必须 403，授予后放行。"""
    probe = APIRouter()
    upload_permission = deps.require_permission("asset.upload")

    @probe.post("/probe/upload")
    def upload(_: deps.Principal = Depends(upload_permission)) -> dict:
        return {"ok": True}

    client.app.include_router(probe)
    editor = service.create_editor(
        db, username="editor", password=EDITOR_PASSWORD, granted=[], actor=owner
    )
    db.commit()
    client.post(f"{API}/session", json={"username": "editor", "password": EDITOR_PASSWORD})

    assert client.post("/probe/upload", headers=admin_headers(client)).status_code == 403

    service.set_permissions(db, editor, ["asset.upload"], actor=owner)
    db.commit()

    assert client.post("/probe/upload", headers=admin_headers(client)).status_code == 200
