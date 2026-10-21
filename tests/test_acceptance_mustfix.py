"""验收「必须修改」六条的回归测试。

对应 `plan/问题总结.md`：配额、检索截断、已发布内容定时更新、内存阈值、
发布/撤回幂等，以及备份脚本的关键顺序。
另覆盖 `plan/低内存队列阻塞.md`：解码等待期间，定时发布与清理仍可执行。
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
from tests.auth_helpers import (
    EDITOR_PASSWORD,
    add_editor,
    admin_headers,
    login,
)
from tests.inspection_helpers import minimal_png
from tests.test_public_api import _site, _site_client, _token
from tests.test_publish import _publish, _text

from ctrlstore import quota
from ctrlstore.assets import publish
from ctrlstore.errors import ApiError
from ctrlstore.jobs import service as jobs
from ctrlstore.models import Asset, Derivative, Job, UploadSession, utcnow
from ctrlstore.uploads import service as uploads
from ctrlstore.worker import run_until_idle

API = "/api/admin/v1"
PUBLIC = "/api/public/v1"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def _upload_image(client: TestClient, *, asset_id: str | None = None):
    data = {"asset_id": asset_id} if asset_id else None
    return client.post(
        f"{API}/uploads/direct",
        files={"file": ("pic.png", minimal_png(16, 16), "image/png")},
        data=data,
        headers=admin_headers(client),
    )


# —— 1. 配额 ——


def test_body_edit_respects_quota(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    article = _text(owner_client, body="# 标题\n\n初始正文")
    used = quota.usage_bytes(db)
    client.app.state.settings.user_quota_bytes = used + 5

    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    response = owner_client.patch(
        f"{API}/assets/{article['id']}",
        json={"revision": detail["revision"], "body": "# 标题\n\n这段正文明显超过剩余额度"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 507
    # 失败不应改变修订号，也不应写入新版本。
    after = owner_client.get(f"{API}/assets/{article['id']}").json()
    assert after["revision"] == detail["revision"]
    assert after["current_version"]["id"] == detail["current_version"]["id"]


def test_editor_replacement_charges_content_owner(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    created = _upload_image(owner_client)
    assert created.status_code == 201, created.text
    used = quota.usage_bytes(db)
    client.app.state.settings.user_quota_bytes = used + 10

    add_editor(db, username="editor", permissions=("asset.update",))
    login(client, "editor", EDITOR_PASSWORD)

    response = _upload_image(client, asset_id=created.json()["asset_id"])

    # 替换的字节记在内容所有者名下；编辑自己的额度再大也挡不住。
    assert response.status_code == 507


# —— 2. 检索先收窄再截断 ——


def test_site_search_narrows_before_limit(
    owner_client: TestClient, client: TestClient, settings
) -> None:
    client.app.state.settings.site_search_enabled = True
    _site(owner_client, "main")
    token = _token(owner_client, "main")
    keyword = "拥堵测试关键词"

    # 60 条「已发布 + 私有」正文含同一个词，足以占满 50 条名额。
    for index in range(60):
        article = _text(owner_client, body=f"# 私有 {index}\n\n{keyword}")
        response = owner_client.post(
            f"{API}/assets/{article['id']}/publish",
            json={"revision": article["revision"]},
            headers=admin_headers(owner_client),
        )
        assert response.status_code == 200, response.text
    visible = _text(owner_client, body=f"# 公开\n\n{keyword}")
    _publish(owner_client, visible["id"])
    run_until_idle(settings)

    result = _site_client(client, token).get(
        f"{PUBLIC}/search", params={"q": keyword}
    ).json()

    ids = {item["id"] for item in result["items"]}
    assert visible["id"] in ids
    assert len(result["items"]) <= 50


def test_admin_search_excludes_trash_before_limit(
    owner_client: TestClient, settings
) -> None:
    keyword = "回收站拥堵关键词"
    trashed: list[dict] = []
    for index in range(60):
        trashed.append(_text(owner_client, body=f"# 待删 {index}\n\n{keyword}"))
    run_until_idle(settings)
    for article in trashed:
        response = owner_client.post(
            f"{API}/assets/{article['id']}/trash",
            json={"revision": article["revision"]},
            headers=admin_headers(owner_client),
        )
        assert response.status_code == 200, response.text
    alive = _text(owner_client, body=f"# 保留\n\n{keyword}")
    run_until_idle(settings)

    result = owner_client.get(
        f"{API}/search", params={"q": keyword}
    ).json()

    assert alive["id"] in {item["id"] for item in result["items"]}


# —— 3. 已发布内容的定时更新 ——


def test_scheduled_update_of_published_asset(
    owner_client: TestClient,
    client: TestClient,
    db: DbSession,
    settings,
) -> None:
    article = _text(owner_client, body="# 文章\n\n第一版正文")
    run_until_idle(settings)
    _publish(owner_client, article["id"])
    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    old_version = detail["published_version_id"]

    updated = owner_client.patch(
        f"{API}/assets/{article['id']}",
        json={"revision": detail["revision"], "body": "# 文章\n\n第二版正文"},
        headers=admin_headers(owner_client),
    ).json()
    when = (utcnow() + timedelta(hours=1)).isoformat(timespec="seconds") + "Z"
    scheduled = owner_client.post(
        f"{API}/assets/{article['id']}/publish",
        json={"revision": updated["revision"], "publish_at": when},
        headers=admin_headers(owner_client),
    )
    assert scheduled.status_code == 200, scheduled.text
    assert scheduled.json()["published_version_id"] == old_version

    db.expire_all()
    asset = db.get(Asset, article["id"])
    asset.publish_at = utcnow() - timedelta(minutes=1)
    # 直接补一个到点任务：周期任务在 1 分钟内不重复入队，测试不等这一分钟。
    jobs.enqueue(db, jobs.KIND_PUBLISH_DUE)
    db.commit()

    run_until_idle(settings)

    db.expire_all()
    asset = db.get(Asset, article["id"])
    assert asset.publish_at is None
    assert asset.published_version_id == asset.current_version_id
    assert asset.published_version_id != old_version
    body = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]
    assert "第二版正文" in body


# —— 4. 内存阈值 ——


def test_low_memory_rejects_upload(
    owner_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(100 * 1024**2))

    response = _upload_image(owner_client)

    assert response.status_code == 507
    assert response.json()["error"]["code"] == "insufficient_storage"


def test_low_memory_rejects_merge(owner_client: TestClient, db: DbSession, monkeypatch) -> None:
    response = owner_client.post(
        f"{API}/uploads", json={"filename": "pic.png"}, headers=admin_headers(owner_client)
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    part = owner_client.put(
        f"{API}/uploads/{session_id}/parts/1",
        content=minimal_png(16, 16),
        headers=admin_headers(owner_client),
    )
    assert part.status_code == 200, part.text
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(100 * 1024**2))

    response = owner_client.post(
        f"{API}/uploads/{session_id}/complete", headers=admin_headers(owner_client)
    )

    assert response.status_code == 507
    assert response.json()["error"]["code"] == "insufficient_storage"
    db.expire_all()
    assert db.get(UploadSession, session_id).status == "receiving"
    assert db.scalar(select(Asset.id)) is None


def test_low_memory_rejects_import(db, owner, settings, storage, tmp_path, monkeypatch) -> None:
    path = tmp_path / "pic.png"
    path.write_bytes(minimal_png(16, 16))
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(100 * 1024**2))

    with pytest.raises(ApiError) as error:
        uploads.import_file(db, settings=settings, storage=storage, actor=owner, path=path)

    assert error.value.status_code == 507
    assert error.value.code == "insufficient_storage"
    assert db.scalar(select(UploadSession.id)) is None
    assert db.scalar(select(Asset.id)) is None


def test_low_memory_defers_decode_but_keeps_public_read(
    owner_client: TestClient, client: TestClient, db: DbSession, settings, storage, monkeypatch
) -> None:
    created = _upload_image(owner_client)
    asset_id = created.json()["asset_id"]
    _publish(owner_client, asset_id)

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(100 * 1024**2))
    processed = run_until_idle(settings)

    # 解码等待期间，周期发布与清理仍会被处理。
    assert processed == 2
    db.expire_all()
    derivatives = db.scalars(
        select(Derivative).where(Derivative.status != "ready")
    ).all()
    assert derivatives and all(item.status == "pending" for item in derivatives)
    deferred = db.scalars(
        select(Job).where(Job.kind.in_((jobs.KIND_THUMB, jobs.KIND_DISPLAY)))
    ).all()
    assert deferred
    assert all(item.status == "pending" and item.attempts == 0 for item in deferred)
    # 已发布文件仍可读。
    assert client.get(f"{PUBLIC}/assets/{asset_id}").status_code == 200

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))
    run_until_idle(settings)
    db.expire_all()
    ready = db.scalars(
        select(Derivative).where(Derivative.status == "ready")
    ).all()
    assert ready

    # 再次低内存时仍允许撤回，原图和派生的公开链接一起摘除。
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(100 * 1024**2))
    detail = owner_client.get(f"{API}/assets/{asset_id}").json()
    response = owner_client.post(
        f"{API}/assets/{asset_id}/unpublish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 200, response.text
    assert client.get(f"{PUBLIC}/assets/{asset_id}").status_code == 404
    assert not storage.public_files(asset_id)


@pytest.mark.parametrize("visibility", ["public", "unlisted", "site", "private"])
def test_low_memory_does_not_block_scheduled_publish(
    owner_client: TestClient, db: DbSession, database, settings, storage, monkeypatch, visibility
) -> None:
    """队首图片等待内存时，到点发布仍先提交快照再挂链接；恢复后补齐派生。"""
    created = _upload_image(owner_client)
    assert created.status_code == 201, created.text
    asset_id = created.json()["asset_id"]
    detail = owner_client.get(f"{API}/assets/{asset_id}").json()
    updated = owner_client.patch(
        f"{API}/assets/{asset_id}",
        json={
            "revision": detail["revision"],
            "visibility": visibility,
            "title": "低内存时的定时发布",
            "slug": "low-memory-publish",
        },
        headers=admin_headers(owner_client),
    )
    assert updated.status_code == 200, updated.text
    when = (utcnow() + timedelta(hours=1)).isoformat(timespec="seconds") + "Z"
    scheduled = owner_client.post(
        f"{API}/assets/{asset_id}/publish",
        json={"revision": updated.json()["revision"], "publish_at": when},
        headers=admin_headers(owner_client),
    )
    assert scheduled.status_code == 200, scheduled.text

    db.expire_all()
    asset = db.get(Asset, asset_id)
    asset.publish_at = utcnow() - timedelta(minutes=1)
    due = jobs.enqueue(db, jobs.KIND_PUBLISH_DUE)
    db.commit()
    decodes = list(db.scalars(select(Job).where(Job.kind.like("image.%"))))
    assert decodes and all(job.created_at < due.created_at for job in decodes)

    sync_links = publish.sync_links
    linked_assets = []

    def check_committed_snapshot(storage, asset):
        # 用独立会话观察提交结果，不能只检查处理器自己未提交的 ORM 对象。
        with database() as committed:
            stored = committed.get(Asset, asset.id)
            assert stored.status == "published"
            assert stored.published_version_id == stored.current_version_id
            assert stored.published_title == "低内存时的定时发布"
            assert stored.published_slug == "low-memory-publish"
            assert stored.publish_at is None
        linked_assets.append(asset.id)
        return sync_links(storage, asset)

    monkeypatch.setattr(publish, "sync_links", check_committed_snapshot)
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: _Memory(settings.min_available_memory_bytes - 1)
    )

    assert run_until_idle(settings, max_jobs=1) == 1

    db.expire_all()
    assert db.get(Job, due.id).status == "done"
    assert linked_assets == [asset_id]
    assert all(job.status == "pending" and job.attempts == 0 for job in decodes)
    derivatives = list(db.scalars(select(Derivative)))
    assert {item.purpose for item in derivatives} == {"thumb", "display"}
    for item in derivatives:
        assert item.status == "pending"
        assert not storage.derivative_path(item.storage_key).exists()
        assert not storage.public_derivative_path(asset_id, item.purpose).exists()
    public = visibility in {"public", "unlisted"}
    original = storage.public_current_path(asset_id, "png")
    assert original.is_file() == public
    if public:
        assert original.read_bytes() == minimal_png(16, 16)

    # 刚好达到阈值即可恢复，不需重新入队、改变顺序或手工重试。
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: _Memory(settings.min_available_memory_bytes)
    )
    run_until_idle(settings)

    db.expire_all()
    assert all(job.status == "done" and job.attempts == 1 for job in decodes)
    for item in derivatives:
        assert item.status == "ready"
        assert storage.derivative_path(item.storage_key).is_file()
        assert storage.public_derivative_path(asset_id, item.purpose).is_file() == public
    if public:
        urls = owner_client.get(f"{PUBLIC}/assets/{asset_id}").json()["urls"]
        assert {"original", "thumb", "display"} <= urls.keys()


def test_low_memory_does_not_block_upload_cleanup(
    owner_client: TestClient, db: DbSession, settings, storage, monkeypatch
) -> None:
    created = _upload_image(owner_client)
    assert created.status_code == 201, created.text
    response = owner_client.post(
        f"{API}/uploads",
        json={"filename": "expired.png", "content_type": "image/png"},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    db.expire_all()
    session = db.get(UploadSession, session_id)
    session.expires_at = utcnow() - timedelta(minutes=1)
    cleanup = jobs.enqueue(db, jobs.KIND_CLEANUP)
    db.commit()
    assert storage.session_dir(session_id).exists()
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(100 * 1024**2))

    assert run_until_idle(settings, max_jobs=1) == 1

    db.expire_all()
    assert db.get(Job, cleanup.id).status == "done"
    assert db.get(UploadSession, session_id).status == "expired"
    assert not storage.session_dir(session_id).exists()
    decodes = list(db.scalars(select(Job).where(Job.kind.like("image.%"))))
    assert decodes and all(job.status == "pending" and job.attempts == 0 for job in decodes)
    assert all(item.status == "pending" for item in db.scalars(select(Derivative)))


# —— 5. 发布/撤回幂等 ——


def test_publish_and_unpublish_are_idempotent(owner_client: TestClient) -> None:
    article = _text(owner_client, body="# 幂等\n\n正文")
    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    key = "publish-once"
    headers = {**admin_headers(owner_client), "Idempotency-Key": key}

    first = owner_client.post(
        f"{API}/assets/{article['id']}/publish",
        json={"revision": detail["revision"]},
        headers=headers,
    )
    second = owner_client.post(
        f"{API}/assets/{article['id']}/publish",
        json={"revision": detail["revision"]},
        headers=headers,
    )
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()

    # 同一密钥换请求体 → 409，不再执行第二次。
    conflict = owner_client.post(
        f"{API}/assets/{article['id']}/publish",
        json={"revision": detail["revision"] + 1},
        headers=headers,
    )
    assert conflict.status_code == 409

    current = owner_client.get(f"{API}/assets/{article['id']}").json()
    unpublish_key = "unpublish-once"
    unpublish_headers = {**admin_headers(owner_client), "Idempotency-Key": unpublish_key}
    first_down = owner_client.post(
        f"{API}/assets/{article['id']}/unpublish",
        json={"revision": current["revision"]},
        headers=unpublish_headers,
    )
    second_down = owner_client.post(
        f"{API}/assets/{article['id']}/unpublish",
        json={"revision": current["revision"]},
        headers=unpublish_headers,
    )
    assert first_down.status_code == second_down.status_code == 200
    assert second_down.json() == first_down.json()
