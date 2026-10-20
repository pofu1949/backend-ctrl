"""上传会话到期后的行为（企划第 8.3 节 U3）。

到期判断必须发生在**使用会话的那一刻**，不能只靠「下一次建会话时顺手清理」：
没有新上传时，旧会话否则会一直可写、可合并。
"""

from __future__ import annotations

import shutil
from collections import namedtuple
from datetime import timedelta

import psutil
import pytest
from fastapi.testclient import TestClient
from tests.auth_helpers import admin_headers

from ctrlstore.models import UploadSession, utcnow

API = "/api/admin/v1"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """磁盘与内存都充裕，本模块只关心时间维度。"""
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def _open_session(client: TestClient, filename: str = "a.txt") -> dict:
    response = client.post(
        f"{API}/uploads", json={"filename": filename}, headers=admin_headers(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _age_session(client: TestClient, session_id: str, *, hours: int = 25) -> None:
    """把会话的到期时间挪到过去，模拟「已经放了 25 小时」。"""
    with client.app.state.session_factory() as db:
        row = db.get(UploadSession, session_id)
        row.expires_at = utcnow() - timedelta(hours=hours)
        db.commit()


def test_expired_session_rejects_new_parts(owner_client: TestClient, storage) -> None:
    session = _open_session(owner_client)
    _age_session(owner_client, session["id"])

    response = owner_client.put(
        f"{API}/uploads/{session['id']}/parts/1",
        content=b"late",
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 409
    assert "过期" in response.json()["error"]["message"]
    # 过期即清临时目录，不再占空间
    assert not storage.session_dir(session["id"]).exists()


def test_expired_session_cannot_be_completed(owner_client: TestClient, storage) -> None:
    session = _open_session(owner_client)
    assert (
        owner_client.put(
            f"{API}/uploads/{session['id']}/parts/1",
            content=b"hello",
            headers=admin_headers(owner_client),
        ).status_code
        == 200
    )
    _age_session(owner_client, session["id"])

    response = owner_client.post(
        f"{API}/uploads/{session['id']}/complete", headers=admin_headers(owner_client)
    )

    assert response.status_code == 409
    assert "过期" in response.json()["error"]["message"]
    assert not storage.session_dir(session["id"]).exists()
    # 没有产生原件
    assert not any(storage.originals_dir.rglob("original.*"))


def test_expired_session_is_reported_on_read(owner_client: TestClient) -> None:
    session = _open_session(owner_client)
    _age_session(owner_client, session["id"])

    body = owner_client.get(f"{API}/uploads/{session['id']}").json()

    assert body["status"] == "expired"


def test_cancel_of_expired_session_is_not_an_error(owner_client: TestClient, storage) -> None:
    session = _open_session(owner_client)
    _age_session(owner_client, session["id"])

    response = owner_client.delete(
        f"{API}/uploads/{session['id']}", headers=admin_headers(owner_client)
    )

    assert response.status_code == 204
    assert not storage.session_dir(session["id"]).exists()


def test_expiry_persists_after_the_rejected_request(owner_client: TestClient) -> None:
    """409 会让请求事务回滚；到期状态必须已经落库，否则下次仍然可写。"""
    session = _open_session(owner_client)
    _age_session(owner_client, session["id"])

    owner_client.put(
        f"{API}/uploads/{session['id']}/parts/1",
        content=b"late",
        headers=admin_headers(owner_client),
    )

    with owner_client.app.state.session_factory() as db:
        assert db.get(UploadSession, session["id"]).status == "expired"


def test_sweep_survives_a_rejected_create(
    owner_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """建新会话时被磁盘挡住，已作废的旧会话仍要保持作废，不能随事务回滚。"""
    session = _open_session(owner_client)
    _age_session(owner_client, session["id"])
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: _DiskUsage(1000, 1000, 0))

    response = owner_client.post(
        f"{API}/uploads", json={"filename": "b.txt"}, headers=admin_headers(owner_client)
    )

    assert response.status_code == 507
    with owner_client.app.state.session_factory() as db:
        assert db.get(UploadSession, session["id"]).status == "expired"
