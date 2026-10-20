"""目录、标签、内容与版本（规划第 4 部分验收项）。

覆盖：根目录重名、目录成环、移动带子孙、修订号冲突、
替换不写 published_version_id、第 6 版不留旧版本、引用与封面规则。
"""

from __future__ import annotations

import hashlib
import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import EDITOR_PASSWORD, add_editor, admin_headers, login
from tests.inspection_helpers import minimal_png

from ctrlstore import audit
from ctrlstore.models import Asset, AssetVersion, AuditLog
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


def _create_folder(client: TestClient, name: str, parent_id: str | None = None) -> dict:
    response = client.post(
        f"{API}/folders",
        json={"name": name, "parent_id": parent_id},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload_png(client: TestClient, name: str = "pic.png", **extra) -> dict:
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": (name, minimal_png(), "image/png")},
        data=extra or None,
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _text_asset(client: TestClient, body: str = "# 标题\n\n正文", **extra) -> dict:
    payload = {"kind": "markdown", "body": body, **extra}
    response = client.post(f"{API}/assets", json=payload, headers=admin_headers(client))
    assert response.status_code == 201, response.text
    return response.json()


# —— 目录 ——


def test_folder_tree_paths(owner_client: TestClient) -> None:
    root = _create_folder(owner_client, "资料")
    child = _create_folder(owner_client, "2026", parent_id=root["id"])

    assert root["path"] == "/资料"
    assert child["path"] == "/资料/2026"


def test_duplicate_name_in_same_parent_conflicts(owner_client: TestClient) -> None:
    _create_folder(owner_client, "资料")

    response = owner_client.post(
        f"{API}/folders", json={"name": "资料"}, headers=admin_headers(owner_client)
    )

    assert response.status_code == 409
    assert "同名" in response.json()["error"]["message"]


def test_same_name_allowed_under_different_parents(owner_client: TestClient) -> None:
    first = _create_folder(owner_client, "A")
    second = _create_folder(owner_client, "B")

    assert _create_folder(owner_client, "子", parent_id=first["id"])["path"] == "/A/子"
    assert _create_folder(owner_client, "子", parent_id=second["id"])["path"] == "/B/子"


def test_folder_name_rejects_slash(owner_client: TestClient) -> None:
    response = owner_client.post(
        f"{API}/folders", json={"name": "a/b"}, headers=admin_headers(owner_client)
    )

    assert response.status_code == 422


def test_rename_updates_descendants(owner_client: TestClient) -> None:
    root = _create_folder(owner_client, "资料")
    child = _create_folder(owner_client, "2026", parent_id=root["id"])
    grandchild = _create_folder(owner_client, "10月", parent_id=child["id"])

    response = owner_client.patch(
        f"{API}/folders/{root['id']}", json={"name": "档案"}, headers=admin_headers(owner_client)
    )

    assert response.status_code == 200
    listing = {item["id"]: item["path"] for item in owner_client.get(f"{API}/folders").json()}
    assert listing[root["id"]] == "/档案"
    assert listing[child["id"]] == "/档案/2026"
    assert listing[grandchild["id"]] == "/档案/2026/10月"


def test_move_folder_carries_descendants(owner_client: TestClient) -> None:
    source = _create_folder(owner_client, "来源")
    target = _create_folder(owner_client, "目标")
    nested = _create_folder(owner_client, "深层", parent_id=source["id"])

    response = owner_client.post(
        f"{API}/folders/{source['id']}/move",
        json={"parent_id": target["id"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200
    listing = {item["id"]: item["path"] for item in owner_client.get(f"{API}/folders").json()}
    assert listing[source["id"]] == "/目标/来源"
    assert listing[nested["id"]] == "/目标/来源/深层"


def test_move_into_own_child_is_rejected(owner_client: TestClient) -> None:
    parent = _create_folder(owner_client, "父")
    child = _create_folder(owner_client, "子", parent_id=parent["id"])

    response = owner_client.post(
        f"{API}/folders/{parent['id']}/move",
        json={"parent_id": child["id"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422
    assert "子目录" in response.json()["error"]["message"]


def test_move_into_itself_is_rejected(owner_client: TestClient) -> None:
    folder = _create_folder(owner_client, "自己")

    response = owner_client.post(
        f"{API}/folders/{folder['id']}/move",
        json={"parent_id": folder["id"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422


def test_folder_requires_manage_permission(client: TestClient, db: DbSession) -> None:
    add_editor(db, username="reader", permissions=["asset.read"])
    login(client, "reader", EDITOR_PASSWORD)

    response = client.post(f"{API}/folders", json={"name": "x"}, headers=admin_headers(client))

    assert response.status_code == 403


# —— 标签 ——


def test_tag_is_created_once(owner_client: TestClient) -> None:
    first = owner_client.post(
        f"{API}/tags", json={"name": "首页"}, headers=admin_headers(owner_client)
    )
    second = owner_client.post(
        f"{API}/tags", json={"name": "首页"}, headers=admin_headers(owner_client)
    )

    assert first.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert first.json()["slug"] == "首页"


def test_tag_filter_by_name(owner_client: TestClient) -> None:
    headers = admin_headers(owner_client)
    owner_client.post(f"{API}/tags", json={"name": "首页主图"}, headers=headers)
    owner_client.post(f"{API}/tags", json={"name": "文章封面"}, headers=headers)

    result = owner_client.get(f"{API}/tags", params={"q": "封面"}).json()

    assert [tag["name"] for tag in result] == ["文章封面"]


def test_slugs_do_not_collide(owner_client: TestClient) -> None:
    headers = admin_headers(owner_client)
    owner_client.post(f"{API}/tags", json={"name": "a b"}, headers=headers)
    second = owner_client.post(f"{API}/tags", json={"name": "a-b"}, headers=headers)

    assert second.json()["slug"] == "a-b-2"


# —— 文本内容 ——


def test_create_markdown_with_front_matter(owner_client: TestClient) -> None:
    body = "---\ntitle: 欢迎\nslug: hello\ntags: [公告, 首页]\n---\n\n# 正文\n\n内容"

    asset = _text_asset(owner_client, body=body)

    assert asset["kind"] == "markdown"
    assert asset["title"] == "欢迎"
    assert asset["slug"] == "hello"
    assert sorted(tag["name"] for tag in asset["tags"]) == ["公告", "首页"]
    assert asset["revision"] == 1
    assert asset["status"] == "draft"
    assert asset["visibility"] == "private"
    assert asset["published_version_id"] is None
    assert asset["current_version"]["version_no"] == 1


def test_text_body_lives_in_its_own_version_file(owner_client: TestClient) -> None:
    asset = _text_asset(owner_client, body="# 标题\n\n正文内容")
    version = asset["current_version"]
    path = LocalStorage.from_settings(owner_client.app.state.settings).original_path(
        version["storage_key"]
    )

    stored = path.read_bytes()
    assert stored.decode("utf-8").endswith("正文内容")
    assert hashlib.sha256(stored).hexdigest() == version["sha256"]


def test_plain_text_title_falls_back_to_first_line(owner_client: TestClient) -> None:
    asset = _text_asset(owner_client, body="公告：今晚维护\n\n详情略", kind="text")

    assert asset["kind"] == "text"
    assert asset["title"].startswith("公告")


def test_create_asset_rejects_file_kinds(owner_client: TestClient) -> None:
    response = owner_client.post(
        f"{API}/assets",
        json={"kind": "image", "body": "x"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422


# —— 上传自动登记 ——


def test_direct_upload_creates_asset_and_version(owner_client: TestClient) -> None:
    session = _upload_png(owner_client, "hero.png")

    asset = owner_client.get(f"{API}/assets/{session['asset_id']}").json()

    assert asset["kind"] == "image"
    assert asset["title"] == "hero"
    assert asset["current_version"]["version_no"] == 1
    assert asset["current_version"]["sha256"] == session["sha256"]
    assert asset["current_version"]["width"] == 8


def test_chunked_upload_also_creates_asset(owner_client: TestClient) -> None:
    created = owner_client.post(
        f"{API}/uploads", json={"filename": "note.md"}, headers=admin_headers(owner_client)
    ).json()
    owner_client.put(
        f"{API}/uploads/{created['id']}/parts/1",
        content=b"# hi\n",
        headers=admin_headers(owner_client),
    )
    done = owner_client.post(
        f"{API}/uploads/{created['id']}/complete", headers=admin_headers(owner_client)
    ).json()

    asset = owner_client.get(f"{API}/assets/{done['asset_id']}").json()

    assert asset["kind"] == "markdown"
    assert asset["current_version"]["byte_size"] == 5


# —— 版本替换 ——


def test_replacement_adds_version_and_keeps_published_empty(owner_client: TestClient) -> None:
    created = _upload_png(owner_client, "v1.png")
    asset_id = created["asset_id"]

    replaced = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("v2.png", minimal_png(width=12, height=9), "image/png")},
        data={"asset_id": asset_id},
        headers=admin_headers(owner_client),
    )

    assert replaced.status_code == 201, replaced.text
    asset = owner_client.get(f"{API}/assets/{asset_id}").json()
    assert asset["current_version"]["version_no"] == 2
    assert asset["current_version"]["width"] == 12
    assert asset["published_version_id"] is None  # 没有发布权限也改不动线上
    assert asset["revision"] == 2
    assert len(asset["versions"]) == 2


def test_replacement_keeps_old_version_file(
    owner_client: TestClient, storage: LocalStorage
) -> None:
    created = _upload_png(owner_client, "v1.png")
    asset_id = created["asset_id"]
    first_key = owner_client.get(f"{API}/assets/{asset_id}").json()["current_version"][
        "storage_key"
    ]

    owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("v2.png", minimal_png(width=12, height=9), "image/png")},
        data={"asset_id": asset_id},
        headers=admin_headers(owner_client),
    )

    assert storage.original_path(first_key).is_file()


def test_replacement_type_mismatch_is_rejected(owner_client: TestClient) -> None:
    created = _upload_png(owner_client, "pic.png")

    response = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("note.md", b"# hi\n", "text/markdown")},
        data={"asset_id": created["asset_id"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422
    assert "不一致" in response.json()["error"]["message"]


def test_sixth_version_drops_the_oldest(owner_client: TestClient, storage: LocalStorage) -> None:
    created = _upload_png(owner_client, "v1.png")
    asset_id = created["asset_id"]
    keys = [owner_client.get(f"{API}/assets/{asset_id}").json()["current_version"]["storage_key"]]

    for index in range(2, 7):
        owner_client.post(
            f"{API}/uploads/direct",
            files={"file": (f"v{index}.png", minimal_png(width=8 + index), "image/png")},
            data={"asset_id": asset_id},
            headers=admin_headers(owner_client),
        )
        keys.append(
            owner_client.get(f"{API}/assets/{asset_id}").json()["current_version"]["storage_key"]
        )

    asset = owner_client.get(f"{API}/assets/{asset_id}").json()

    assert [version["version_no"] for version in asset["versions"]] == [2, 3, 4, 5, 6]
    assert not storage.original_path(keys[0]).exists()  # 第 1 版被淘汰
    assert storage.original_path(keys[-1]).is_file()  # 当前版本一定在


def test_prune_keeps_published_version(owner_client: TestClient, db: DbSession, storage) -> None:
    """即使已发布版本号很旧，也不能被保留规则删掉（第 5 部分才会写入该指针）。"""
    created = _upload_png(owner_client, "v1.png")
    asset = db.get(Asset, created["asset_id"])
    first_version = db.get(AssetVersion, asset.current_version_id)
    # 手工把「已发布版本」指向第 1 版，模拟第 5 部分的状态
    asset.published_version_id = first_version.id
    db.commit()

    for index in range(2, 8):
        owner_client.post(
            f"{API}/uploads/direct",
            files={"file": (f"v{index}.png", minimal_png(width=8 + index), "image/png")},
            data={"asset_id": created["asset_id"]},
            headers=admin_headers(owner_client),
        )

    db.expire_all()
    versions = sorted(
        db.query(AssetVersion).filter_by(asset_id=created["asset_id"]).all(),
        key=lambda item: item.version_no,
    )
    assert first_version.id in {version.id for version in versions}
    assert storage.original_path(first_version.storage_key).is_file()


def test_video_keeps_two_versions(owner_client: TestClient, db: DbSession) -> None:
    created = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32, "video/mp4")},
        headers=admin_headers(owner_client),
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["asset_id"]

    for index in range(2, 5):
        owner_client.post(
            f"{API}/uploads/direct",
            files={
                "file": (
                    f"clip{index}.mp4",
                    b"\x00\x00\x00\x18ftypmp42" + b"\x00" * (32 + index),
                    "video/mp4",
                )
            },
            data={"asset_id": asset_id},
            headers=admin_headers(owner_client),
        )

    asset = owner_client.get(f"{API}/assets/{asset_id}").json()

    assert [version["version_no"] for version in asset["versions"]] == [3, 4]


# —— 元数据与修订号 ——


def test_metadata_update_bumps_revision(owner_client: TestClient) -> None:
    asset = _text_asset(owner_client)

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "title": "新标题", "summary": "摘要"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200
    assert response.json()["title"] == "新标题"
    assert response.json()["revision"] == 2


def test_stale_revision_conflicts(owner_client: TestClient) -> None:
    asset = _text_asset(owner_client)
    headers = admin_headers(owner_client)
    owner_client.patch(
        f"{API}/assets/{asset['id']}", json={"revision": 1, "title": "A"}, headers=headers
    )

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}", json={"revision": 1, "title": "B"}, headers=headers
    )

    assert response.status_code == 409
    assert "修订号" in response.json()["error"]["message"]


def test_patch_requires_revision(owner_client: TestClient) -> None:
    asset = _text_asset(owner_client)

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}", json={"title": "x"}, headers=admin_headers(owner_client)
    )

    assert response.status_code == 422


def test_unknown_field_is_rejected(owner_client: TestClient) -> None:
    asset = _text_asset(owner_client)

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "published_title": "偷改线上"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422
    assert "published_title" in response.json()["error"]["message"]


def test_folder_assignment_requires_existing_folder(owner_client: TestClient) -> None:
    asset = _text_asset(owner_client)

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "folder_id": "no-such-folder"},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422


def test_tags_can_be_replaced_wholesale(owner_client: TestClient) -> None:
    asset = _text_asset(owner_client, tags=["旧"])

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "tags": ["新一", "新二"]},
        headers=admin_headers(owner_client),
    )

    assert sorted(tag["name"] for tag in response.json()["tags"]) == ["新一", "新二"]


def test_one_request_bumps_revision_once(owner_client: TestClient) -> None:
    """同时改字段和标签，仍然只算一次修改。"""
    asset = _text_asset(owner_client)

    response = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": 1, "title": "新标题", "tags": ["甲"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2


# —— 封面与引用 ——


def test_cover_must_be_an_image(owner_client: TestClient) -> None:
    text_asset = _text_asset(owner_client)

    response = owner_client.patch(
        f"{API}/assets/{text_asset['id']}",
        json={"revision": 1, "cover_asset_id": text_asset["id"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422
    assert "封面只能指向图片" in response.json()["error"]["message"]


def test_markdown_references_are_recorded(owner_client: TestClient, db: DbSession) -> None:
    image = _upload_png(owner_client, "inline.png")
    body = f"正文\n\n![图](/p/{image['asset_id']}/original.png)\n"

    asset = _text_asset(owner_client, body=body)

    from ctrlstore.models import AssetReference

    references = db.query(AssetReference).filter_by(from_asset_id=asset["id"]).all()
    assert [row.to_asset_id for row in references] == [image["asset_id"]]


def test_references_ignore_unknown_ids(owner_client: TestClient, db: DbSession) -> None:
    body = "![x](/p/99999999-9999-9999-9999-999999999999/original.png)"

    asset = _text_asset(owner_client, body=body)

    from ctrlstore.models import AssetReference

    assert db.query(AssetReference).filter_by(from_asset_id=asset["id"]).count() == 0


# —— 列表、分页与可见性 ——


def test_list_excludes_trashed_and_supports_filters(
    owner_client: TestClient, db: DbSession
) -> None:
    _text_asset(owner_client, body="# 文章一")
    image = _upload_png(owner_client, "pic.png")

    markdown_only = owner_client.get(f"{API}/assets", params={"kind": "markdown"}).json()
    assert [item["kind"] for item in markdown_only["items"]] == ["markdown"]

    by_title = owner_client.get(f"{API}/assets", params={"q": "pic"}).json()
    assert image["asset_id"] in {item["id"] for item in by_title["items"]}


def test_list_uses_cursor_pagination(owner_client: TestClient) -> None:
    for index in range(3):
        _text_asset(owner_client, body=f"# 第{index}篇")

    first = owner_client.get(f"{API}/assets", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"]

    second = owner_client.get(
        f"{API}/assets", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert {item["id"] for item in first["items"]} & {
        item["id"] for item in second["items"]
    } == set()


def test_editor_without_read_permission_is_blocked(client: TestClient, db: DbSession) -> None:
    add_editor(db, username="nobody", permissions=[])
    login(client, "nobody", EDITOR_PASSWORD)

    assert client.get(f"{API}/assets").status_code == 403


def test_editor_with_read_permission_sees_drafts(
    client: TestClient, db: DbSession, owner_client
) -> None:
    _text_asset(owner_client, body="# 别人的草稿")
    add_editor(db, username="reader", permissions=["asset.read"])
    login(client, "reader", EDITOR_PASSWORD)

    listing = client.get(f"{API}/assets").json()

    assert any(item["title"].startswith("别人的") for item in listing["items"])


def test_audit_records_asset_actions(owner_client: TestClient, db: DbSession) -> None:
    _upload_png(owner_client, "audit.png")

    actions = {row.action for row in db.query(AuditLog).all()}
    assert audit.ASSET_CREATED in actions
