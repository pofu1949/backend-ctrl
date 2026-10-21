"""用户配额与全库配额（规划第 9 部分）。

口径：未彻底删除的所有版本字节 + 派生文件字节，含回收站；公开区硬链接不重复计。
上传在开会话时按单文件上限预估一次，合并时按真实字节再检查一次。
"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import (
    EDITOR_PASSWORD,
    add_editor,
    admin_headers,
    login,
)
from tests.inspection_helpers import minimal_png
from tests.test_publish import _public_files, _publish

from ctrlstore import quota
from ctrlstore.storage import LocalStorage
from ctrlstore.worker import run_until_idle

API = "/api/admin/v1"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def _upload(client: TestClient, size: int = 8):
    return client.post(
        f"{API}/uploads/direct",
        files={"file": ("pic.png", minimal_png(size, size), "image/png")},
        headers=admin_headers(client),
    )


def test_user_quota_rejects_before_upload(
    owner_client: TestClient, client: TestClient
) -> None:
    client.app.state.settings.user_quota_bytes = 1024

    response = _upload(owner_client)

    assert response.status_code == 507
    assert response.json()["error"]["code"] == "insufficient_storage"


def test_upload_within_quota_then_rejects(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    image_limit = client.app.state.settings.limits.for_kind("image")
    client.app.state.settings.user_quota_bytes = image_limit + 10

    first = _upload(owner_client)
    assert first.status_code == 201, first.text
    second = _upload(owner_client)
    assert second.status_code == 507

    used = quota.usage_bytes(db)
    assert used > 0


def test_text_creation_respects_quota(owner_client: TestClient, client: TestClient) -> None:
    client.app.state.settings.user_quota_bytes = 10

    response = owner_client.post(
        f"{API}/assets",
        json={"kind": "text", "body": "这段正文明显超过十个字节"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 507


def test_quota_counts_versions_derivatives_and_trash(
    owner_client: TestClient, client: TestClient, db: DbSession, settings
) -> None:
    created = _upload(owner_client, 40)
    assert created.status_code == 201, created.text
    asset_id = created.json()["asset_id"]
    run_until_idle(settings)
    detail = owner_client.get(f"{API}/assets/{asset_id}").json()
    version_bytes = detail["current_version"]["byte_size"]
    before = quota.usage_bytes(db)
    assert before > version_bytes  # 版本 + 缩略图

    _publish(owner_client, asset_id)
    # 公开区是硬链接，不是新字节。
    assert quota.usage_bytes(db) == before

    detail = owner_client.get(f"{API}/assets/{asset_id}").json()
    owner_client.post(
        f"{API}/assets/{asset_id}/trash",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )
    # 回收站仍占用配额；彻底删除后才释放。
    assert quota.usage_bytes(db) == before


def test_library_quota_covers_all_users(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    image_limit = client.app.state.settings.limits.for_kind("image")
    client.app.state.settings.library_quota_bytes = image_limit + 10
    first = _upload(owner_client)
    assert first.status_code == 201, first.text

    add_editor(db, username="writer", permissions=("asset.upload",))
    login(client, "writer", EDITOR_PASSWORD)
    second = _upload(client)

    assert second.status_code == 507


def test_usage_ignores_public_hardlinks(
    owner_client: TestClient, db: DbSession, storage: LocalStorage
) -> None:
    """计数按数据库登记，不按目录里有多少文件——公开链接不算第二份。"""
    created = _upload(owner_client)
    asset_id = created.json()["asset_id"]
    _publish(owner_client, asset_id)
    used = quota.usage_bytes(db)

    assert _public_files(storage, asset_id)
    assert quota.usage_bytes(db) == used
