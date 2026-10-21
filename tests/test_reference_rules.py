"""复验问题清单第 1、2 条：引用规则的两处边界。

1. 正文里的**下载链接**（指向已发布的 PDF / 纯文本）不该被当成未发布图片，
   否则文章无法发布。T4 只约束图片。
2. 授权站点读含「仅站点」图片的公开文章时，图片要保留，``src`` 是可取的签名地址。
   上一版比较 HTML 里的 ``&amp;`` 与签名地址里的 ``&``，把图删掉了。
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

from ctrlstore.assets import markdown

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


# —— 解析：只认图片 ——


def test_image_ids_ignores_plain_links() -> None:
    body = "请下载 [手册](/p/11111111-1111-1111-1111-111111111111/original.txt)。"

    assert markdown.image_ids(body) == []


def test_image_ids_finds_image_syntax_and_img_tags() -> None:
    first = "11111111-1111-1111-1111-111111111111"
    second = "22222222-2222-2222-2222-222222222222"
    body = f'![甲](asset:{first})\n\n<img src="/p/{second}/original.png">'

    assert markdown.image_ids(body) == [first, second]


def test_image_ids_ignores_code_blocks() -> None:
    body = (
        "示例：\n\n```markdown\n![别照抄](asset:33333333-3333-3333-3333-333333333333)\n```\n\n"
        "行内 `![也别照抄](asset:44444444-4444-4444-4444-444444444444)` 结束"
    )

    assert markdown.image_ids(body) == []


def test_image_ids_ignores_external_image() -> None:
    assert markdown.image_ids("![外站](https://example.com/x.png)") == []


# —— 发布：链接不挡发布 ——


def _text(client: TestClient, visibility: str = "public", slug: str | None = None) -> dict:
    payload = {"kind": "text", "body": "手册正文"}
    if slug:
        payload["slug"] = slug
    created = client.post(f"{API}/assets", json=payload, headers=admin_headers(client))
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
    return published.json()


def _article(client: TestClient, body: str) -> dict:
    created = client.post(
        f"{API}/assets",
        json={"kind": "markdown", "body": body},
        headers=admin_headers(client),
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    detail = client.get(f"{API}/assets/{asset_id}").json()
    detail = client.patch(
        f"{API}/assets/{asset_id}",
        json={"revision": detail["revision"], "visibility": "public"},
        headers=admin_headers(client),
    ).json()
    published = client.post(
        f"{API}/assets/{asset_id}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(client),
    )
    return {"asset_id": asset_id, "response": published, **published.json()}


def test_published_text_link_does_not_block_article_publish(
    owner_client: TestClient, client: TestClient
) -> None:
    """复现第 1 条：链接一条已发布纯文本，文章必须能发布。"""
    manual = _text(owner_client)
    body = f"请下载 [手册](/p/{manual['id']}/original.txt)。\n\n正文结束。"
    article = _article(owner_client, body)

    assert article["response"].status_code == 200, article["response"].text

    html = client.get(f"{PUBLIC}/assets/{article['asset_id']}").json()["body"]["html"]
    assert "<a " in html
    assert "手册" in html
    assert "正文结束" in html


def test_link_to_published_text_is_absolute(owner_client: TestClient, client: TestClient) -> None:
    """站点可能不在同一个域名下，链接也要补成绝对地址。"""
    manual = _text(owner_client)
    article = _article(owner_client, f"[手册](/p/{manual['id']}/original.txt)")

    html = client.get(f"{PUBLIC}/assets/{article['asset_id']}").json()["body"]["html"]

    assert f'href="http://127.0.0.1:8000/p/{manual["id"]}/original.txt"' in html


def test_unpublished_image_still_blocks_publish(owner_client: TestClient, db: DbSession) -> None:
    """图片的规则不变：未发布的图片不能让文章带着它公开。"""
    image = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("draft.png", minimal_png(), "image/png")},
        headers=admin_headers(owner_client),
    ).json()
    body = f"![图](asset:{image['asset_id']})"
    created = owner_client.post(
        f"{API}/assets",
        json={"kind": "markdown", "body": body},
        headers=admin_headers(owner_client),
    ).json()
    detail = owner_client.get(f"{API}/assets/{created['id']}").json()

    response = owner_client.post(
        f"{API}/assets/{created['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 409
    assert image["asset_id"] in response.json()["error"]["message"]


def test_image_syntax_to_published_text_is_rejected(owner_client: TestClient) -> None:
    """``![](/p/<text-id>/…)`` 是写法错误：不是图片。选定策略是拒绝，并说清原因。"""
    manual = _text(owner_client)
    created = owner_client.post(
        f"{API}/assets",
        json={"kind": "markdown", "body": f"![](/p/{manual['id']}/original.txt)"},
        headers=admin_headers(owner_client),
    ).json()
    detail = owner_client.get(f"{API}/assets/{created['id']}").json()

    response = owner_client.post(
        f"{API}/assets/{created['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 409
    assert "不是图片" in response.json()["error"]["message"]


def test_link_to_missing_target_does_not_block_publish(
    owner_client: TestClient, client: TestClient
) -> None:
    """链接不做存在性校验：目标写错是作者的事，不该让整篇文章发不出去。"""
    missing = "99999999-9999-9999-9999-999999999999"
    article = _article(owner_client, f"[失效链接](/p/{missing}/original.pdf)")

    assert article["response"].status_code == 200
    html = client.get(f"{PUBLIC}/assets/{article['asset_id']}").json()["body"]["html"]
    assert "失效链接" in html


# —— 授权站点读含「仅站点」图片的文章 ——


def _site_setup(client: TestClient) -> tuple[dict, str, dict]:
    """建站点 + 令牌 + 授权 + 集合，返回站点、令牌与集合。"""
    site = client.post(
        f"{API}/sites", json={"key": "main-site"}, headers=admin_headers(client)
    ).json()
    token = client.post(
        f"{API}/tokens",
        json={"subject_type": "site", "subject_key": "main-site"},
        headers=admin_headers(client),
    ).json()["token"]
    client.post(
        f"{API}/sites/{site['id']}/grants",
        json={"prefix": "home"},
        headers=admin_headers(client),
    )
    collection = client.post(
        f"{API}/collections",
        json={"slug": "home.hero", "title": "首页"},
        headers=admin_headers(client),
    ).json()
    return site, token, collection


def _site_only_image(client: TestClient, collection: dict) -> dict:
    upload = client.post(
        f"{API}/uploads/direct",
        files={"file": ("inner.png", minimal_png(), "image/png")},
        headers=admin_headers(client),
    ).json()
    asset_id = upload["asset_id"]
    detail = client.get(f"{API}/assets/{asset_id}").json()
    detail = client.patch(
        f"{API}/assets/{asset_id}",
        json={"revision": detail["revision"], "visibility": "site"},
        headers=admin_headers(client),
    ).json()
    published = client.post(
        f"{API}/assets/{asset_id}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(client),
    )
    assert published.status_code == 200, published.text
    client.post(
        f"{API}/collections/{collection['id']}/items",
        json={"asset_id": asset_id},
        headers=admin_headers(client),
    )
    return {"asset_id": asset_id, **published.json()}


def test_site_token_keeps_site_only_image_in_article(
    owner_client: TestClient, client: TestClient
) -> None:
    """复现第 2 条：授权站点读文章时，仅站点图片要保留且可取。"""
    from fastapi.testclient import TestClient as NewClient

    _, token, collection = _site_setup(owner_client)
    image = _site_only_image(owner_client, collection)
    body = f"# 文章\n\n![内部](asset:{image['asset_id']})\n\n后文。"
    article = _article(owner_client, body)

    site_client = NewClient(client.app, headers={"Authorization": f"Bearer {token}"})
    html = site_client.get(f"{PUBLIC}/assets/{article['asset_id']}").json()["body"]["html"]

    assert "<img" in html
    assert "后文" in html
    # src 是可取的签名地址
    source = html.split('src="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    fetched = site_client.get(source.replace("http://127.0.0.1:8000", ""))
    assert fetched.status_code == 200


def test_anonymous_article_drops_site_only_image(
    owner_client: TestClient, client: TestClient
) -> None:
    _, _, collection = _site_setup(owner_client)
    image = _site_only_image(owner_client, collection)
    article = _article(owner_client, f"# 文章\n\n![内部](asset:{image['asset_id']})\n\n后文。")

    html = client.get(f"{PUBLIC}/assets/{article['asset_id']}").json()["body"]["html"]

    assert "<img" not in html
    assert "后文" in html


def test_site_only_image_dropped_after_unpublish(
    owner_client: TestClient, client: TestClient
) -> None:
    from fastapi.testclient import TestClient as NewClient

    _, token, collection = _site_setup(owner_client)
    image = _site_only_image(owner_client, collection)
    article = _article(owner_client, f"# 文章\n\n![内部](asset:{image['asset_id']})\n\n后文。")

    site_client = NewClient(client.app, headers={"Authorization": f"Bearer {token}"})
    assert (
        "<img" in site_client.get(f"{PUBLIC}/assets/{article['asset_id']}").json()["body"]["html"]
    )

    detail = owner_client.get(f"{API}/assets/{image['asset_id']}").json()
    owner_client.post(
        f"{API}/assets/{image['asset_id']}/unpublish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    html = site_client.get(f"{PUBLIC}/assets/{article['asset_id']}").json()["body"]["html"]
    assert "<img" not in html
    assert "后文" in html


# —— 渲染单元：签名地址里的 & 不能导致图片被删 ——


def test_signed_style_url_survives_sanitising() -> None:
    from ctrlstore.content import markdown_render

    signed = "http://h/api/public/v1/files/11111111-1111-1111-1111-111111111111?version=1&expires=2&sig=abc"
    rendered = markdown_render.render(
        "![x](asset:11111111-1111-1111-1111-111111111111)",
        image_url=lambda _id: signed,
    )

    assert "<img" in rendered.html
    assert "&amp;" in rendered.html  # 消毒后的形态
    assert rendered.asset_ids == ["11111111-1111-1111-1111-111111111111"]
