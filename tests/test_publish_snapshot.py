"""复验用例：公开响应只随发布变化（问题总结第 1–6 条）。

每一条都对应验收方给的一个复现步骤，断言的是**修复后**的行为。
"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import admin_headers
from tests.inspection_helpers import minimal_png

from ctrlstore.models import Asset

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


# —— 工具 ——


def _image(client: TestClient, visibility: str = "public", name: str = "pic.png") -> dict:
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": (name, minimal_png(), "image/png")},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    asset_id = response.json()["asset_id"]
    detail = client.get(f"{API}/assets/{asset_id}").json()
    if detail["visibility"] != visibility:
        detail = client.patch(
            f"{API}/assets/{asset_id}",
            json={"revision": detail["revision"], "visibility": visibility},
            headers=admin_headers(client),
        ).json()
    published = client.post(
        f"{API}/assets/{asset_id}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(client),
    )
    assert published.status_code == 200, published.text
    return {**published.json(), "asset_id": published.json()["id"]}


def _article(client: TestClient, body: str, *, visibility: str = "public", **extra) -> dict:
    created = client.post(
        f"{API}/assets",
        json={"kind": "markdown", "body": body, **extra},
        headers=admin_headers(client),
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    detail = client.get(f"{API}/assets/{asset_id}").json()
    if detail["visibility"] != visibility:
        detail = client.patch(
            f"{API}/assets/{asset_id}",
            json={"revision": detail["revision"], "visibility": visibility},
            headers=admin_headers(client),
        ).json()
    published = client.post(
        f"{API}/assets/{asset_id}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(client),
    )
    assert published.status_code == 200, published.text
    return {**published.json(), "asset_id": published.json()["id"]}


def _patch(client: TestClient, asset_id: str, **changes) -> dict:
    detail = client.get(f"{API}/assets/{asset_id}").json()
    response = client.patch(
        f"{API}/assets/{asset_id}",
        json={"revision": detail["revision"], **changes},
        headers=admin_headers(client),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _republish(client: TestClient, asset_id: str) -> dict:
    detail = client.get(f"{API}/assets/{asset_id}").json()
    response = client.post(
        f"{API}/assets/{asset_id}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(client),
    )
    assert response.status_code == 200, response.text
    return response.json()


# —— 1. 修改未被发布的正文不会改变线上 HTML ——


def test_draft_body_change_does_not_change_public_html(
    owner_client: TestClient, client: TestClient
) -> None:
    image = _image(owner_client)
    article = _article(owner_client, f"# 文章\n\n![图](asset:{image['asset_id']})")
    before = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]
    assert "<img" in before

    # 只改草稿正文，去掉图片引用；不发布
    _patch(owner_client, article["id"], body="# 文章\n\n没有图了")

    after = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]

    assert after == before
    assert "<img" in after


def test_draft_body_change_without_publish_permission_does_not_leak(
    client: TestClient, db: DbSession, owner_client: TestClient
) -> None:
    """只有 asset.update 的编辑改草稿，也不能改变访客看到的正文。"""
    from tests.auth_helpers import EDITOR_PASSWORD, add_editor, login

    image = _image(owner_client)
    article = _article(owner_client, f"# 文章\n\n![图](asset:{image['asset_id']})")
    before = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]

    add_editor(db, username="writer", permissions=["asset.read", "asset.update"])
    client.cookies.clear()
    login(client, "writer", EDITOR_PASSWORD)
    _patch(client, article["id"], body="# 改过\n\n没有图")

    client.cookies.clear()
    after = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]

    assert after == before
    assert "改过" not in after


def test_publishing_new_body_updates_public_html(
    owner_client: TestClient, client: TestClient
) -> None:
    article = _article(owner_client, "# 旧版本")
    _patch(owner_client, article["id"], body="# 新版本")

    _republish(owner_client, article["id"])

    html = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]
    assert "新版本" in html


# —— 2. ``/p/`` 写法：绝对地址、撤回后去掉 ——


def test_relative_public_path_becomes_absolute(
    owner_client: TestClient, client: TestClient
) -> None:
    image = _image(owner_client)
    article = _article(owner_client, f"# 图\n\n![图](/p/{image['asset_id']}/original.png)")

    html = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]

    assert html.count("<img") == 1
    assert f"http://127.0.0.1:8000/p/{image['asset_id']}/original.png" in html
    assert 'src="/p/' not in html


def test_relative_public_path_dropped_after_image_unpublish(
    owner_client: TestClient, client: TestClient
) -> None:
    image = _image(owner_client)
    article = _article(owner_client, f"# 图\n\n![图](/p/{image['asset_id']}/original.png)")
    assert "<img" in client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]

    detail = owner_client.get(f"{API}/assets/{image['asset_id']}").json()
    owner_client.post(
        f"{API}/assets/{image['asset_id']}/unpublish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    html = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]

    assert "<img" not in html
    assert "文章" in html or "# " not in html


def test_two_image_syntaxes_produce_the_same_url(
    owner_client: TestClient, client: TestClient
) -> None:
    image = _image(owner_client)
    body = (
        f"![甲](asset:{image['asset_id']})\n\n"
        f"![乙](/p/{image['asset_id']}/original.png)\n\n"
        f"![丙](http://127.0.0.1:8000/p/{image['asset_id']}/original.png)"
    )
    article = _article(owner_client, body)

    html = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]

    expected = f'src="http://127.0.0.1:8000/p/{image["asset_id"]}/original.png"'
    assert html.count(expected) == 3


# —— 3. 别名属于发布快照，且已发布别名唯一 ——


def test_slug_change_needs_publish(owner_client: TestClient, client: TestClient) -> None:
    article = _article(owner_client, "# 文章", slug="old-name")
    assert client.get(f"{PUBLIC}/by-slug/old-name").status_code == 200

    _patch(owner_client, article["id"], slug="new-name")

    assert client.get(f"{PUBLIC}/by-slug/old-name").status_code == 200
    assert client.get(f"{PUBLIC}/by-slug/new-name").status_code == 404

    _republish(owner_client, article["id"])

    assert client.get(f"{PUBLIC}/by-slug/new-name").status_code == 200
    assert client.get(f"{PUBLIC}/by-slug/old-name").status_code == 404


def test_duplicate_published_slug_is_rejected(owner_client: TestClient) -> None:
    _article(owner_client, "# 第一篇", slug="same-name")
    second = owner_client.post(
        f"{API}/assets",
        json={"kind": "markdown", "body": "# 第二篇", "slug": "same-name"},
        headers=admin_headers(owner_client),
    ).json()
    detail = owner_client.get(f"{API}/assets/{second['id']}").json()
    detail = owner_client.patch(
        f"{API}/assets/{second['id']}",
        json={"revision": detail["revision"], "visibility": "public"},
        headers=admin_headers(owner_client),
    ).json()

    response = owner_client.post(
        f"{API}/assets/{second['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 409
    assert "same-name" in response.json()["error"]["message"]


def test_duplicate_published_slug_blocked_by_database(
    owner_client: TestClient, db: DbSession
) -> None:
    """库层还有一条部分唯一索引，不只靠应用代码。"""
    first = _article(owner_client, "# 一", slug="dup-slug")
    second = _article(owner_client, "# 二")
    assert first["published_slug"] == "dup-slug"
    assert second["published_slug"] is None

    row = db.get(Asset, second["id"])
    row.published_slug = "dup-slug"
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# —— 4. 仅站点内容不给 /p/，站点令牌拿签名地址 ——


def test_site_only_payload_has_no_public_url(owner_client: TestClient, client: TestClient) -> None:
    site = owner_client.post(
        f"{API}/sites", json={"key": "main-site"}, headers=admin_headers(owner_client)
    ).json()
    token = owner_client.post(
        f"{API}/tokens",
        json={"subject_type": "site", "subject_key": "main-site"},
        headers=admin_headers(owner_client),
    ).json()["token"]
    owner_client.post(
        f"{API}/sites/{site['id']}/grants",
        json={"prefix": "home"},
        headers=admin_headers(owner_client),
    )
    collection = owner_client.post(
        f"{API}/collections",
        json={"slug": "home.hero", "title": "首页"},
        headers=admin_headers(owner_client),
    ).json()
    image = _image(owner_client, visibility="site")
    owner_client.post(
        f"{API}/collections/{collection['id']}/items",
        json={"asset_id": image["asset_id"]},
        headers=admin_headers(owner_client),
    )

    from fastapi.testclient import TestClient as NewClient

    site_client = NewClient(client.app, headers={"Authorization": f"Bearer {token}"})
    payload = site_client.get(f"{PUBLIC}/assets/{image['asset_id']}").json()

    assert "urls" in payload
    assert "signed" in payload["urls"]
    assert "original" not in payload["urls"]
    assert "/p/" not in payload["urls"]["signed"]
    # 签名地址可以直接取到字节
    follow = site_client.get(payload["urls"]["signed"].replace("http://127.0.0.1:8000", ""))
    assert follow.status_code == 200


def test_signed_url_in_payload_dies_after_unpublish(
    owner_client: TestClient, client: TestClient
) -> None:
    site = owner_client.post(
        f"{API}/sites", json={"key": "main-site"}, headers=admin_headers(owner_client)
    ).json()
    token = owner_client.post(
        f"{API}/tokens",
        json={"subject_type": "site", "subject_key": "main-site"},
        headers=admin_headers(owner_client),
    ).json()["token"]
    owner_client.post(
        f"{API}/sites/{site['id']}/grants",
        json={"prefix": "home"},
        headers=admin_headers(owner_client),
    )
    collection = owner_client.post(
        f"{API}/collections",
        json={"slug": "home.hero", "title": "首页"},
        headers=admin_headers(owner_client),
    ).json()
    image = _image(owner_client, visibility="site")
    owner_client.post(
        f"{API}/collections/{collection['id']}/items",
        json={"asset_id": image["asset_id"]},
        headers=admin_headers(owner_client),
    )
    from fastapi.testclient import TestClient as NewClient

    site_client = NewClient(client.app, headers={"Authorization": f"Bearer {token}"})
    signed = site_client.get(f"{PUBLIC}/assets/{image['asset_id']}").json()["urls"]["signed"]

    detail = owner_client.get(f"{API}/assets/{image['asset_id']}").json()
    owner_client.post(
        f"{API}/assets/{image['asset_id']}/unpublish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    follow = site_client.get(signed.replace("http://127.0.0.1:8000", ""))
    assert follow.status_code == 404


def test_anonymous_never_gets_signed_url(owner_client: TestClient, client: TestClient) -> None:
    image = _image(owner_client, visibility="private")

    assert client.get(f"{PUBLIC}/assets/{image['asset_id']}").status_code == 404


# —— 5. 集合缓存标识不受不可见条目影响 ——


def test_collection_updated_at_ignores_invisible_items(
    owner_client: TestClient, client: TestClient
) -> None:
    collection = owner_client.post(
        f"{API}/collections",
        json={"slug": "home.hero", "title": "首页"},
        headers=admin_headers(owner_client),
    ).json()
    visible = _image(owner_client)
    hidden = _image(owner_client, visibility="private")
    for asset in (visible, hidden):
        owner_client.post(
            f"{API}/collections/{collection['id']}/items",
            json={"asset_id": asset["asset_id"]},
            headers=admin_headers(owner_client),
        )

    first = client.get(f"{PUBLIC}/collections/home.hero")

    # 改一条匿名看不见的内容，再发布
    _patch(owner_client, hidden["asset_id"], title="改过的隐藏标题")
    _republish(owner_client, hidden["asset_id"])

    second = client.get(f"{PUBLIC}/collections/home.hero")

    assert first.json()["updated_at"] == second.json()["updated_at"]
    assert first.headers["etag"] == second.headers["etag"]


# —— 6. purge 检查已发布封面 ——


def test_purge_blocks_published_cover(owner_client: TestClient) -> None:
    image = _image(owner_client)
    article = _article(owner_client, "# 文章")
    owner_client.patch(
        f"{API}/assets/{article['id']}",
        json={"revision": article["revision"], "cover_asset_id": image["asset_id"]},
        headers=admin_headers(owner_client),
    )
    _republish(owner_client, article["id"])

    # 清掉编辑中的封面，只留已发布快照里的引用
    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    owner_client.patch(
        f"{API}/assets/{article['id']}",
        json={"revision": detail["revision"], "cover_asset_id": None},
        headers=admin_headers(owner_client),
    )
    image_detail = owner_client.get(f"{API}/assets/{image['asset_id']}").json()
    owner_client.post(
        f"{API}/assets/{image['asset_id']}/trash",
        json={"revision": image_detail["revision"]},
        headers=admin_headers(owner_client),
    )

    response = owner_client.delete(
        f"{API}/assets/{image['asset_id']}", headers=admin_headers(owner_client)
    )

    assert response.status_code == 409
    assert "封面" in response.json()["error"]["message"]
    covers = response.json()["error"]["details"]["references"]["covers"]
    assert len(covers) == 1
    assert covers[0]["id"] == article["id"]
    assert covers[0]["current"] is False
    assert covers[0]["published"] is True
