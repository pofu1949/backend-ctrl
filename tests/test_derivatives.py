"""图片派生与公开区（规划第 7 部分验收项）。

覆盖：上传后自动排缩略图、480 像素 WebP、就绪后补进公开区、
发布前就绪的缩略图随发布一起挂出、生成失败不牵连原图、
仅站点内容不进 ``public/``、撤回摘链接、fsck 补链接、版本淘汰删派生文件。
"""

from __future__ import annotations

import hashlib
import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import admin_headers
from tests.inspection_helpers import minimal_png
from tests.test_publish import API, _image, _public_files, _publish, _set_visibility

from ctrlstore import fsck
from ctrlstore.jobs import service as jobs
from ctrlstore.models import (
    DERIVATIVE_FAILED,
    DERIVATIVE_FILENAMES,
    DERIVATIVE_PENDING,
    DERIVATIVE_READY,
    JOB_DONE,
    JOB_FAILED,
    AssetVersion,
    Derivative,
    Job,
)
from ctrlstore.storage import LocalStorage, keys
from ctrlstore.worker import run_until_idle

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


def _thumbnail(db: DbSession, asset_id: str) -> Derivative:
    db.expire_all()
    found = db.scalar(
        select(Derivative)
        .join(Derivative.version)
        .where(AssetVersion.asset_id == asset_id, Derivative.purpose == "thumb")
    )
    assert found is not None
    return found


def _jobs_of(db: DbSession, kind: str) -> list[Job]:
    db.expire_all()
    return list(db.scalars(select(Job).where(Job.kind == kind)).all())


def _thumb_url(payload: dict) -> str:
    return payload["urls"]["thumb"]


# —— 键与布局 ——


def test_filename_tables_stay_in_sync() -> None:
    """用途 → 文件名只在两处出现，任何一处改了都要一起改。"""
    assert keys.DERIVATIVE_FILENAMES == DERIVATIVE_FILENAMES


def test_derivative_key_rejects_traversal(storage: LocalStorage) -> None:
    with pytest.raises(keys.InvalidStorageKey):
        storage.derivative_path("aa/../../etc/passwd")


# —— 生成 ——


def test_upload_schedules_thumbnail_without_waiting(
    owner_client: TestClient, db: DbSession
) -> None:
    """图片入库后立刻有 pending 记录与任务；缩略图没做完不影响内容可用。"""
    created = _image(owner_client, "pic.png", minimal_png(40, 40))
    derivative = _thumbnail(db, created["asset_id"])

    assert derivative.status == DERIVATIVE_PENDING
    assert [job.kind for job in _jobs_of(db, jobs.KIND_THUMB)] == [jobs.KIND_THUMB]


def test_worker_writes_480px_webp(
    owner_client: TestClient, db: DbSession, settings, storage
) -> None:
    created = _image(owner_client, "wide.png", minimal_png(1200, 600))

    assert run_until_idle(settings) >= 1

    derivative = _thumbnail(db, created["asset_id"])
    assert derivative.status == DERIVATIVE_READY
    path = storage.derivative_path(derivative.storage_key)
    assert path.is_file()
    with Image.open(path) as image:
        assert image.format == "WEBP"
        # 最长边压到 480，宽高比保持不变。
        assert max(image.size) == 480
        assert image.size == (480, 240)
    assert _jobs_of(db, jobs.KIND_THUMB)[0].status == JOB_DONE


def test_publish_links_ready_thumbnail(
    owner_client: TestClient, client: TestClient, db: DbSession, storage
) -> None:
    created = _image(owner_client)
    run_until_idle(owner_client.app.state.settings)
    published = _publish(owner_client, created["asset_id"])

    assert _public_files(storage, created["asset_id"]) == {
        f"{created['asset_id']}/original.png",
        f"{created['asset_id']}/v/1/original.png",
        f"{created['asset_id']}/thumb.webp",
    }

    payload = client.get(f"/api/public/v1/assets/{created['asset_id']}").json()
    assert set(payload["urls"]) == {"original", "thumb"}
    assert _thumb_url(payload).endswith(f"/p/{created['asset_id']}/thumb.webp")
    assert published["status"] == "published"


def test_thumbnail_finishing_after_publish_is_linked(
    owner_client: TestClient, client: TestClient, db: DbSession, storage, settings
) -> None:
    created = _image(owner_client)
    _publish(owner_client, created["asset_id"])

    # 发布时缩略图还没生成：公开 JSON 只有原图，公开区没有 thumb。
    payload = client.get(f"/api/public/v1/assets/{created['asset_id']}").json()
    assert set(payload["urls"]) == {"original"}
    assert f"{created['asset_id']}/thumb.webp" not in _public_files(storage, created["asset_id"])

    run_until_idle(settings)

    payload = client.get(f"/api/public/v1/assets/{created['asset_id']}").json()
    assert set(payload["urls"]) == {"original", "thumb"}
    assert f"{created['asset_id']}/thumb.webp" in _public_files(storage, created["asset_id"])


def test_thumbnail_failure_keeps_original_available(
    owner_client: TestClient, client: TestClient, db: DbSession, storage, settings
) -> None:
    """派生失败只把状态记成 failed：原图照常发布，响应里没有 thumb。"""
    created = _image(owner_client)
    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    version = detail["current_version"]
    # 把原件弄坏，模拟无法解码的字节。
    original = storage.original_path(version["storage_key"])
    original.write_bytes(b"not an image")

    run_until_idle(settings)

    derivative = _thumbnail(db, created["asset_id"])
    assert derivative.status == DERIVATIVE_FAILED
    assert _jobs_of(db, jobs.KIND_THUMB)[0].status == JOB_FAILED

    _publish(owner_client, created["asset_id"])

    payload = client.get(f"/api/public/v1/assets/{created['asset_id']}").json()
    assert set(payload["urls"]) == {"original"}
    files = _public_files(storage, created["asset_id"])
    assert f"{created['asset_id']}/original.png" in files
    assert f"{created['asset_id']}/thumb.webp" not in files


def test_site_only_image_never_reaches_public_dir(
    owner_client: TestClient, db: DbSession, storage, settings
) -> None:
    created = _image(owner_client)
    run_until_idle(settings)
    assert _thumbnail(db, created["asset_id"]).status == DERIVATIVE_READY

    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    detail = _set_visibility(owner_client, detail, "site")
    response = owner_client.post(
        f"{API}/assets/{created['asset_id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 200, response.text

    # 缩略图已就绪，但「仅站点」不进公开区：目录里连一个文件都没有。
    assert _public_files(storage, created["asset_id"]) == set()


def test_unpublish_removes_thumbnail_link(
    owner_client: TestClient, db: DbSession, storage
) -> None:
    created = _image(owner_client)
    run_until_idle(owner_client.app.state.settings)
    published = _publish(owner_client, created["asset_id"])
    assert _public_files(storage, created["asset_id"])

    response = owner_client.post(
        f"{API}/assets/{created['asset_id']}/unpublish",
        json={"revision": published["revision"]},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 200, response.text
    assert _public_files(storage, created["asset_id"]) == set()


def test_replacing_published_image_keeps_old_thumbnail_until_publish(
    owner_client: TestClient, db: DbSession, storage, settings
) -> None:
    created = _image(owner_client, "wide.png", minimal_png(1200, 600))
    run_until_idle(settings)
    published = _publish(owner_client, created["asset_id"])
    public_thumb = storage.public_derivative_path(created["asset_id"], "thumb")
    before = hashlib.sha256(public_thumb.read_bytes()).hexdigest()

    replaced = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("square.png", minimal_png(400, 400), "image/png")},
        data={"asset_id": created["asset_id"]},
        headers=admin_headers(owner_client),
    )
    assert replaced.status_code == 201, replaced.text
    run_until_idle(settings)

    # v2 的缩略图已生成，但线上仍是 v1 那一版：公开文件不变。
    assert hashlib.sha256(public_thumb.read_bytes()).hexdigest() == before

    response = owner_client.post(
        f"{API}/assets/{created['asset_id']}/publish",
        json={"revision": published["revision"] + 1},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 200, response.text
    with Image.open(public_thumb) as image:
        assert image.size == (400, 400)


def test_fsck_reports_and_fixes_missing_thumbnail(
    owner_client: TestClient, db: DbSession, storage
) -> None:
    created = _image(owner_client)
    run_until_idle(owner_client.app.state.settings)
    _publish(owner_client, created["asset_id"])
    thumb = storage.public_derivative_path(created["asset_id"], "thumb")
    thumb.unlink()

    report = fsck.run(db, storage=storage, check_hashes=False)
    missing = [issue for issue in report.issues if issue.code == fsck.MISSING_LINK]
    assert any("thumb.webp" in issue.detail for issue in missing)

    fsck.run(db, storage=storage, fix_public_links=True, check_hashes=False)
    assert thumb.is_file()


def test_pruning_a_version_removes_its_derivatives(
    owner_client: TestClient, db: DbSession, storage, settings
) -> None:
    created = _image(owner_client, "v1.png", minimal_png(10, 10))
    for index in range(2, 7):
        response = owner_client.post(
            f"{API}/uploads/direct",
            files={"file": (f"v{index}.png", minimal_png(10 + index, 10), "image/png")},
            data={"asset_id": created["asset_id"]},
            headers=admin_headers(owner_client),
        )
        assert response.status_code == 201, response.text
        run_until_idle(settings)

    db.expire_all()
    versions = owner_client.get(f"{API}/assets/{created['asset_id']}").json()["versions"]
    # 留最近 5 版：v1 被淘汰，它的派生目录也一起删掉。
    assert [item["version_no"] for item in versions] == [2, 3, 4, 5, 6]
    assert not storage.derivative_version_dir(created["asset_id"], 1).exists()
    assert storage.derivative_version_dir(created["asset_id"], 6).exists()


def test_purge_removes_derivative_files(
    owner_client: TestClient, db: DbSession, storage
) -> None:
    created = _image(owner_client)
    run_until_idle(owner_client.app.state.settings)
    assert _thumbnail(db, created["asset_id"]).status == DERIVATIVE_READY
    derivative_dir = storage.derivative_version_dir(created["asset_id"], 1)
    assert derivative_dir.exists()

    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    owner_client.post(
        f"{API}/assets/{created['asset_id']}/trash",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )
    response = owner_client.delete(
        f"{API}/assets/{created['asset_id']}", headers=admin_headers(owner_client)
    )
    assert response.status_code == 204, response.text
    assert not derivative_dir.exists()
