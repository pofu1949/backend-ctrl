"""公开链接必须指向「已发布版本」，不是「当前版本」。

发布之后又替换了文件时，两条指针指向不同的行。这一段曾经把 current_version
传进 desired_links，导致 fsck 与改可见性把仍在线上的一版当成多余链接删掉。
"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.test_publish import (
    API,
    _image,
    _jpeg,
    _public_files,
    _publish,
    _set_visibility,
)

from ctrlstore import fsck
from ctrlstore.storage import LocalStorage

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


@pytest.fixture
def storage(settings) -> LocalStorage:
    return LocalStorage.from_settings(settings)


def _replace(
    client: TestClient, asset_id: str, name: str, payload: bytes, content_type: str
) -> dict:
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": (name, payload, content_type)},
        headers={"X-CSRF-Token": client.get(f"{API}/me").json()["csrf_token"]},
        data={"asset_id": asset_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _replaced_but_published(client: TestClient, storage: LocalStorage) -> tuple[dict, dict]:
    """发布 v1 之后再替换成 v2：线上仍是 v1，current 已是 v2。"""
    created = _image(client, "v1.png")
    published = _publish(client, created["asset_id"])
    replaced = _replace(client, created["asset_id"], "v2.png", _png(24), "image/png")
    detail = client.get(f"{API}/assets/{created['asset_id']}").json()
    assert detail["current_version"]["version_no"] == 2
    assert detail["published_version_id"] == published["published_version_id"]
    assert replaced["asset_id"] == created["asset_id"]
    return created, detail


def _png(size: int) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (0, 90, 180)).save(buffer, format="PNG")
    return buffer.getvalue()


# —— fsck ——


def test_fsck_keeps_links_when_pending_replacement_exists(
    owner_client: TestClient, db: DbSession, storage: LocalStorage
) -> None:
    created, _ = _replaced_but_published(owner_client, storage)
    before = _public_files(storage, created["asset_id"])
    assert before

    report = fsck.run(db, storage=storage)

    assert report.ok, report.issues
    assert _public_files(storage, created["asset_id"]) == before


def test_fsck_fix_does_not_delete_live_links_after_replacement(
    owner_client: TestClient, db: DbSession, storage: LocalStorage
) -> None:
    created, _ = _replaced_but_published(owner_client, storage)
    before = _public_files(storage, created["asset_id"])

    fsck.run(db, storage=storage, fix_public_links=True)

    assert _public_files(storage, created["asset_id"]) == before
    assert storage.public_current_path(created["asset_id"], "png").is_file()


def test_fsck_restores_links_of_published_version_after_replacement(
    owner_client: TestClient, db: DbSession, storage: LocalStorage
) -> None:
    """缺链接时补回来的必须是已发布那一版的文件。"""
    created, detail = _replaced_but_published(owner_client, storage)
    published_key = next(
        version["storage_key"]
        for version in detail["versions"]
        if version["id"] == detail["published_version_id"]
    )
    published_bytes = storage.original_path(published_key).read_bytes()
    storage.clear_public(created["asset_id"])

    fsck.run(db, storage=storage, fix_public_links=True)

    restored = storage.public_current_path(created["asset_id"], "png")
    assert restored.is_file()
    assert restored.read_bytes() == published_bytes


def test_fsck_treats_old_extension_as_live_until_republished(
    owner_client: TestClient, db: DbSession, storage: LocalStorage
) -> None:
    """线上是 .png，待发布是 .jpg：.png 链接不是多余，.jpg 也不该出现。"""
    created = _image(owner_client, "v1.png")
    _publish(owner_client, created["asset_id"])
    _replace(owner_client, created["asset_id"], "v2.jpg", _jpeg(), "image/jpeg")

    report = fsck.run(db, storage=storage, fix_public_links=True)

    assert report.ok, report.issues
    assert storage.public_current_path(created["asset_id"], "png").is_file()
    assert not storage.public_current_path(created["asset_id"], "jpg").exists()


# —— 改可见性 ——


def test_visibility_change_keeps_published_links(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    created, _ = _replaced_but_published(owner_client, storage)

    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    after = _set_visibility(owner_client, detail, "public")

    assert after["visibility"] == "public"
    assert storage.public_current_path(created["asset_id"], "png").is_file()


def test_visibility_toggle_restores_published_links(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    """转私有再转回公开：链接按已发布版本重建，不是按待发布版本。"""
    created, detail = _replaced_but_published(owner_client, storage)
    published_key = next(
        version["storage_key"]
        for version in detail["versions"]
        if version["id"] == detail["published_version_id"]
    )
    published_bytes = storage.original_path(published_key).read_bytes()

    toggled = _set_visibility(owner_client, detail, "private")
    assert _public_files(storage, created["asset_id"]) == set()

    restored = _set_visibility(owner_client, toggled, "public")

    assert restored["visibility"] == "public"
    assert storage.public_current_path(created["asset_id"], "png").read_bytes() == published_bytes


def test_visibility_to_unlisted_after_replacement_uses_published_version(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    created, detail = _replaced_but_published(owner_client, storage)

    after = _set_visibility(owner_client, detail, "unlisted")

    assert after["visibility"] == "unlisted"
    assert _public_files(storage, created["asset_id"]) == {
        f"{created['asset_id']}/original.png",
        f"{created['asset_id']}/v/1/original.png",
    }
