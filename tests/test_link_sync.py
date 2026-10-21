"""重新发布时不出现公开链接空窗（验收建议 1）。"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from tests.auth_helpers import admin_headers
from tests.inspection_helpers import minimal_png
from tests.test_publish import API, _publish

from ctrlstore.storage import LocalStorage

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def _replace(client: TestClient, asset_id: str, payload: bytes):
    return client.post(
        f"{API}/uploads/direct",
        files={"file": ("v2.png", payload, "image/png")},
        data={"asset_id": asset_id},
        headers=admin_headers(client),
    )


def test_republish_replaces_links_without_clearing(
    owner_client: TestClient, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("v1.png", minimal_png(10, 10), "image/png")},
        headers=admin_headers(owner_client),
    ).json()
    asset_id = created["asset_id"]
    _publish(owner_client, asset_id)
    current = storage.public_current_path(asset_id, "png")
    first_bytes = current.read_bytes()

    replaced = _replace(owner_client, asset_id, minimal_png(24, 24))
    assert replaced.status_code == 201, replaced.text
    detail = owner_client.get(f"{API}/assets/{asset_id}").json()

    # 留下的旧文件模拟上一版扩展名/旧链接；重新发布应删掉它。
    stale = storage.public_dir_for(asset_id) / "old.jpg"
    stale.write_bytes(b"stale")

    def _forbidden_clear(self, target_id):
        raise AssertionError("重新发布不应清空整个公开目录")

    monkeypatch.setattr(LocalStorage, "clear_public", _forbidden_clear)

    response = owner_client.post(
        f"{API}/assets/{asset_id}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200, response.text
    assert current.read_bytes() != first_bytes
    assert current.is_file()
    assert not stale.exists()
