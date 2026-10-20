"""上传接口与流程（规划第 3 部分验收项）。

覆盖：分片上传、缺片、重传覆盖、一次性上传、类型不符、路径穿越、
磁盘阈值、并发会话上限、幂等重放、权限隔离、过期清理。
"""

from __future__ import annotations

import hashlib
import shutil
from collections import namedtuple
from datetime import timedelta

import psutil
import pytest
from fastapi.testclient import TestClient
from tests.auth_helpers import EDITOR_PASSWORD, add_editor, admin_headers, login
from tests.inspection_helpers import minimal_png

from ctrlstore.config import Settings
from ctrlstore.models import UploadSession, utcnow
from ctrlstore.storage import LocalStorage
from ctrlstore.uploads import service as uploads

API = "/api/admin/v1"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定成磁盘与内存都充裕，避免用例受本机状态影响。

    需要验证「磁盘满」的用例在测试体内再覆盖一次即可（后设置的补丁生效）。
    """
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


@pytest.fixture
def storage(settings: Settings) -> LocalStorage:
    return LocalStorage.from_settings(settings)


def _create(client: TestClient, filename: str) -> dict:
    response = client.post(
        f"{API}/uploads", json={"filename": filename}, headers=admin_headers(client)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _put(client: TestClient, upload_id: str, index: int, payload: bytes):
    return client.put(
        f"{API}/uploads/{upload_id}/parts/{index}", content=payload, headers=admin_headers(client)
    )


def _complete(client: TestClient, upload_id: str, headers: dict | None = None):
    return client.post(
        f"{API}/uploads/{upload_id}/complete",
        headers={**admin_headers(client), **(headers or {})},
    )


def _direct(client: TestClient, filename: str, payload: bytes, content_type: str | None = None):
    return client.post(
        f"{API}/uploads/direct",
        files={"file": (filename, payload, content_type or "application/octet-stream")},
        headers=admin_headers(client),
    )


# —— 分片流程 ——


def test_chunked_upload_reaches_ready(owner_client: TestClient, storage: LocalStorage) -> None:
    png = minimal_png()
    session = _create(owner_client, "cat.png")

    assert _put(owner_client, session["id"], 1, png[:100]).status_code == 200
    assert _put(owner_client, session["id"], 2, png[100:]).status_code == 200
    done = _complete(owner_client, session["id"])

    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "ready"
    assert body["kind"] == "image"
    assert body["sha256"] == hashlib.sha256(png).hexdigest()
    assert body["byte_size"] == len(png)
    assert body["asset_id"] and body["storage_key"]
    stored = storage.original_path(body["storage_key"])
    assert stored.is_file()
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == body["sha256"]
    # 完成后临时目录清空
    assert not storage.session_dir(session["id"]).exists()


def test_missing_part_blocks_completion(owner_client: TestClient, storage: LocalStorage) -> None:
    session = _create(owner_client, "a.txt")
    _put(owner_client, session["id"], 1, b"hello ")
    _put(owner_client, session["id"], 3, b"world")

    response = _complete(owner_client, session["id"])

    assert response.status_code == 409
    assert "缺少分片：2" in response.json()["error"]["message"]
    assert storage.session_dir(session["id"]).exists()


def test_reuploading_a_part_overwrites_it(owner_client: TestClient, storage: LocalStorage) -> None:
    session = _create(owner_client, "a.txt")
    _put(owner_client, session["id"], 1, b"wrong")
    _put(owner_client, session["id"], 1, b"right")

    body = _complete(owner_client, session["id"]).json()

    assert storage.original_path(body["storage_key"]).read_bytes() == b"right"
    assert body["received_parts"] == [1]


def test_part_index_must_start_at_one(owner_client: TestClient) -> None:
    session = _create(owner_client, "a.txt")

    assert _put(owner_client, session["id"], 0, b"x").status_code == 422


def test_completion_twice_is_rejected(owner_client: TestClient) -> None:
    session = _create(owner_client, "a.txt")
    _put(owner_client, session["id"], 1, b"hi")
    assert _complete(owner_client, session["id"]).status_code == 200

    assert _complete(owner_client, session["id"]).status_code == 409


def test_cancel_removes_temp_files(owner_client: TestClient, storage: LocalStorage) -> None:
    session = _create(owner_client, "a.txt")
    _put(owner_client, session["id"], 1, b"hi")

    response = owner_client.delete(
        f"{API}/uploads/{session['id']}", headers=admin_headers(owner_client)
    )

    assert response.status_code == 204
    assert not storage.session_dir(session["id"]).exists()


def test_unknown_session_is_404(owner_client: TestClient) -> None:
    response = owner_client.get(f"{API}/uploads/does-not-exist")

    assert response.status_code == 404


# —— 一次性上传 ——


def test_direct_upload_stores_bytes(owner_client: TestClient, storage: LocalStorage) -> None:
    png = minimal_png(width=32, height=16)

    response = _direct(owner_client, "direct.png", png, "image/png")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert (body["width"], body["height"]) == (32, 16)
    assert storage.original_path(body["storage_key"]).read_bytes() == png


@pytest.mark.parametrize(
    ("filename", "payload", "content_type"),
    [
        ("evil.html", b"<html><script>alert(1)</script></html>", "text/html"),
        ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
        ("bundle.zip", b"PK\x03\x04payload", "application/zip"),
        ("tool.exe", b"MZ\x90\x00payload", "application/octet-stream"),
    ],
)
def test_direct_upload_rejects_dangerous_files(
    owner_client: TestClient, filename: str, payload: bytes, content_type: str
) -> None:
    response = _direct(owner_client, filename, payload, content_type)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid"


def test_extension_mismatch_is_rejected_end_to_end(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    response = _direct(owner_client, "fake.jpg", minimal_png(), "image/jpeg")

    assert response.status_code == 422
    assert "不符" in response.json()["error"]["message"]
    assert not storage.originals_dir.exists() or not any(storage.originals_dir.rglob("*"))


def test_direct_upload_leaves_no_temp_files(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    _direct(owner_client, "ok.png", minimal_png(), "image/png")

    uploads_dir = storage.tmp_dir / "uploads"
    assert not uploads_dir.exists() or not any(uploads_dir.iterdir())


# —— 上限与磁盘 ——


def test_oversized_part_is_rejected(owner_client: TestClient, settings: Settings) -> None:
    session = _create(owner_client, "a.md")

    response = _put(owner_client, session["id"], 1, b"x" * (settings.limits.markdown + 10))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"


def test_upload_refused_when_disk_is_full(
    owner_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: _DiskUsage(1000, 1000, 0))

    response = owner_client.post(
        f"{API}/uploads", json={"filename": "a.txt"}, headers=admin_headers(owner_client)
    )

    assert response.status_code == 507
    assert response.json()["error"]["code"] == "insufficient_storage"


def test_open_session_limit_per_user(owner_client: TestClient, plenty_of_space) -> None:
    _create(owner_client, "a.txt")
    _create(owner_client, "b.txt")

    response = owner_client.post(
        f"{API}/uploads", json={"filename": "c.txt"}, headers=admin_headers(owner_client)
    )

    assert response.status_code == 409
    assert "2 个" in response.json()["error"]["message"]


def test_open_session_limit_is_global(
    owner_client: TestClient, db, plenty_of_space, settings: Settings, storage: LocalStorage
) -> None:
    """全库 4 个：两个编辑各占 2 个之后，第五个（无论谁）都被挡住。"""
    editor_a = add_editor(db, username="a", permissions=["asset.upload"])
    editor_b = add_editor(db, username="b", permissions=["asset.upload"])
    for actor in (editor_a, editor_b):
        for index in range(2):
            uploads.create_session(
                db,
                settings=settings,
                storage=storage,
                actor=actor,
                filename=f"{actor.username}{index}.txt",
                declared_type=None,
            )
    db.commit()

    response = owner_client.post(
        f"{API}/uploads", json={"filename": "fifth.txt"}, headers=admin_headers(owner_client)
    )

    assert response.status_code == 409
    assert "全库" in response.json()["error"]["message"]


# —— 幂等 ——


def test_complete_replays_with_same_key(owner_client: TestClient) -> None:
    session = _create(owner_client, "a.txt")
    _put(owner_client, session["id"], 1, b"hello")

    first = _complete(owner_client, session["id"], {"Idempotency-Key": "k-1"})
    second = _complete(owner_client, session["id"], {"Idempotency-Key": "k-1"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["storage_key"] == first.json()["storage_key"]
    assert second.json()["asset_id"] == first.json()["asset_id"]


# —— 权限与隔离 ——


def test_editor_without_upload_permission_cannot_create(client: TestClient, db) -> None:
    add_editor(db, username="reader", permissions=["asset.read"])
    login(client, "reader", EDITOR_PASSWORD)

    response = client.post(
        f"{API}/uploads", json={"filename": "a.txt"}, headers=admin_headers(client)
    )

    assert response.status_code == 403
    assert "asset.upload" in response.json()["error"]["message"]


def test_editor_cannot_read_another_editors_session(
    owner_client: TestClient, client: TestClient, db
) -> None:
    session = _create(owner_client, "a.txt")
    add_editor(db, username="snoop", permissions=["asset.upload"])

    client.cookies.clear()
    login(client, "snoop", EDITOR_PASSWORD)
    response = client.get(f"{API}/uploads/{session['id']}", headers=admin_headers(client))

    assert response.status_code == 404


def test_editor_cancel_another_editors_session_is_404(
    owner_client: TestClient, client: TestClient, db
) -> None:
    session = _create(owner_client, "a.txt")
    add_editor(db, username="snoop", permissions=["asset.upload"])

    client.cookies.clear()
    login(client, "snoop", EDITOR_PASSWORD)
    response = client.delete(f"{API}/uploads/{session['id']}", headers=admin_headers(client))

    assert response.status_code == 404


# —— 过期清理 ——


def test_expired_sessions_are_cleaned_up(
    owner_client: TestClient, storage: LocalStorage, plenty_of_space
) -> None:
    session = _create(owner_client, "old.txt")
    _put(owner_client, session["id"], 1, b"hi")

    with owner_client.app.state.session_factory() as db:
        row = db.get(UploadSession, session["id"])
        row.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()

    _create(owner_client, "new.txt")  # 建会话时顺手清理

    with owner_client.app.state.session_factory() as db:
        assert db.get(UploadSession, session["id"]).status == "expired"
    assert not storage.session_dir(session["id"]).exists()


def test_same_bytes_are_reported_as_duplicate(owner_client: TestClient) -> None:
    png = minimal_png()
    first = _direct(owner_client, "one.png", png, "image/png").json()

    second = _direct(owner_client, "two.png", png, "image/png").json()

    assert second["id"] != first["id"]
    assert second["duplicate_of"] == first["id"]
