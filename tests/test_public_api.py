"""集合与网站只读契约（规划第 6 部分验收项）。

覆盖：可见性矩阵（匿名 / 站点令牌）、站点隔离、前缀按点号分段匹配、
Markdown 消毒、图片撤回后从 HTML 去掉、缓存头与 ETag、别名读取、表格预览形状。
"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import admin_headers
from tests.inspection_helpers import minimal_png

from ctrlstore import audit

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


# —— 准备数据的工具 ——


def _site(client: TestClient, key: str) -> dict:
    response = client.post(f"{API}/sites", json={"key": key}, headers=admin_headers(client))
    assert response.status_code in (200, 201), response.text
    return response.json()


def _token(client: TestClient, site_key: str) -> str:
    response = client.post(
        f"{API}/tokens",
        json={"subject_type": "site", "subject_key": site_key, "name": "read"},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _collection(client: TestClient, slug: str, **extra) -> dict:
    response = client.post(
        f"{API}/collections",
        json={"slug": slug, "title": slug, **extra},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _asset(client: TestClient, *, kind: str = "image", visibility: str = "public", **extra) -> dict:
    """建一条内容并发布。``kind=image`` 走上传，其余直接创建文本。"""
    if kind == "image":
        created = client.post(
            f"{API}/uploads/direct",
            files={"file": ("pic.png", minimal_png(), "image/png")},
            headers=admin_headers(client),
        ).json()
        asset_id = created["asset_id"]
    else:
        payload = {"kind": kind, "body": extra.pop("body", "# 标题\n\n正文"), **extra}
        response = client.post(f"{API}/assets", json=payload, headers=admin_headers(client))
        assert response.status_code == 201, response.text
        asset_id = response.json()["id"]

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
    return published.json()


def _add(client: TestClient, collection_id: str, asset_id: str, **extra) -> None:
    response = client.post(
        f"{API}/collections/{collection_id}/items",
        json={"asset_id": asset_id, **extra},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text


def _grant(client: TestClient, site_id: str, prefix: str, *, unlisted: bool = False) -> dict:
    response = client.post(
        f"{API}/sites/{site_id}/grants",
        json={"prefix": prefix, "can_read_unlisted": unlisted},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _revoke(client: TestClient, site_id: str, grant_id: str) -> None:
    response = client.delete(
        f"{API}/sites/{site_id}/grants/{grant_id}", headers=admin_headers(client)
    )
    assert response.status_code == 204, response.text


def _site_client(client: TestClient, token: str) -> TestClient:
    """带站点令牌的客户端（不共用 Cookie）。"""
    from fastapi.testclient import TestClient as NewClient

    return NewClient(client.app, headers={"Authorization": f"Bearer {token}"})


# —— 集合读取与可见性矩阵 ——


def test_anonymous_collection_returns_only_public(
    owner_client: TestClient, client: TestClient
) -> None:
    collection = _collection(owner_client, "home.hero")
    public = _asset(owner_client, visibility="public")
    unlisted = _asset(owner_client, visibility="unlisted")
    site_only = _asset(owner_client, visibility="site")
    for asset in (public, unlisted, site_only):
        _add(owner_client, collection["id"], asset["id"])

    body = client.get(f"{PUBLIC}/collections/home.hero").json()

    assert [item["asset"]["id"] for item in body["items"]] == [public["id"]]


def test_site_token_sees_site_only_inside_granted_collection(
    owner_client: TestClient, client: TestClient
) -> None:
    site = _site(owner_client, "main-site")
    token = _token(owner_client, "main-site")
    _grant(owner_client, site["id"], "home")
    collection = _collection(owner_client, "home.hero")
    hidden = _asset(owner_client, visibility="site")
    _add(owner_client, collection["id"], hidden["id"])

    body = _site_client(client, token).get(f"{PUBLIC}/collections/home.hero").json()

    assert [item["asset"]["id"] for item in body["items"]] == [hidden["id"]]


def test_site_token_cannot_read_another_sites_collection(
    owner_client: TestClient, client: TestClient
) -> None:
    """集合归属决定可见性：属于别的站点的集合，本站点读不到。

    库级集合（不带 site_id）对所有调用方开放，那是公开内容的正常入口。
    """
    _site(owner_client, "main-site")
    other = _site(owner_client, "other-site")
    token = _token(owner_client, "main-site")
    _collection(owner_client, "other.hero", site_id=other["id"])

    response = _site_client(client, token).get(f"{PUBLIC}/collections/other.hero")

    assert response.status_code == 404


def test_prefix_matches_on_dot_boundaries(owner_client: TestClient, client: TestClient) -> None:
    """``home`` 覆盖 ``home.hero``，不覆盖 ``homepage``。

    这里必须用「仅站点」内容来验：集合本身是公开入口，前缀管的是内容授权。
    """
    site = _site(owner_client, "main-site")
    token = _token(owner_client, "main-site")
    _grant(owner_client, site["id"], "home")

    inside = _collection(owner_client, "home.hero")
    outside = _collection(owner_client, "homepage")
    allowed = _asset(owner_client, visibility="site")
    blocked = _asset(owner_client, visibility="site")
    _add(owner_client, inside["id"], allowed["id"])
    _add(owner_client, outside["id"], blocked["id"])

    site_client = _site_client(client, token)

    assert site_client.get(f"{PUBLIC}/assets/{allowed['id']}").status_code == 200
    assert site_client.get(f"{PUBLIC}/assets/{blocked['id']}").status_code == 404


def test_two_sites_do_not_see_each_others_collections(
    owner_client: TestClient, client: TestClient
) -> None:
    first = _site(owner_client, "site-a")
    second = _site(owner_client, "site-b")
    token_a = _token(owner_client, "site-a")
    token_b = _token(owner_client, "site-b")
    _collection(owner_client, "a.hero", site_id=first["id"])
    _collection(owner_client, "b.hero", site_id=second["id"])

    a = _site_client(client, token_a)
    b = _site_client(client, token_b)

    assert a.get(f"{PUBLIC}/collections/a.hero").status_code == 200
    assert a.get(f"{PUBLIC}/collections/b.hero").status_code == 404
    assert b.get(f"{PUBLIC}/collections/b.hero").status_code == 200
    assert b.get(f"{PUBLIC}/collections/a.hero").status_code == 404


def test_unlisted_listed_only_with_grant(owner_client: TestClient, client: TestClient) -> None:
    site = _site(owner_client, "main-site")
    token = _token(owner_client, "main-site")
    grant = _grant(owner_client, site["id"], "home")
    collection = _collection(owner_client, "home.hero")
    unlisted = _asset(owner_client, visibility="unlisted")
    _add(owner_client, collection["id"], unlisted["id"])

    without = _site_client(client, token).get(f"{PUBLIC}/collections/home.hero").json()
    assert without["items"] == []

    # 同一个前缀只能有一条授权：先撤掉再按「允许未列出」重建。
    _revoke(owner_client, site["id"], grant["id"])
    _grant(owner_client, site["id"], "home", unlisted=True)

    with_grant = _site_client(client, token).get(f"{PUBLIC}/collections/home.hero").json()
    assert [item["asset"]["id"] for item in with_grant["items"]] == [unlisted["id"]]


# —— 单条内容 ——


def test_anonymous_can_read_public_asset(owner_client: TestClient, client: TestClient) -> None:
    asset = _asset(owner_client, visibility="public")

    response = client.get(f"{PUBLIC}/assets/{asset['id']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == asset["id"]
    assert payload["urls"]["original"].endswith(f"/p/{asset['id']}/original.png")


def test_private_asset_is_invisible(owner_client: TestClient, client: TestClient) -> None:
    asset = _asset(owner_client, visibility="private")

    assert client.get(f"{PUBLIC}/assets/{asset['id']}").status_code == 404


def test_draft_asset_is_invisible(owner_client: TestClient, client: TestClient) -> None:
    created = owner_client.post(
        f"{API}/assets",
        json={"kind": "markdown", "body": "# 草稿"},
        headers=admin_headers(owner_client),
    ).json()

    assert client.get(f"{PUBLIC}/assets/{created['id']}").status_code == 404


def test_site_only_asset_readable_by_granted_site(
    owner_client: TestClient, client: TestClient
) -> None:
    site = _site(owner_client, "main-site")
    token = _token(owner_client, "main-site")
    _grant(owner_client, site["id"], "home")
    collection = _collection(owner_client, "home.hero")
    asset = _asset(owner_client, visibility="site")
    _add(owner_client, collection["id"], asset["id"])

    anonymous = client.get(f"{PUBLIC}/assets/{asset['id']}")
    assert anonymous.status_code == 404

    authorised = _site_client(client, token).get(f"{PUBLIC}/assets/{asset['id']}")
    assert authorised.status_code == 200


def test_site_only_asset_without_collection_is_invisible_to_site(
    owner_client: TestClient, client: TestClient
) -> None:
    """「仅站点」还必须出现在已授权集合里；只发布、不进集合，站点也读不到。"""
    site = _site(owner_client, "main-site")
    token = _token(owner_client, "main-site")
    _grant(owner_client, site["id"], "home")
    asset = _asset(owner_client, visibility="site")

    assert _site_client(client, token).get(f"{PUBLIC}/assets/{asset['id']}").status_code == 404


def test_invalid_token_is_401_not_anonymous(owner_client: TestClient, client: TestClient) -> None:
    asset = _asset(owner_client, visibility="public")

    response = client.get(
        f"{PUBLIC}/assets/{asset['id']}", headers={"Authorization": "Bearer nope.nope"}
    )

    assert response.status_code == 401


# —— 别名与正文 ——


def test_by_slug_returns_article(owner_client: TestClient, client: TestClient) -> None:
    body = "# 欢迎\n\n第一段摘要与正文。"
    asset = _asset(owner_client, kind="markdown", visibility="public", body=body, slug="hello")
    owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": asset["revision"], "slug": "hello-ctrlstore"},
        headers=admin_headers(owner_client),
    )
    detail = owner_client.get(f"{API}/assets/{asset['id']}").json()
    owner_client.post(
        f"{API}/assets/{asset['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    response = client.get(f"{PUBLIC}/by-slug/hello-ctrlstore")

    assert response.status_code == 200
    payload = response.json()
    assert payload["slug"] == "hello-ctrlstore"
    assert payload["body"]["format"] == "html"
    assert "<h1>" in payload["body"]["html"]


def test_markdown_html_is_sanitised(owner_client: TestClient, client: TestClient) -> None:
    body = (
        "# 标题\n\n<script>alert(1)</script>\n\n[点我](javascript:alert(1))\n\n"
        "<img src=x onerror=alert(1)>"
    )
    asset = _asset(owner_client, kind="markdown", visibility="public", body=body, slug="xss")

    html = client.get(f"{PUBLIC}/assets/{asset['id']}").json()["body"]["html"]

    assert "<script" not in html
    assert "onerror" not in html
    assert "javascript:" not in html


def test_markdown_images_use_absolute_original_url(
    owner_client: TestClient, client: TestClient
) -> None:
    image = _asset(owner_client, visibility="public")
    body = f"# 有图\n\n![主图](asset:{image['id']})"
    article = _asset(
        owner_client, kind="markdown", visibility="public", body=body, slug="with-image"
    )

    html = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]

    assert f"/p/{image['id']}/original.png" in html
    assert "http://" in html  # 绝对地址


def test_external_images_are_removed(owner_client: TestClient, client: TestClient) -> None:
    body = "# 外链\n\n![外站](https://example.com/x.png)"
    asset = _asset(owner_client, kind="markdown", visibility="public", body=body, slug="external")

    html = client.get(f"{PUBLIC}/assets/{asset['id']}").json()["body"]["html"]

    assert "example.com" not in html
    assert "<img" not in html


def test_image_unpublished_is_removed_from_article(
    owner_client: TestClient, client: TestClient
) -> None:
    """图片被撤回后，文章仍在，公开 HTML 里去掉那张图（企划第 8.4 节 T4）。"""
    image = _asset(owner_client, visibility="public")
    body = f"# 文章\n\n前文\n\n![图](asset:{image['id']})\n\n后文"
    article = _asset(
        owner_client, kind="markdown", visibility="public", body=body, slug="drop-image"
    )
    before = client.get(f"{PUBLIC}/assets/{article['id']}").json()["body"]["html"]
    assert "<img" in before

    detail = owner_client.get(f"{API}/assets/{image['id']}").json()
    owner_client.post(
        f"{API}/assets/{image['id']}/unpublish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    after = client.get(f"{PUBLIC}/assets/{article['id']}").json()
    assert "<img" not in after["body"]["html"]
    assert "后文" in after["body"]["html"]


def test_markdown_format_needs_site_token(owner_client: TestClient, client: TestClient) -> None:
    asset = _asset(owner_client, kind="markdown", visibility="public", body="# 原文", slug="raw")

    assert client.get(f"{PUBLIC}/assets/{asset['id']}?format=markdown").status_code == 403


def test_site_token_can_get_markdown_source(owner_client: TestClient, client: TestClient) -> None:
    _site(owner_client, "main-site")
    token = _token(owner_client, "main-site")
    asset = _asset(
        owner_client, kind="markdown", visibility="public", body="# 原文正文", slug="raw"
    )

    body = _site_client(client, token).get(f"{PUBLIC}/assets/{asset['id']}?format=markdown").json()

    assert body["body"]["format"] == "markdown"
    assert "原文正文" in body["body"]["markdown"]


def test_text_asset_body_is_rendered_as_text(owner_client: TestClient, client: TestClient) -> None:
    _asset(owner_client, kind="text", visibility="public", body="公告：今晚维护", slug="notice")

    payload = client.get(f"{PUBLIC}/by-slug/notice").json()

    assert payload["body"]["format"] == "html"
    assert "今晚维护" in payload["body"]["html"]


def test_front_matter_is_not_rendered_as_body(owner_client: TestClient, client: TestClient) -> None:
    """front matter 是元数据，不该出现在正文里（曾经被当成 <hr> 段落渲染出去）。"""
    body = "---\ntitle: 带元数据的标题\nslug: with-front-matter\n---\n\n# 正文\n\n只留正文。"
    _asset(owner_client, kind="markdown", visibility="public", body=body, slug="with-front-matter")

    payload = client.get(f"{PUBLIC}/by-slug/with-front-matter").json()

    html = payload["body"]["html"]
    assert payload["title"] == "带元数据的标题"
    assert "title:" not in html
    assert "<hr" not in html
    assert "只留正文" in html


def test_table_preview_shape(owner_client: TestClient, client: TestClient) -> None:
    created = owner_client.post(
        f"{API}/assets", json={"kind": "text", "body": "占位"}, headers=admin_headers(owner_client)
    ).json()
    detail = owner_client.get(f"{API}/assets/{created['id']}").json()
    owner_client.patch(
        f"{API}/assets/{created['id']}",
        json={"revision": detail["revision"], "visibility": "public"},
        headers=admin_headers(owner_client),
    )
    detail = owner_client.get(f"{API}/assets/{created['id']}").json()
    owner_client.post(
        f"{API}/assets/{created['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    response = client.get(f"{PUBLIC}/assets/{created['id']}/table")

    assert response.status_code == 422  # 不是 xlsx


# —— 缓存 ——


def test_anonymous_response_is_cacheable(owner_client: TestClient, client: TestClient) -> None:
    asset = _asset(owner_client, visibility="public")

    response = client.get(f"{PUBLIC}/assets/{asset['id']}")

    assert response.headers["cache-control"] == "public, max-age=60"
    assert response.headers["etag"].startswith('W/"')


def test_etag_yields_304(owner_client: TestClient, client: TestClient) -> None:
    asset = _asset(owner_client, visibility="public")
    first = client.get(f"{PUBLIC}/assets/{asset['id']}")

    second = client.get(
        f"{PUBLIC}/assets/{asset['id']}", headers={"If-None-Match": first.headers["etag"]}
    )

    assert second.status_code == 304


def test_token_response_is_private(owner_client: TestClient, client: TestClient) -> None:
    site = _site(owner_client, "main-site")
    token = _token(owner_client, "main-site")
    _grant(owner_client, site["id"], "home")
    asset = _asset(owner_client, visibility="public")

    response = _site_client(client, token).get(f"{PUBLIC}/assets/{asset['id']}")

    assert response.headers["cache-control"] == "private, no-store"
    assert "etag" in response.headers


# —— 发布快照 ——


def test_public_never_exposes_editing_fields(owner_client: TestClient, client: TestClient) -> None:
    """编辑中的字段不出现在公开响应里；线上读的是发布快照。"""
    asset = _asset(owner_client, visibility="public")
    detail = owner_client.get(f"{API}/assets/{asset['id']}").json()
    owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": detail["revision"], "title": "改过的标题", "summary": "改过的摘要"},
        headers=admin_headers(owner_client),
    )

    payload = client.get(f"{PUBLIC}/assets/{asset['id']}").json()

    assert payload["title"] != "改过的标题"


def test_unpublished_replacement_does_not_change_public_html(
    owner_client: TestClient, client: TestClient
) -> None:
    asset = _asset(
        owner_client, kind="markdown", visibility="public", body="# 线上版本", slug="stable"
    )
    before = client.get(f"{PUBLIC}/assets/{asset['id']}").json()["body"]["html"]

    detail = owner_client.get(f"{API}/assets/{asset['id']}").json()
    owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": detail["revision"], "body": "# 待发布版本"},
        headers=admin_headers(owner_client),
    )
    after = client.get(f"{PUBLIC}/assets/{asset['id']}").json()["body"]["html"]

    assert after == before


def test_publishing_switches_public_html(owner_client: TestClient, client: TestClient) -> None:
    asset = _asset(
        owner_client, kind="markdown", visibility="public", body="# 线上版本", slug="switch"
    )
    detail = owner_client.get(f"{API}/assets/{asset['id']}").json()
    detail = owner_client.patch(
        f"{API}/assets/{asset['id']}",
        json={"revision": detail["revision"], "body": "# 新版本"},
        headers=admin_headers(owner_client),
    ).json()

    owner_client.post(
        f"{API}/assets/{asset['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    html = client.get(f"{PUBLIC}/assets/{asset['id']}").json()["body"]["html"]
    assert "新版本" in html


# —— 集合管理 ——


def test_duplicate_asset_in_collection_is_rejected(owner_client: TestClient) -> None:
    collection = _collection(owner_client, "dup.test")
    asset = _asset(owner_client)
    _add(owner_client, collection["id"], asset["id"])

    response = owner_client.post(
        f"{API}/collections/{collection['id']}/items",
        json={"asset_id": asset["id"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 409


def test_kind_limit_is_enforced(owner_client: TestClient) -> None:
    collection = _collection(owner_client, "only.images", allowed_kinds=["image"])
    text = _asset(owner_client, kind="markdown", body="# 文本")

    response = owner_client.post(
        f"{API}/collections/{collection['id']}/items",
        json={"asset_id": text["id"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422


def test_tightening_kind_limit_rejects_existing_items(owner_client: TestClient) -> None:
    collection = _collection(owner_client, "mixed")
    text = _asset(owner_client, kind="markdown", body="# 文本")
    _add(owner_client, collection["id"], text["id"])

    response = owner_client.patch(
        f"{API}/collections/{collection['id']}",
        json={"allowed_kinds": ["image"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 409


def test_reorder_applies_whole_order(owner_client: TestClient) -> None:
    collection = _collection(owner_client, "order.test")
    assets = [_asset(owner_client) for _ in range(3)]
    for asset in assets:
        _add(owner_client, collection["id"], asset["id"])

    reversed_ids = [asset["id"] for asset in reversed(assets)]
    response = owner_client.put(
        f"{API}/collections/{collection['id']}/order",
        json={"asset_ids": reversed_ids},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 200
    assert [item["asset_id"] for item in response.json()] == reversed_ids
    assert [item["position"] for item in response.json()] == [0, 1, 2]


def test_reorder_rejects_incomplete_list(owner_client: TestClient) -> None:
    collection = _collection(owner_client, "order.bad")
    asset = _asset(owner_client)
    _add(owner_client, collection["id"], asset["id"])

    response = owner_client.put(
        f"{API}/collections/{collection['id']}/order",
        json={"asset_ids": []},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422


def test_collection_requires_manage_permission(
    client: TestClient, db: DbSession, owner_client
) -> None:
    from tests.auth_helpers import EDITOR_PASSWORD, add_editor, login

    add_editor(db, username="reader", permissions=["asset.read"])
    client.cookies.clear()
    login(client, "reader", EDITOR_PASSWORD)

    response = client.post(
        f"{API}/collections", json={"slug": "x.y"}, headers=admin_headers(client)
    )

    assert response.status_code == 403


def test_collection_actions_are_audited(owner_client: TestClient, db: DbSession) -> None:
    collection = _collection(owner_client, "audit.test")
    asset = _asset(owner_client)
    _add(owner_client, collection["id"], asset["id"])

    actions = {row.action for row in db.query(audit_row_model()).all()}
    assert audit.COLLECTION_CREATED in actions
    assert audit.COLLECTION_ITEM_ADDED in actions


def audit_row_model():
    from ctrlstore.models import AuditLog

    return AuditLog
