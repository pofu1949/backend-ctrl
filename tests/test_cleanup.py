"""worker 的到期清理（规划第 7 部分）。

清理只删「按已提交状态可以删」的东西：回收站超过保留天数、
过期未完成的上传、过旧的已完成任务记录。仍被引用的回收站内容会跳过，
留给所有者先解除引用。
"""

from __future__ import annotations

import shutil
from collections import namedtuple
from datetime import timedelta

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import admin_headers
from tests.test_publish import API, _image

from ctrlstore.jobs import service as jobs
from ctrlstore.models import JOB_DONE, Asset, Job, UploadSession, utcnow
from ctrlstore.storage import LocalStorage
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


def _trash(client: TestClient, asset_id: str) -> None:
    detail = client.get(f"{API}/assets/{asset_id}").json()
    response = client.post(
        f"{API}/assets/{asset_id}/trash",
        json={"revision": detail["revision"]},
        headers=admin_headers(client),
    )
    assert response.status_code == 200, response.text


def _age_trash(db: DbSession, asset_id: str, *, days: int = 31) -> None:
    db.expire_all()
    asset = db.get(Asset, asset_id)
    asset.trashed_at = utcnow() - timedelta(days=days)
    db.commit()


def test_expired_trash_is_purged(
    owner_client: TestClient, db: DbSession, storage, settings
) -> None:
    created = _image(owner_client)
    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    original = storage.original_path(detail["current_version"]["storage_key"])
    _trash(owner_client, created["asset_id"])
    _age_trash(db, created["asset_id"])

    run_until_idle(settings)

    db.expire_all()
    assert db.get(Asset, created["asset_id"]) is None
    assert not original.exists()


def test_trash_still_referenced_is_kept(
    owner_client: TestClient, db: DbSession, settings
) -> None:
    """回收站内容仍是别人的封面：清理跳过，等所有者先解除引用。"""
    cover_me = _image(owner_client, "hero.png")
    article = _image(owner_client, "article.png")
    detail = owner_client.get(f"{API}/assets/{article['asset_id']}").json()
    response = owner_client.patch(
        f"{API}/assets/{article['asset_id']}",
        json={"revision": detail["revision"], "cover_asset_id": cover_me["asset_id"]},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 200, response.text

    _trash(owner_client, cover_me["asset_id"])
    _age_trash(db, cover_me["asset_id"])

    run_until_idle(settings)

    db.expire_all()
    assert db.get(Asset, cover_me["asset_id"]) is not None


def test_expired_upload_session_is_cleaned(
    owner_client: TestClient, db: DbSession, storage, settings
) -> None:
    response = owner_client.post(
        f"{API}/uploads",
        json={"filename": "late.png", "content_type": "image/png"},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]

    db.expire_all()
    session = db.get(UploadSession, session_id)
    session.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert storage.session_dir(session_id).exists()

    run_until_idle(settings)

    db.expire_all()
    assert db.get(UploadSession, session_id).status == "expired"
    assert not storage.session_dir(session_id).exists()


def test_old_done_jobs_are_removed(db: DbSession, settings) -> None:
    job = jobs.enqueue(db, jobs.KIND_CLEANUP)
    job.status = JOB_DONE
    job.created_at = utcnow() - timedelta(days=40)
    job.updated_at = job.created_at
    db.commit()
    job_id = job.id

    run_until_idle(settings)

    db.expire_all()
    assert db.get(Job, job_id) is None
