"""验收反馈的两处修正（企划 T1 与 8.6 的替换权限）。

1. Markdown / 纯文本要能直接改正文，不能只能再传一份文件；
2. 替换已有文件属于 ``asset.update``，只有上传权限的编辑不能替换别人的内容。
"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import EDITOR_PASSWORD, OWNER_PASSWORD, add_editor, admin_headers, login
from tests.inspection_helpers import minimal_png

from ctrlstore.models import Asset, AssetReference, AssetVersion
from ctrlstore.storage import LocalStorage

API = "/api/admin/v1"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def _text(client: TestClient, body: str = "# 标题\n\n正文", **extra) -> dict:
    response = client.post(
        f"{API}/assets",
        json={"kind": "markdown", "body": body, **extra},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _image(client: TestClient, name: str = "pic.png") -> dict:
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": (name, minimal_png(), "image/png")},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _as_owner(client: TestClient, asset_id: str) -> dict:
    """切回所有者读详情。

    ``owner_client`` 与 ``client`` 是同一个 TestClient：用例中间换了登录身份，
    这里重新以所有者登录再读。
    """
    client.cookies.clear()
    login(client, "owner", OWNER_PASSWORD)
    return client.get(f"{API}/assets/{asset_id}").json()


# —— 第一处：正文可以直接改 ——


def test_patch_body_creates_a_new_version(owner_client: TestClient, storage: LocalStorage) -> None:
    asset = _text(owner_client, body="# 旧标题\n\n旧正文")
    first_key = asset["current_version"]["storage_key"]

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "body": "# 新正文\n\n改过了"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 2
    assert body["current_version"]["version_no"] == 2
    new_key = body["current_version"]["storage_key"]
    assert new_key != first_key
    assert "改过了" in storage.original_path(new_key).read_text(encoding="utf-8")
    # 旧版本文件仍然保留（版本写完后不再改）
    assert storage.original_path(first_key).is_file()


def test_patch_body_works_for_plain_text(owner_client: TestClient, storage: LocalStorage) -> None:
    asset = owner_client.post(
        f"{API}/assets",
        json={"kind": "text", "body": "第一版公告"},
        headers=admin_headers(owner_client),
    ).json()

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "body": "第二版公告"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200
    stored = storage.original_path(response.json()["current_version"]["storage_key"])
    assert stored.read_text(encoding="utf-8") == "第二版公告"


def test_patch_body_is_rejected_for_file_kinds(owner_client: TestClient) -> None:
    image = _image(owner_client)

    response = owner_client.patch(
        f"{API}/assets/{image['asset_id']}",
        json={"revision": 1, "body": "不能这么改图片"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422
    assert "Markdown" in response.json()["error"]["message"]


def test_patch_body_applies_front_matter(owner_client: TestClient) -> None:
    asset = _text(owner_client, body="# 原标题")

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "body": "---\ntitle: 正文里的标题\ntags: [新标签]\n---\n\n正文"},
        headers=admin_headers(owner_client),
    )

    body = response.json()
    assert body["title"] == "正文里的标题"
    assert [tag["name"] for tag in body["tags"]] == ["新标签"]


def test_explicit_field_wins_over_front_matter(owner_client: TestClient) -> None:
    asset = _text(owner_client, body="# 原标题")

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={
            "revision": 1,
            "title": "请求里指定的标题",
            "body": "---\ntitle: 正文里的标题\n---\n\n正文",
        },
        headers=admin_headers(owner_client),
    )

    assert response.json()["title"] == "请求里指定的标题"


def test_patch_body_refreshes_references(owner_client: TestClient, db: DbSession) -> None:
    image = _image(owner_client)
    asset = _text(owner_client, body="# 无图")

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "body": f"# 有图\n\n![图](/p/{image['asset_id']}/original.png)"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200
    references = db.query(AssetReference).filter_by(from_asset_id=asset["id"]).all()
    assert [row.to_asset_id for row in references] == [image["asset_id"]]


def test_patch_body_respects_size_limit(owner_client: TestClient, settings) -> None:
    asset = _text(owner_client)

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "body": "x" * (settings.limits.markdown + 10)},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 413


def test_metadata_only_patch_keeps_version(owner_client: TestClient) -> None:
    asset = _text(owner_client)

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "title": "只改标题"},
        headers=admin_headers(owner_client),
    )

    assert response.json()["current_version"]["version_no"] == 1
    assert response.json()["revision"] == 2


# —— 第二处：替换文件要 asset.update ——


def test_upload_only_editor_cannot_replace(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    created = _image(owner_client, "origin.png")
    asset_id = created["asset_id"]
    add_editor(db, username="uploader", permissions=["asset.upload"])

    client.cookies.clear()
    login(client, "uploader", EDITOR_PASSWORD)
    response = client.post(
        f"{API}/uploads",
        json={"filename": "hijack.png", "asset_id": asset_id},
        headers=admin_headers(client),
    )

    assert response.status_code == 403
    assert "asset.update" in response.json()["error"]["message"]


def test_upload_only_editor_cannot_replace_in_one_shot(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    created = _image(owner_client, "origin.png")
    add_editor(db, username="uploader", permissions=["asset.upload"])

    client.cookies.clear()
    login(client, "uploader", EDITOR_PASSWORD)
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": ("hijack.png", minimal_png(width=9, height=9), "image/png")},
        data={"asset_id": created["asset_id"]},
        headers=admin_headers(client),
    )

    assert response.status_code == 403


def test_upload_only_editor_can_still_create_new_content(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    add_editor(db, username="uploader", permissions=["asset.upload"])

    client.cookies.clear()
    login(client, "uploader", EDITOR_PASSWORD)
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": ("fresh.png", minimal_png(), "image/png")},
        headers=admin_headers(client),
    )

    assert response.status_code == 201


def test_update_permission_alone_allows_replacement(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    """asset.update 是替换的权限；编辑同时需要上传字节的能力，这里单独验权限判定。"""
    created = _image(owner_client, "origin.png")
    add_editor(db, username="editor", permissions=["asset.upload", "asset.update"])

    client.cookies.clear()
    login(client, "editor", EDITOR_PASSWORD)
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": ("v2.png", minimal_png(width=10, height=10), "image/png")},
        data={"asset_id": created["asset_id"]},
        headers=admin_headers(client),
    )

    assert response.status_code == 201
    detail = _as_owner(client, created["asset_id"])
    assert detail["current_version"]["version_no"] == 2


def test_completion_rechecks_update_permission(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    """会话建好之后被收回权限，完成时仍然要挡住。"""
    created = _image(owner_client, "origin.png")
    editor = add_editor(db, username="editor", permissions=["asset.upload", "asset.update"])

    client.cookies.clear()
    login(client, "editor", EDITOR_PASSWORD)
    session = client.post(
        f"{API}/uploads",
        json={"filename": "v2.png", "asset_id": created["asset_id"]},
        headers=admin_headers(client),
    ).json()
    client.put(
        f"{API}/uploads/{session['id']}/parts/1",
        content=minimal_png(width=11, height=11),
        headers=admin_headers(client),
    )

    from ctrlstore.auth import service as auth_service

    owner = auth_service.get_owner(db)
    auth_service.set_permissions(db, editor, ["asset.upload"], actor=owner)
    db.commit()

    response = client.post(f"{API}/uploads/{session['id']}/complete", headers=admin_headers(client))

    assert response.status_code == 403
    detail = _as_owner(client, created["asset_id"])
    assert detail["current_version"]["version_no"] == 1


def test_replacement_leaves_no_partial_version(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    created = _image(owner_client, "origin.png")
    add_editor(db, username="uploader", permissions=["asset.upload"])
    client.cookies.clear()
    login(client, "uploader", EDITOR_PASSWORD)
    client.post(
        f"{API}/uploads",
        json={"filename": "hijack.png", "asset_id": created["asset_id"]},
        headers=admin_headers(client),
    )

    versions = db.query(AssetVersion).filter_by(asset_id=created["asset_id"]).count()
    asset = db.get(Asset, created["asset_id"])
    assert versions == 1
    assert asset.revision == 1
