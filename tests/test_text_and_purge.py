"""纯文本的 HTML 输出，以及「被集合引用时不能彻底删除」。

两处都是验收反馈：

1. 纯文本曾经被包进 Markdown 代码围栏再渲染。正文里出现三个反引号时围栏提前结束，
   后面的内容被当成 Markdown 解析——公告里写示例代码就会变形。
2. 彻底删除只检查了封面引用；集合引用同样是「还在用」，必须拒绝并列出。
"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from tests.auth_helpers import admin_headers
from tests.inspection_helpers import minimal_png

from ctrlstore.content import markdown_render

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


# —— 纯文本的 HTML ——


def test_render_plain_text_keeps_backticks_literal() -> None:
    """三个反引号只是文字，不是围栏。"""
    text = "示例：\n```\nprint('hi')\n```\n# 这不是标题\n[这不是链接](x)"

    html = markdown_render.render_plain_text(text)

    assert html.startswith("<pre>")
    assert html.endswith("</pre>")
    assert "```" in html
    assert "<h1" not in html
    assert "<a " not in html
    assert "# 这不是标题" in html


def test_render_plain_text_escapes_html() -> None:
    html = markdown_render.render_plain_text("<b>粗体</b> 与 <script>alert(1)</script>")

    assert "<b>" not in html
    assert "<script" not in html
    assert "&lt;b&gt;" in html
    assert "&lt;script&gt;" in html


def test_render_plain_text_keeps_line_breaks() -> None:
    html = markdown_render.render_plain_text("第一行\n第二行")

    assert "第一行\n第二行" in html


def test_public_text_body_survives_code_fence(owner_client: TestClient, client: TestClient) -> None:
    body = "部署说明：\n```\n# 上面这一行是示例，不是标题\n```\n结束"
    response = owner_client.post(
        f"{API}/assets",
        json={"kind": "text", "body": body, "slug": "notice"},
        headers=admin_headers(owner_client),
    )
    asset = response.json()
    detail = owner_client.get(f"{API}/assets/{asset['id']}").json()
    detail = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": detail["revision"], "visibility": "public"},
        headers=admin_headers(owner_client),
    ).json()
    owner_client.post(
        f"{API}/assets/{asset['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    html = client.get(f"{PUBLIC}/by-slug/notice").json()["body"]["html"]

    assert html.startswith("<pre>")
    assert "```" in html
    assert "<h1" not in html
    assert "结束" in html


def test_public_text_body_escapes_script(owner_client: TestClient, client: TestClient) -> None:
    response = owner_client.post(
        f"{API}/assets",
        json={"kind": "text", "body": "<script>alert(1)</script> 公告", "slug": "alert"},
        headers=admin_headers(owner_client),
    )
    asset = response.json()
    detail = owner_client.get(f"{API}/assets/{asset['id']}").json()
    detail = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": detail["revision"], "visibility": "public"},
        headers=admin_headers(owner_client),
    ).json()
    owner_client.post(
        f"{API}/assets/{asset['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    html = client.get(f"{PUBLIC}/by-slug/alert").json()["body"]["html"]

    assert "<script" not in html
    assert "&lt;script&gt;" in html


def test_text_markdown_format_returns_source(owner_client: TestClient, client: TestClient) -> None:
    """站点令牌要原文时拿到的就是原文，不做任何转义或渲染。"""
    body = "原文：\n```\n代码\n```"
    response = owner_client.post(
        f"{API}/assets",
        json={"kind": "text", "body": body, "slug": "raw-text"},
        headers=admin_headers(owner_client),
    )
    asset = response.json()
    detail = owner_client.get(f"{API}/assets/{asset['id']}").json()
    detail = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": detail["revision"], "visibility": "public"},
        headers=admin_headers(owner_client),
    ).json()
    owner_client.post(
        f"{API}/assets/{asset['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    site = owner_client.post(
        f"{API}/sites", json={"key": "main-site"}, headers=admin_headers(owner_client)
    ).json()
    token = owner_client.post(
        f"{API}/tokens",
        json={"subject_type": "site", "subject_key": "main-site"},
        headers=admin_headers(owner_client),
    ).json()["token"]
    assert site["id"]

    from fastapi.testclient import TestClient as NewClient

    body_json = (
        NewClient(client.app, headers={"Authorization": f"Bearer {token}"})
        .get(f"{PUBLIC}/assets/{asset['id']}?format=markdown")
        .json()
    )

    assert body_json["body"]["markdown"] == body


# —— 彻底删除要检查集合引用 ——


def _image(owner_client: TestClient, name: str = "pic.png") -> dict:
    response = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": (name, minimal_png(), "image/png")},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _collection(owner_client: TestClient, slug: str) -> dict:
    response = owner_client.post(
        f"{API}/collections",
        json={"slug": slug, "title": slug},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_purge_refuses_when_still_in_a_collection(owner_client: TestClient) -> None:
    created = _image(owner_client)
    collection = _collection(owner_client, "home.hero")
    owner_client.post(
        f"{API}/collections/{collection['id']}/items",
        json={"asset_id": created["asset_id"]},
        headers=admin_headers(owner_client),
    )
    trashed = owner_client.post(
        f"{API}/assets/{created['asset_id']}/trash",
        json={"revision": 1},
        headers=admin_headers(owner_client),
    )
    assert trashed.status_code == 200

    response = owner_client.delete(
        f"{API}/assets/{created['asset_id']}", headers=admin_headers(owner_client)
    )

    assert response.status_code == 409
    assert "home.hero" in response.json()["error"]["message"]


def test_purge_works_after_leaving_the_collection(owner_client: TestClient, storage) -> None:
    created = _image(owner_client)
    collection = _collection(owner_client, "home.hero")
    owner_client.post(
        f"{API}/collections/{collection['id']}/items",
        json={"asset_id": created["asset_id"]},
        headers=admin_headers(owner_client),
    )
    owner_client.post(
        f"{API}/assets/{created['asset_id']}/trash",
        json={"revision": 1},
        headers=admin_headers(owner_client),
    )
    owner_client.delete(
        f"{API}/collections/{collection['id']}/items/{created['asset_id']}",
        headers=admin_headers(owner_client),
    )

    response = owner_client.delete(
        f"{API}/assets/{created['asset_id']}", headers=admin_headers(owner_client)
    )

    assert response.status_code == 204
    assert not storage.original_path(created["storage_key"]).exists()


def test_purge_reports_cover_and_collection_together(owner_client: TestClient) -> None:
    image = _image(owner_client, "cover.png")
    article = owner_client.post(
        f"{API}/assets",
        json={"kind": "markdown", "body": "# 文章"},
        headers=admin_headers(owner_client),
    ).json()
    owner_client.patch(
        f"{API}/assets/{article['id']}",
        json={"revision": 1, "cover_asset_id": image["asset_id"]},
        headers=admin_headers(owner_client),
    )
    collection = _collection(owner_client, "blog.covers")
    owner_client.post(
        f"{API}/collections/{collection['id']}/items",
        json={"asset_id": image["asset_id"]},
        headers=admin_headers(owner_client),
    )
    owner_client.post(
        f"{API}/assets/{image['asset_id']}/trash",
        json={"revision": 1},
        headers=admin_headers(owner_client),
    )

    response = owner_client.delete(
        f"{API}/assets/{image['asset_id']}", headers=admin_headers(owner_client)
    )

    assert response.status_code == 409
    message = response.json()["error"]["message"]
    assert "封面" in message
    assert "blog.covers" in message
