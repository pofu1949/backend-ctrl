"""发布与公开文件（规划第 5 部分验收项）。

覆盖：可见性矩阵、撤回后 404、公开字节不变、扩展名随版本、回收站与恢复、
引用检查、fsck 报告与修复、签名下载。

注意：内容默认 ``private``，**发布不等于公开**。要让文件出现在 ``public/`` 下，
必须先把可见性设成 ``public`` 或 ``unlisted``（企划第 12.7 节的矩阵）。
"""

from __future__ import annotations

import hashlib
import io
import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import EDITOR_PASSWORD, add_editor, admin_headers, login
from tests.inspection_helpers import minimal_png

from ctrlstore import fsck
from ctrlstore.assets import signing
from ctrlstore.storage import LocalStorage

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


@pytest.fixture
def storage(settings) -> LocalStorage:
    return LocalStorage.from_settings(settings)


# —— 小工具 ——


def _image(client: TestClient, name: str = "pic.png", payload: bytes | None = None) -> dict:
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": (name, payload or minimal_png(), "image/png")},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _text(client: TestClient, body: str = "# 文章\n\n正文", **extra) -> dict:
    payload = {"kind": "markdown", "body": body, **extra}
    response = client.post(f"{API}/assets", json=payload, headers=admin_headers(client))
    assert response.status_code == 201, response.text
    return response.json()


def _act(client: TestClient, asset_id: str, action: str, revision: int):
    return client.post(
        f"{API}/assets/{asset_id}/{action}",
        json={"revision": revision},
        headers=admin_headers(client),
    )


def _set_visibility(client: TestClient, asset: dict, visibility: str) -> dict:
    response = client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": asset["revision"], "visibility": visibility},
        headers=admin_headers(client),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client: TestClient, asset_id: str, *, visibility: str = "public") -> dict:
    """设好可见性再发布。默认可见性是 private，不设就看不出公开文件。"""
    detail = client.get(f"{API}/assets/{asset_id}").json()
    if detail["visibility"] != visibility:
        detail = _set_visibility(client, detail, visibility)
    response = _act(client, asset_id, "publish", detail["revision"])
    assert response.status_code == 200, response.text
    return response.json()


def _public_files(storage: LocalStorage, asset_id: str) -> set[str]:
    return {
        path.relative_to(storage.public_dir).as_posix() for path in storage.public_files(asset_id)
    }


def _jpeg() -> bytes:
    """用 Pillow 生成一张真正的 JPEG（类型识别读文件头）。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


# —— 可见性矩阵 ——


def test_draft_has_no_public_links(owner_client: TestClient, storage: LocalStorage) -> None:
    created = _image(owner_client)

    assert _public_files(storage, created["asset_id"]) == set()


def test_publish_creates_current_and_versioned_links(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    created = _image(owner_client)

    published = _publish(owner_client, created["asset_id"])

    assert published["status"] == "published"
    assert published["published_version_id"] == published["current_version"]["id"]
    assert _public_files(storage, created["asset_id"]) == {
        f"{created['asset_id']}/original.png",
        f"{created['asset_id']}/v/1/original.png",
    }


def test_public_bytes_match_published_version(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    payload = minimal_png(width=40, height=20)
    created = _image(owner_client, "hero.png", payload)
    _publish(owner_client, created["asset_id"])

    path = storage.public_current_path(created["asset_id"], "png")

    assert hashlib.sha256(path.read_bytes()).hexdigest() == created["sha256"]


def test_publish_does_not_expose_private_visibility(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    """已发布 + 私有：状态是 published，但没有公开文件。"""
    created = _image(owner_client)

    published = _publish(owner_client, created["asset_id"], visibility="private")

    assert published["status"] == "published"
    assert _public_files(storage, created["asset_id"]) == set()


def test_publish_does_not_expose_site_visibility(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    created = _image(owner_client)

    published = _publish(owner_client, created["asset_id"], visibility="site")

    assert published["status"] == "published"
    assert _public_files(storage, created["asset_id"]) == set()


def test_unlisted_gets_links(owner_client: TestClient, storage: LocalStorage) -> None:
    created = _image(owner_client)

    _publish(owner_client, created["asset_id"], visibility="unlisted")

    assert storage.public_current_path(created["asset_id"], "png").is_file()


def test_switching_to_private_removes_links(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    created = _image(owner_client)
    published = _publish(owner_client, created["asset_id"])
    assert _public_files(storage, created["asset_id"])

    after = _set_visibility(owner_client, published, "private")

    assert after["status"] == "published"
    assert _public_files(storage, created["asset_id"]) == set()


def test_switching_to_public_adds_links(owner_client: TestClient, storage: LocalStorage) -> None:
    created = _image(owner_client)
    published = _publish(owner_client, created["asset_id"], visibility="private")
    assert _public_files(storage, created["asset_id"]) == set()

    after = _set_visibility(owner_client, published, "public")

    assert after["visibility"] == "public"
    assert storage.public_current_path(created["asset_id"], "png").is_file()


# —— 撤回、归档、回收站 ——


def test_unpublish_removes_links_and_keeps_file(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    created = _image(owner_client)
    published = _publish(owner_client, created["asset_id"])
    storage_key = published["current_version"]["storage_key"]

    after = _act(owner_client, created["asset_id"], "unpublish", published["revision"]).json()

    assert after["status"] == "draft"
    assert after["published_version_id"] is None
    assert _public_files(storage, created["asset_id"]) == set()
    assert storage.original_path(storage_key).is_file()


def test_archive_removes_links(owner_client: TestClient, storage: LocalStorage) -> None:
    created = _image(owner_client)
    published = _publish(owner_client, created["asset_id"])

    after = _act(owner_client, created["asset_id"], "archive", published["revision"]).json()

    assert after["status"] == "archived"
    assert _public_files(storage, created["asset_id"]) == set()


def test_trash_removes_links_and_restore_does_not_republish(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    created = _image(owner_client)
    published = _publish(owner_client, created["asset_id"])

    trashed = _act(owner_client, created["asset_id"], "trash", published["revision"]).json()
    assert trashed["status"] == "trashed"
    assert _public_files(storage, created["asset_id"]) == set()

    restored = _act(owner_client, created["asset_id"], "restore", trashed["revision"]).json()

    assert restored["status"] == "draft"
    assert restored["published_version_id"] is None
    assert _public_files(storage, created["asset_id"]) == set()


def test_trashed_asset_cannot_be_published(owner_client: TestClient) -> None:
    created = _image(owner_client)
    trashed = _act(owner_client, created["asset_id"], "trash", 1).json()

    response = _act(owner_client, created["asset_id"], "publish", trashed["revision"])

    assert response.status_code == 409


def test_stale_revision_blocks_lifecycle_actions(owner_client: TestClient) -> None:
    created = _image(owner_client)
    _publish(owner_client, created["asset_id"])

    response = _act(owner_client, created["asset_id"], "publish", 1)

    assert response.status_code == 409


# —— 替换与公开字节 ——


def test_replacement_does_not_change_published_bytes(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    """只有修改权限的编辑替换文件，公开字节不能变（企划第 3.2 节）。"""
    original = minimal_png(width=40, height=20)
    created = _image(owner_client, "v1.png", original)
    published = _publish(owner_client, created["asset_id"])
    before = storage.public_current_path(created["asset_id"], "png").read_bytes()

    new_payload = minimal_png(width=64, height=48)
    replaced = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("v2.png", new_payload, "image/png")},
        data={"asset_id": created["asset_id"]},
        headers=admin_headers(owner_client),
    )
    assert replaced.status_code == 201

    assert storage.public_current_path(created["asset_id"], "png").read_bytes() == before
    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    assert detail["current_version"]["version_no"] == 2
    assert detail["published_version_id"] == published["published_version_id"]

    _publish(owner_client, created["asset_id"])
    assert storage.public_current_path(created["asset_id"], "png").read_bytes() == new_payload


def test_extension_follows_published_version(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    """扩展名随真实类型变化：换成 JPEG 之后，旧的 .png 地址必须消失。"""
    created = _image(owner_client, "a.png")
    _publish(owner_client, created["asset_id"])
    assert storage.public_current_path(created["asset_id"], "png").is_file()

    replaced = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("b.jpg", _jpeg(), "image/jpeg")},
        data={"asset_id": created["asset_id"]},
        headers=admin_headers(owner_client),
    )
    assert replaced.status_code == 201, replaced.text
    _publish(owner_client, created["asset_id"])

    assert storage.public_current_path(created["asset_id"], "jpg").is_file()
    assert not storage.public_current_path(created["asset_id"], "png").exists()


# —— 引用检查 ——


def test_reference_must_be_published_first(owner_client: TestClient) -> None:
    image = _image(owner_client, "inline.png")
    article = _text(owner_client, body=f"# 文章\n\n![图](/p/{image['asset_id']}/original.png)")

    response = _act(owner_client, article["id"], "publish", article["revision"])

    assert response.status_code == 409
    assert image["asset_id"] in response.json()["error"]["message"]


def test_publish_allowed_after_reference_is_published(owner_client: TestClient) -> None:
    image = _image(owner_client, "inline.png")
    article = _text(owner_client, body=f"# 文章\n\n![图](/p/{image['asset_id']}/original.png)")
    _publish(owner_client, image["asset_id"])

    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    response = _act(owner_client, article["id"], "publish", detail["revision"])

    assert response.status_code == 200


# —— 权限 ——


def test_editor_without_publish_cannot_publish(
    client: TestClient, db: DbSession, owner_client
) -> None:
    created = _image(owner_client, "pic.png")
    add_editor(db, username="writer", permissions=["asset.upload", "asset.update"])
    client.cookies.clear()
    login(client, "writer", EDITOR_PASSWORD)

    response = _act(client, created["asset_id"], "publish", 1)

    assert response.status_code == 403
    assert "asset.publish" in response.json()["error"]["message"]


def test_trash_requires_trash_permission(client: TestClient, db: DbSession, owner_client) -> None:
    created = _image(owner_client, "pic.png")
    add_editor(db, username="writer", permissions=["asset.publish"])
    client.cookies.clear()
    login(client, "writer", EDITOR_PASSWORD)

    assert _act(client, created["asset_id"], "trash", 1).status_code == 403


def test_visibility_change_on_published_needs_publish_permission(
    client: TestClient, db: DbSession, owner_client
) -> None:
    created = _image(owner_client, "pic.png")
    published = _publish(owner_client, created["asset_id"])
    add_editor(db, username="writer", permissions=["asset.update"])
    client.cookies.clear()
    login(client, "writer", EDITOR_PASSWORD)

    response = client.patch(
        f"{API}/assets/{created['asset_id']}",
        json={"revision": published["revision"], "visibility": "private"},
        headers=admin_headers(client),
    )

    assert response.status_code == 403
    assert "asset.publish" in response.json()["error"]["message"]


def test_only_owner_can_purge(client: TestClient, db: DbSession, owner_client) -> None:
    created = _image(owner_client, "pic.png")
    _act(owner_client, created["asset_id"], "trash", 1)
    add_editor(db, username="writer", permissions=["asset.upload", "asset.trash"])
    client.cookies.clear()
    login(client, "writer", EDITOR_PASSWORD)

    response = client.delete(f"{API}/assets/{created['asset_id']}", headers=admin_headers(client))

    assert response.status_code == 403


def test_purge_requires_trashed_state(owner_client: TestClient) -> None:
    created = _image(owner_client)

    response = owner_client.delete(
        f"{API}/assets/{created['asset_id']}", headers=admin_headers(owner_client)
    )

    assert response.status_code == 409


def test_purge_removes_files(owner_client: TestClient, storage: LocalStorage) -> None:
    created = _image(owner_client, "gone.png")
    published = _publish(owner_client, created["asset_id"])
    trashed = _act(owner_client, created["asset_id"], "trash", published["revision"])
    assert trashed.status_code == 200, trashed.text

    response = owner_client.delete(
        f"{API}/assets/{created['asset_id']}", headers=admin_headers(owner_client)
    )

    assert response.status_code == 204
    assert not storage.original_path(created["storage_key"]).exists()
    assert owner_client.get(f"{API}/assets/{created['asset_id']}").status_code == 404


# —— 私有下载 ——


def test_owner_can_download_draft(owner_client: TestClient, settings) -> None:
    created = _image(owner_client, "draft.png")

    response = owner_client.get(f"{API}/assets/{created['asset_id']}/file")

    assert response.status_code == 200
    if settings.x_accel_redirect:
        assert response.headers["x-accel-redirect"].startswith("/_internal/originals/")
        assert response.headers["content-disposition"].startswith("attachment")
        assert response.headers["cache-control"] == "private, no-store"
    else:
        assert hashlib.sha256(response.content).hexdigest() == created["sha256"]


def test_download_requires_read_permission(client: TestClient, db: DbSession, owner_client) -> None:
    created = _image(owner_client, "draft.png")
    add_editor(db, username="nobody", permissions=[])
    client.cookies.clear()
    login(client, "nobody", EDITOR_PASSWORD)

    response = client.get(f"{API}/assets/{created['asset_id']}/file")

    assert response.status_code == 403


# —— 签名地址 ——


def _strip_base(url: str) -> str:
    """签名地址带 ``public_base_url``，测试里只取路径与查询串。"""
    marker = "/api/public/v1/files/"
    return marker + url.split(marker, 1)[1]


def test_signed_url_works_for_site_visibility(owner_client: TestClient, client: TestClient) -> None:
    created = _image(owner_client, "site-only.png")
    _publish(owner_client, created["asset_id"], visibility="site")

    signed = owner_client.get(
        f"{API}/assets/{created['asset_id']}/signed-url", params={"ttl": 60}
    ).json()

    client.cookies.clear()
    response = client.get(_strip_base(signed["url"]))

    assert response.status_code == 200


@pytest.mark.parametrize("visibility", ["public", "unlisted"])
def test_signed_url_works_for_public_visibilities(
    visibility: str, owner_client: TestClient, client: TestClient
) -> None:
    """公开与未列出同样能用签名地址读取——这里曾经因为集合写错而失效。"""
    created = _image(owner_client, "可见.png")
    _publish(owner_client, created["asset_id"], visibility=visibility)
    signed = owner_client.get(
        f"{API}/assets/{created['asset_id']}/signed-url", params={"ttl": 60}
    ).json()

    client.cookies.clear()
    response = client.get(_strip_base(signed["url"]))

    assert response.status_code == 200


def test_signed_url_dies_after_unpublish(owner_client: TestClient, client: TestClient) -> None:
    created = _image(owner_client, "site-only.png")
    published = _publish(owner_client, created["asset_id"], visibility="unlisted")
    signed = owner_client.get(
        f"{API}/assets/{created['asset_id']}/signed-url", params={"ttl": 60}
    ).json()
    _act(owner_client, created["asset_id"], "unpublish", published["revision"])

    client.cookies.clear()
    response = client.get(_strip_base(signed["url"]))

    assert response.status_code == 404


def test_signed_url_requires_published_content(owner_client: TestClient) -> None:
    created = _image(owner_client, "draft.png")

    response = owner_client.get(f"{API}/assets/{created['asset_id']}/signed-url")

    assert response.status_code == 409


def test_forged_signature_is_rejected(client: TestClient, owner_client: TestClient) -> None:
    created = _image(owner_client, "x.png")
    published = _publish(owner_client, created["asset_id"])

    response = client.get(
        f"{PUBLIC}/files/{created['asset_id']}",
        params={
            "version": published["published_version_id"],
            "expires": 99999999999,
            "sig": "0" * 64,
        },
    )

    assert response.status_code == 404


def test_expired_signature_is_rejected(
    settings, client: TestClient, owner_client: TestClient
) -> None:
    created = _image(owner_client, "x.png")
    published = _publish(owner_client, created["asset_id"])
    expired = signing.sign(
        settings.pageview_hmac_secret,
        asset_id=created["asset_id"],
        version_id=published["published_version_id"],
        ttl=1,
        now=1,
    )

    response = client.get(
        f"{PUBLIC}/files/{created['asset_id']}",
        params={
            "version": expired.version_id,
            "expires": expired.expires,
            "sig": expired.signature,
        },
    )

    assert response.status_code == 404


# —— fsck ——


def _run_fsck(db: DbSession, storage: LocalStorage, *, fix: bool = False) -> fsck.Report:
    return fsck.run(db, storage=storage, fix_public_links=fix)


def test_fsck_clean_after_publish(owner_client: TestClient, db: DbSession, storage) -> None:
    created = _image(owner_client, "ok.png")
    _publish(owner_client, created["asset_id"])

    report = _run_fsck(db, storage)

    assert report.ok, report.issues


def test_fsck_reports_missing_link(owner_client: TestClient, db: DbSession, storage) -> None:
    created = _image(owner_client, "ok.png")
    _publish(owner_client, created["asset_id"])
    storage.public_current_path(created["asset_id"], "png").unlink()

    report = _run_fsck(db, storage)

    assert any(issue.code == fsck.MISSING_LINK for issue in report.issues)


def test_fsck_fix_restores_links(owner_client: TestClient, db: DbSession, storage) -> None:
    created = _image(owner_client, "ok.png")
    _publish(owner_client, created["asset_id"])
    storage.clear_public(created["asset_id"])

    report = _run_fsck(db, storage, fix=True)

    assert report.fixed >= 1
    assert storage.public_current_path(created["asset_id"], "png").is_file()


def test_fsck_reports_stale_link_and_removes_it(
    owner_client: TestClient, db: DbSession, storage
) -> None:
    created = _image(owner_client, "ok.png")
    _act(owner_client, created["asset_id"], "trash", 1)
    # 模拟「改状态时没删干净」：公开区留一份副本
    path = storage.public_current_path(created["asset_id"], "png")
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(storage.original_path(created["storage_key"]), path)

    report = _run_fsck(db, storage)
    assert any(issue.code == fsck.STALE_LINK for issue in report.issues)

    _run_fsck(db, storage, fix=True)
    assert storage.public_files(created["asset_id"]) == []


def test_fsck_does_not_republish_withdrawn_content(
    owner_client: TestClient, db: DbSession, storage
) -> None:
    created = _image(owner_client, "ok.png")
    published = _publish(owner_client, created["asset_id"])
    _act(owner_client, created["asset_id"], "unpublish", published["revision"])

    _run_fsck(db, storage, fix=True)

    assert storage.public_files(created["asset_id"]) == []


def test_fsck_reports_missing_file(owner_client: TestClient, db: DbSession, storage) -> None:
    created = _image(owner_client, "gone.png")
    storage.original_path(created["storage_key"]).unlink()

    report = _run_fsck(db, storage)

    assert any(issue.code == fsck.MISSING_FILE for issue in report.issues)


def test_fsck_reports_orphan_file_without_deleting_it(
    owner_client: TestClient, db: DbSession, storage
) -> None:
    _image(owner_client, "ok.png")
    stray = storage.originals_dir / "aa" / "stray" / "v1" / "original.png"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(minimal_png())

    report = _run_fsck(db, storage, fix=True)

    assert any(issue.code == fsck.ORPHAN_FILE for issue in report.issues)
    assert stray.is_file()


def test_fsck_reports_hash_mismatch(owner_client: TestClient, db: DbSession, storage) -> None:
    created = _image(owner_client, "ok.png")
    storage.original_path(created["storage_key"]).write_bytes(minimal_png(width=13, height=13))

    report = _run_fsck(db, storage)

    assert any(issue.code == fsck.HASH_MISMATCH for issue in report.issues)


def test_fsck_reports_and_clears_temp_link(
    owner_client: TestClient, db: DbSession, storage
) -> None:
    created = _image(owner_client, "ok.png")
    _publish(owner_client, created["asset_id"])
    temp = storage.public_current_path(created["asset_id"], "png").with_name("original.png.tmp")
    temp.write_bytes(b"leftover")

    report = _run_fsck(db, storage)
    assert any(issue.code == fsck.TEMP_LINK for issue in report.issues)

    _run_fsck(db, storage, fix=True)
    assert not temp.exists()


def test_fsck_skips_hashes_when_asked(owner_client: TestClient, db: DbSession, storage) -> None:
    created = _image(owner_client, "ok.png")
    storage.original_path(created["storage_key"]).write_bytes(minimal_png(width=13, height=13))

    report = fsck.run(db, storage=storage, check_hashes=False)

    assert not any(issue.code == fsck.HASH_MISMATCH for issue in report.issues)
