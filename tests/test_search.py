"""检索（规划第 9 部分）。

覆盖：三字及以上全文、一两个字只查标题/文件名/标签、回收站不出现、
站点搜索默认关闭且需要令牌、只搜发布快照与已发布版本、待发布替换不可见、
仅站点内容必须落在已授权集合、未列出内容不出现在搜索里。
"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import EDITOR_PASSWORD, add_editor, admin_headers, login
from tests.file_samples import docx_bytes
from tests.test_public_api import (
    _add,
    _collection,
    _grant,
    _site,
    _site_client,
    _token,
)
from tests.test_publish import _publish, _text

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


def _search(client: TestClient, q: str, **params) -> dict:
    response = client.get(f"{API}/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _site_search(client: TestClient, q: str) -> dict:
    response = client.get(f"{PUBLIC}/search", params={"q": q})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body: dict) -> set[str]:
    return {item["id"] for item in body["items"]}


# —— 管理端 ——


def test_long_query_searches_body(
    owner_client: TestClient, settings
) -> None:
    article = _text(owner_client, body="# 标题\n\n这是一篇关于萤火虫计划的说明")
    run_until_idle(settings)

    body = _search(owner_client, "萤火虫计划")

    assert body["mode"] == "fts"
    assert article["id"] in _ids(body)
    # 三字同样走全文。
    assert _search(owner_client, "萤火虫")["mode"] == "fts"


def test_short_query_uses_title_filename_and_tags(
    owner_client: TestClient, settings
) -> None:
    upload = owner_client.post(
        f"{API}/uploads/direct",
        files={
            "file": (
                "年度合同.docx",
                docx_bytes("正文与关键词无关"),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers=admin_headers(owner_client),
    )
    assert upload.status_code == 201, upload.text
    tagged = _text(owner_client, body="# 另一篇\n\n正文", tags=["年度"], title="另一篇")
    run_until_idle(settings)

    by_filename = _search(owner_client, "合同")
    assert by_filename["mode"] == "title"
    assert upload.json()["asset_id"] in _ids(by_filename)

    by_tag = _search(owner_client, "年度")
    assert tagged["id"] in _ids(by_tag)


def test_short_query_does_not_match_body(
    owner_client: TestClient, settings
) -> None:
    article = _text(owner_client, body="# 标题\n\n萤火虫")
    run_until_idle(settings)

    body = _search(owner_client, "萤火")

    assert body["mode"] == "title"
    assert article["id"] not in _ids(body)


def test_trashed_assets_disappear_from_search(
    owner_client: TestClient, settings
) -> None:
    article = _text(owner_client, body="# 标题\n\n独一无二的关键词")
    run_until_idle(settings)
    assert article["id"] in _ids(_search(owner_client, "独一无二的关键词"))

    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    owner_client.post(
        f"{API}/assets/{article['id']}/trash",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    assert article["id"] not in _ids(_search(owner_client, "独一无二的关键词"))


def test_editor_with_read_permission_can_search(
    owner_client: TestClient, client: TestClient, db: DbSession, settings
) -> None:
    article = _text(owner_client, body="# 标题\n\n检索权限验证关键词")
    run_until_idle(settings)
    add_editor(db, username="reader", permissions=("asset.read",))
    login(client, "reader", EDITOR_PASSWORD)

    assert article["id"] in _ids(_search(client, "检索权限验证关键词"))


def test_title_update_reindexes(
    owner_client: TestClient, settings
) -> None:
    article = _text(owner_client, body="# 原标题\n\n正文")
    run_until_idle(settings)
    detail = owner_client.get(f"{API}/assets/{article['id']}").json()

    response = owner_client.patch(
        f"{API}/assets/{article['id']}",
        json={"revision": detail["revision"], "title": "改名后的检索标题"},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 200, response.text

    body = _search(owner_client, "改名后的检索标题")
    assert body["mode"] == "fts"
    assert article["id"] in _ids(body)


# —— 站点搜索 ——


def test_site_search_is_off_by_default(owner_client: TestClient, client: TestClient) -> None:
    _site(owner_client, "main")
    token = _token(owner_client, "main")

    response = _site_client(client, token).get(f"{PUBLIC}/search", params={"q": "任何内容"})

    assert response.status_code == 404


def test_site_search_needs_token(owner_client: TestClient, client: TestClient) -> None:
    client.app.state.settings.site_search_enabled = True

    response = client.get(f"{PUBLIC}/search", params={"q": "任何内容"})

    assert response.status_code == 401


def test_site_search_only_returns_published(
    owner_client: TestClient, client: TestClient, settings
) -> None:
    client.app.state.settings.site_search_enabled = True
    _site(owner_client, "main")
    token = _token(owner_client, "main")
    site_client = _site_client(client, token)
    article = _text(owner_client, body="# 文章\n\n尚未发布的关键词")
    run_until_idle(settings)

    assert _ids(_site_search(site_client, "尚未发布的关键词")) == set()

    _publish(owner_client, article["id"])
    assert article["id"] in _ids(_site_search(site_client, "尚未发布的关键词"))


def test_pending_replacement_is_not_searchable(
    owner_client: TestClient, client: TestClient, settings, db: DbSession
) -> None:
    client.app.state.settings.site_search_enabled = True
    _site(owner_client, "main")
    token = _token(owner_client, "main")
    site_client = _site_client(client, token)
    article = _text(owner_client, body="# 文章\n\n旧版关键词甲")
    run_until_idle(settings)
    _publish(owner_client, article["id"])

    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    updated = owner_client.patch(
        f"{API}/assets/{article['id']}",
        json={"revision": detail["revision"], "body": "# 文章\n\n新版关键词乙"},
        headers=admin_headers(owner_client),
    )
    assert updated.status_code == 200, updated.text
    run_until_idle(settings)

    # 站点仍只能搜到已发布那一版的正文；管理端能搜到新草稿。
    assert article["id"] in _ids(_site_search(site_client, "旧版关键词甲"))
    assert _ids(_site_search(site_client, "新版关键词乙")) == set()
    assert article["id"] in _ids(_search(owner_client, "新版关键词乙"))


def test_site_search_requires_granted_collection_for_site_only(
    owner_client: TestClient, client: TestClient, settings
) -> None:
    client.app.state.settings.site_search_enabled = True
    site_a = _site(owner_client, "site-a")
    _site(owner_client, "site-b")
    token_a = _token(owner_client, "site-a")
    token_b = _token(owner_client, "site-b")
    article = _text(owner_client, body="# 内部\n\n内部资料专用关键词")
    collection = _collection(owner_client, "notes.private")
    _add(owner_client, collection["id"], article["id"])
    _grant(owner_client, site_a["id"], "notes")
    run_until_idle(settings)
    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    detail = owner_client.patch(
        f"{API}/assets/{article['id']}",
        json={"revision": detail["revision"], "visibility": "site"},
        headers=admin_headers(owner_client),
    ).json()
    owner_client.post(
        f"{API}/assets/{article['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    result = _site_search(_site_client(client, token_a), "内部资料专用关键词")
    assert article["id"] in _ids(result)
    assert set(result["items"][0]["urls"]) == {"signed"}
    assert _ids(_site_search(_site_client(client, token_b), "内部资料专用关键词")) == set()


def test_unlisted_content_is_not_searchable(
    owner_client: TestClient, client: TestClient, settings
) -> None:
    client.app.state.settings.site_search_enabled = True
    _site(owner_client, "main")
    token = _token(owner_client, "main")
    article = _text(owner_client, body="# 未列出\n\n未列出的搜索关键词")
    run_until_idle(settings)
    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    detail = owner_client.patch(
        f"{API}/assets/{article['id']}",
        json={"revision": detail["revision"], "visibility": "unlisted"},
        headers=admin_headers(owner_client),
    ).json()
    owner_client.post(
        f"{API}/assets/{article['id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    assert _ids(_site_search(_site_client(client, token), "未列出的搜索关键词")) == set()
