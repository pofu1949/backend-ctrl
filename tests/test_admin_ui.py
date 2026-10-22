"""管理端界面与其专用只读接口。"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import (
    API,
    EDITOR_PASSWORD,
    OWNER_PASSWORD,
    add_editor,
    admin_headers,
    login,
)
from tests.inspection_helpers import minimal_png

from ctrlstore.config import Settings
from ctrlstore.content import markdown_render

UI = "/admin/"


def test_admin_ui_shell_is_served_with_security_headers(client: TestClient) -> None:
    response = client.get(UI)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/admin/assets/admin.js" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"


def test_admin_ui_redirect_and_assets(client: TestClient) -> None:
    redirect = client.get("/admin", follow_redirects=False)
    assert redirect.status_code == 308
    assert redirect.headers["location"] == UI

    assert client.get("/admin/assets/admin.css").status_code == 200
    assert client.get("/admin/assets/admin.js").status_code == 200


def test_body_endpoint_reads_markdown_and_rejects_non_text(
    owner_client: TestClient,
) -> None:
    created = owner_client.post(
        f"{API}/assets",
        headers=admin_headers(owner_client),
        json={"kind": "markdown", "title": "编辑测试", "body": "# 你好\n\n正文。"},
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]

    response = owner_client.get(f"{API}/assets/{asset_id}/body")
    assert response.status_code == 200
    assert response.json()["body"] == "# 你好\n\n正文。"
    assert response.json()["revision"] == created.json()["revision"]
    assert "storage_key" not in response.json()

    image = owner_client.post(
        f"{API}/uploads/direct",
        headers=admin_headers(owner_client),
        files={"file": ("pic.png", minimal_png(), "image/png")},
    )
    assert image.status_code == 201, image.text
    rejected = owner_client.get(f"{API}/assets/{image.json()['asset_id']}/body")
    assert rejected.status_code == 422


def test_preview_requires_auth_and_serves_image_fallback(owner_client: TestClient) -> None:
    created = owner_client.post(
        f"{API}/uploads/direct",
        headers=admin_headers(owner_client),
        files={"file": ("pic.png", minimal_png(width=12, height=9), "image/png")},
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["asset_id"]

    owner_client.cookies.clear()
    assert owner_client.get(f"{API}/assets/{asset_id}/preview").status_code == 401
    login(owner_client, "owner", OWNER_PASSWORD)

    preview = owner_client.get(f"{API}/assets/{asset_id}/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/png")
    assert preview.content == minimal_png(width=12, height=9)

    # 缩略图接口只读派生文件；图片还没有跑 worker，不能拿原图冒充 thumb。
    assert owner_client.get(f"{API}/assets/{asset_id}/thumb").status_code == 404


def test_system_read_endpoints_are_owner_only(owner_client: TestClient, db: DbSession) -> None:
    jobs = owner_client.get(f"{API}/jobs")
    assert jobs.status_code == 200
    assert isinstance(jobs.json(), list)

    audit = owner_client.get(f"{API}/audit")
    assert audit.status_code == 200
    assert isinstance(audit.json(), list)
    assert any(row["action"] == "login" for row in audit.json())

    add_editor(db, username="reader", permissions=["asset.read"])
    login(owner_client, "reader", EDITOR_PASSWORD)
    assert owner_client.get(f"{API}/jobs").status_code == 403
    assert owner_client.get(f"{API}/audit").status_code == 403


def test_asset_list_contains_trashed_at(owner_client: TestClient) -> None:
    created = owner_client.post(
        f"{API}/assets",
        headers=admin_headers(owner_client),
        json={"kind": "text", "title": "待删除", "body": "x"},
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    response = owner_client.post(
        f"{API}/assets/{asset_id}/trash",
        headers=admin_headers(owner_client),
        json={"revision": created.json()["revision"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["trashed_at"] is not None
    trashed = owner_client.get(f"{API}/assets", params={"status": "trashed"})
    assert trashed.status_code == 200
    assert trashed.json()["items"][0]["trashed_at"] is not None


def test_editor_preview_renders_unsaved_body_with_public_sanitizer(
    owner_client: TestClient, settings: Settings
) -> None:
    settings.x_accel_redirect = False
    headers = admin_headers(owner_client)
    article = owner_client.post(
        f"{API}/assets",
        headers=headers,
        json={"kind": "markdown", "title": "预览", "body": "尚未修改"},
    ).json()
    images = {}
    for visibility in ("public", "unlisted", "site", "private", "draft"):
        uploaded = owner_client.post(
            f"{API}/uploads/direct",
            headers=headers,
            files={"file": (f"{visibility}.png", minimal_png(), "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text
        image_id = uploaded.json()["asset_id"]
        images[visibility] = image_id
        if visibility != "draft":
            updated = owner_client.patch(
                f"{API}/assets/{image_id}",
                headers=headers,
                json={"revision": 1, "visibility": visibility},
            ).json()
            published = owner_client.post(
                f"{API}/assets/{image_id}/publish",
                headers=headers,
                json={"revision": updated["revision"]},
            )
            assert published.status_code == 200, published.text
    nonexistent = str(uuid4())
    source = "\n\n".join(
        [
            "---\ntitle: 隐藏元数据\n---\n# 未保存的新正文",
            '<script>alert("bad")</script>',
            "![外站](https://external.invalid/tracker.png)",
            '<img src="https://external.invalid/raw.png" onerror="alert(1)">',
            *[f"![{key}](asset:{value})" for key, value in images.items()],
            f"![不存在](asset:{nonexistent})",
            f'<img src="/p/{images["public"]}/original.png" onload="bad()">',
            f"![绝对旧地址](https://old.invalid/p/{images['public']}/original.png)",
            "[脚本链接](javascript:alert)",
            "| 表头 |\n| --- |\n| 单元格 |",
        ]
    )
    rendered = owner_client.post(
        f"{API}/assets/{article['id']}/render", headers=headers, json={"body": source}
    )
    assert rendered.status_code == 200, rendered.text
    assert rendered.headers["cache-control"] == "no-store"
    html = rendered.json()["html"]
    assert "未保存的新正文" in html and "尚未修改" not in html
    assert "隐藏元数据" not in html
    assert "<script" not in html and "alert" not in html
    assert "external.invalid" not in html and "old.invalid" not in html
    assert "onload" not in html and "onerror" not in html and "javascript:" not in html
    assert "<table>" in html
    assert "图片缺失（未发布或不可用）" in html
    assert set(rendered.json()["missing_asset_ids"]) == {
        nonexistent,
        images["draft"],
        images["private"],
    }
    for visibility in ("public", "unlisted"):
        assert f"{settings.public_base_url}/p/{images[visibility]}/original.png" in html
    assert f"/p/{images['site']}/" not in html
    signed = re.search(r'src="([^"]*/files/[^"]+)"', html)
    assert signed is not None
    signed_url = signed.group(1).replace("&amp;", "&")
    assert "&expires=" in signed_url and "&sig=" in signed_url
    assert owner_client.get(signed_url).content == minimal_png()
    assert owner_client.get(f"{API}/assets/{article['id']}/body").json()["body"] == "尚未修改"
    # 同样的可用图片经公开管线渲染，得到同一份 HTML；管理预览只额外标注缺失。
    available_source = f"**同一规则** ![图](asset:{images['public']})"
    preview = owner_client.post(
        f"{API}/assets/{article['id']}/render",
        headers=headers,
        json={"body": available_source},
    ).json()["html"]
    assert (
        preview
        == markdown_render.render(
            available_source,
            image_url=lambda _: f"{settings.public_base_url}/p/{images['public']}/original.png",
            base_url=settings.public_base_url,
        ).html
    )


def test_editor_preview_requires_read_permission_and_csrf(
    owner_client: TestClient, db: DbSession
) -> None:
    article = owner_client.post(
        f"{API}/assets",
        headers=admin_headers(owner_client),
        json={"kind": "text", "body": "旧正文"},
    ).json()
    url = f"{API}/assets/{article['id']}/render"
    assert owner_client.post(url, json={"body": "x"}).status_code == 403
    add_editor(db, username="reader", permissions=["asset.read"])
    login(owner_client, "reader", EDITOR_PASSWORD)
    response = owner_client.post(
        url, headers=admin_headers(owner_client), json={"body": "# 文本 <script>x</script>"}
    )
    assert response.status_code == 200
    assert response.json()["html"] == "<pre># 文本 &lt;script&gt;x&lt;/script&gt;</pre>"
    add_editor(db, username="uploader", permissions=["asset.upload"])
    login(owner_client, "uploader", EDITOR_PASSWORD)
    assert (
        owner_client.post(url, headers=admin_headers(owner_client), json={"body": "x"}).status_code
        == 403
    )
    owner_client.cookies.clear()
    assert owner_client.post(url, json={"body": "x"}).status_code == 401


def test_admin_csp_allows_configured_public_image_origin(client: TestClient) -> None:
    client.app.state.settings.public_base_url = "https://files.example.test"
    csp = client.get(UI).headers["content-security-policy"]
    assert "img-src 'self' data: blob: https://files.example.test;" in csp
