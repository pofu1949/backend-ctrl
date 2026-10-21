"""定时发布（规划第 7 部分）。

到点前不写快照、不进公开区；到点后按第 5 部分的同一套顺序上线：
先提交快照，再按可见性挂链接。发不出去的内容取消定时并写审计，
不会每分钟重试一次。
"""

from __future__ import annotations

import shutil
from collections import namedtuple
from datetime import timedelta

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import admin_headers
from tests.test_publish import API, _image, _public_files, _set_visibility, _text

from ctrlstore import audit
from ctrlstore.models import Asset, AuditLog, utcnow
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


def _schedule(client: TestClient, asset_id: str, *, hours: int = 2) -> dict:
    detail = client.get(f"{API}/assets/{asset_id}").json()
    when = (utcnow() + timedelta(hours=hours)).isoformat(timespec="seconds") + "Z"
    response = client.post(
        f"{API}/assets/{asset_id}/publish",
        json={"revision": detail["revision"], "publish_at": when},
        headers=admin_headers(client),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _make_due(db: DbSession, asset_id: str) -> None:
    """把计划时间改到刚才，模拟时间已经到点。"""
    db.expire_all()
    asset = db.get(Asset, asset_id)
    asset.publish_at = utcnow() - timedelta(minutes=1)
    db.commit()


def test_future_schedule_does_not_touch_public_area(
    owner_client: TestClient, client: TestClient, storage
) -> None:
    created = _image(owner_client)
    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    _set_visibility(owner_client, detail, "public")
    scheduled = _schedule(owner_client, created["asset_id"])

    assert scheduled["status"] == "draft"
    assert scheduled["publish_at"] is not None
    assert scheduled["published_version_id"] is None
    assert _public_files(storage, created["asset_id"]) == set()
    assert client.get(f"/api/public/v1/assets/{created['asset_id']}").status_code == 404


def test_due_schedule_publishes_with_snapshot(
    owner_client: TestClient, client: TestClient, db: DbSession, storage, settings
) -> None:
    created = _image(owner_client)
    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    _set_visibility(owner_client, detail, "public")
    _schedule(owner_client, created["asset_id"])
    _make_due(db, created["asset_id"])

    run_until_idle(settings)

    db.expire_all()
    asset = db.get(Asset, created["asset_id"])
    assert asset.status == "published"
    assert asset.publish_at is None
    assert asset.published_title == asset.title
    assert asset.published_version_id == asset.current_version_id
    assert f"{created['asset_id']}/original.png" in _public_files(storage, created["asset_id"])
    assert client.get(f"/api/public/v1/assets/{created['asset_id']}").status_code == 200


def test_failed_schedule_cancels_and_audits(
    owner_client: TestClient, db: DbSession, settings
) -> None:
    """正文引用了未发布图片：到点也发不出去，取消定时并留下审计。"""
    image = _image(owner_client)
    article = _text(owner_client, body=f"# 文章\n\n![图](asset:{image['asset_id']})")
    _schedule(owner_client, article["id"])
    _make_due(db, article["id"])

    run_until_idle(settings)

    db.expire_all()
    asset = db.get(Asset, article["id"])
    assert asset.status == "draft"
    assert asset.publish_at is None
    actions = db.scalars(
        select(AuditLog.action).where(AuditLog.action == audit.ASSET_PUBLISH_FAILED)
    ).all()
    assert actions == [audit.ASSET_PUBLISH_FAILED]


def test_immediate_publish_clears_schedule(owner_client: TestClient) -> None:
    created = _image(owner_client)
    scheduled = _schedule(owner_client, created["asset_id"])

    response = owner_client.post(
        f"{API}/assets/{created['asset_id']}/publish",
        json={"revision": scheduled["revision"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "published"
    assert body["publish_at"] is None


def test_trash_cancels_schedule(owner_client: TestClient) -> None:
    created = _image(owner_client)
    scheduled = _schedule(owner_client, created["asset_id"])

    response = owner_client.post(
        f"{API}/assets/{created['asset_id']}/trash",
        json={"revision": scheduled["revision"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "trashed"
    assert body["publish_at"] is None
