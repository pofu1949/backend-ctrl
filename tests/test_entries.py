"""文案字典契约：快照隔离、权限、站点、分页缓存、幂等、并发与 CLI。"""

from __future__ import annotations

import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.auth_helpers import (
    API,
    EDITOR_PASSWORD,
    OWNER_PASSWORD,
    add_editor,
    admin_headers,
    login,
)
from tests.conftest import TEST_SECRETS

from ctrlstore.auth import permissions
from ctrlstore.cli import main
from ctrlstore.config import Settings
from ctrlstore.db import create_engine
from ctrlstore.entries import service
from ctrlstore.errors import ApiError
from ctrlstore.models import Asset, AuditLog, Entry, IdempotencyKey, User, utcnow

PUBLIC = "/api/public/v1/entries"


def create(client, key="nav.about", **fields):
    response = client.post(
        f"{API}/entries",
        headers=admin_headers(client),
        json={"key": key, "value": "关于我们", **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


def action(client, entry, name="publish", *, key=None):
    headers = admin_headers(client)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        f"{API}/entries/{entry['id']}/{name}",
        headers=headers,
        json={"revision": entry["revision"]},
    )


def publish(client, entry):
    response = action(client, entry)
    assert response.status_code == 200, response.text
    return response.json()


def update(client, entry, **fields):
    response = client.patch(
        f"{API}/entries/{entry['id']}",
        headers=admin_headers(client),
        json={"revision": entry["revision"], **fields},
    )
    assert response.status_code == 200, response.text
    return response.json()


def site(client, key):
    response = client.post(f"{API}/sites", headers=admin_headers(client), json={"key": key})
    assert response.status_code == 201, response.text
    created = response.json()
    response = client.post(
        f"{API}/tokens",
        headers=admin_headers(client),
        json={
            "subject_type": "site",
            "subject_key": key,
            "name": "entries test",
        },
    )
    assert response.status_code == 201, response.text
    return created, {"Authorization": f"Bearer {response.json()['token']}"}


def test_snapshot_isolation_and_republish(owner_client):
    entry = create(owner_client)
    assert owner_client.get(f"{PUBLIC}/nav.about").status_code == 404
    entry = publish(owner_client, entry)
    before = owner_client.get(f"{PUBLIC}/nav.about")
    assert before.json()["value"] == "关于我们"
    assert set(before.json()) == {"key", "language", "value", "updated_at"}
    entry = update(owner_client, entry, value="关于", key="nav.team", language="en")
    after = owner_client.get(f"{PUBLIC}/nav.about")
    assert after.json() == before.json()
    assert after.headers["etag"] == before.headers["etag"]
    assert owner_client.get(f"{PUBLIC}/nav.team?language=en").status_code == 404
    assert owner_client.get(f"{PUBLIC}/nav.about?language=en").status_code == 404
    entry = publish(owner_client, entry)
    assert owner_client.get(f"{PUBLIC}/nav.about").status_code == 404
    assert owner_client.get(f"{PUBLIC}/nav.team?language=en").json()["value"] == "关于"
    assert publish(owner_client, entry)["revision"] == entry["revision"]


def test_permissions_csrf_and_site_tokens_cannot_write(owner_client, db):
    entry = publish(owner_client, create(owner_client))
    add_editor(db, username="writer", permissions=["entry.update"])
    add_editor(db, username="reviewer", permissions=["entry.publish"])
    add_editor(db, username="ordinary", permissions=permissions.from_preset("editor"))
    login(owner_client, "writer", EDITOR_PASSWORD)
    entry = update(owner_client, entry, value="待审核")
    assert action(owner_client, entry).status_code == 403
    assert owner_client.get(f"{PUBLIC}/nav.about").json()["value"] == "关于我们"
    assert owner_client.post(f"{API}/entries", json={"key": "new"}).status_code == 403
    assert (
        owner_client.delete(
            f"{API}/entries/{entry['id']}", headers=admin_headers(owner_client)
        ).status_code
        == 403
    )
    assert owner_client.get(f"{API}/entry-sites").status_code == 200
    assert owner_client.get(f"{API}/sites").status_code == 403
    login(owner_client, "reviewer", EDITOR_PASSWORD)
    assert owner_client.get(f"{API}/entries/{entry['id']}").json()["value"] == "待审核"
    assert (
        owner_client.patch(
            f"{API}/entries/{entry['id']}",
            headers=admin_headers(owner_client),
            json={"revision": entry["revision"], "value": "越权"},
        ).status_code
        == 403
    )
    entry = publish(owner_client, entry)
    assert owner_client.get(f"{PUBLIC}/nav.about").json()["value"] == "待审核"
    login(owner_client, "ordinary", EDITOR_PASSWORD)
    assert owner_client.get(f"{API}/entries").status_code == 403
    assert owner_client.get(f"{API}/entries/{entry['id']}").status_code == 403
    login(owner_client, "owner", OWNER_PASSWORD)
    _, headers = site(owner_client, "site-writer")
    with TestClient(owner_client.app) as token_client:
        assert token_client.get(f"{API}/entries", headers=headers).status_code == 403
        assert (
            token_client.post(f"{API}/entries", headers=headers, json={"key": "bad"}).status_code
            == 403
        )
    owner_client.cookies.clear()
    assert owner_client.get(f"{API}/entries").status_code == 401


def test_unpublish_clears_snapshot_and_owner_delete(owner_client):
    entry = publish(owner_client, create(owner_client))
    assert (
        owner_client.delete(
            f"{API}/entries/{entry['id']}", headers=admin_headers(owner_client)
        ).status_code
        == 409
    )
    response = action(owner_client, entry, "unpublish")
    assert response.status_code == 200
    entry = response.json()
    assert entry["revision"] == 3
    for field in ("key", "language", "value", "visibility", "site_id"):
        assert entry[f"published_{field}"] is None
    assert entry["published_at"] is None
    assert entry["value"] == "关于我们"
    assert owner_client.get(f"{PUBLIC}/nav.about").status_code == 404
    assert owner_client.get(PUBLIC).json()["items"] == []
    assert (
        owner_client.delete(
            f"{API}/entries/{entry['id']}?revision=2", headers=admin_headers(owner_client)
        ).status_code
        == 409
    )
    assert (
        owner_client.delete(
            f"{API}/entries/{entry['id']}?revision=3", headers=admin_headers(owner_client)
        ).status_code
        == 204
    )
    assert owner_client.get(f"{API}/entries/{entry['id']}").status_code == 404


@pytest.mark.parametrize("visibility", ["public", "site"])
def test_site_scope_and_library_visibility(owner_client, visibility):
    a, auth_a = site(owner_client, "site-a")
    _, auth_b = site(owner_client, "site-b")
    library = publish(owner_client, create(owner_client, "library.value", visibility=visibility))
    owned = publish(
        owner_client, create(owner_client, "owned.value", visibility=visibility, site_id=a["id"])
    )
    assert owner_client.get(f"{PUBLIC}/{library['key']}").status_code == (
        200 if visibility == "public" else 404
    )
    for auth in (auth_a, auth_b):
        assert owner_client.get(f"{PUBLIC}/{library['key']}", headers=auth).status_code == 200
    assert owner_client.get(f"{PUBLIC}/{owned['key']}").status_code == 404
    assert owner_client.get(f"{PUBLIC}/{owned['key']}", headers=auth_a).status_code == 200
    assert owner_client.get(f"{PUBLIC}/{owned['key']}", headers=auth_b).status_code == 404
    # 归属和可见性修改只进草稿，不能移交线上读取范围。
    update(owner_client, owned, site_id=None, visibility="public")
    assert owner_client.get(f"{PUBLIC}/{owned['key']}").status_code == 404
    assert owner_client.get(f"{PUBLIC}/{owned['key']}", headers=auth_a).status_code == 200
    assert owner_client.get(f"{PUBLIC}/{owned['key']}", headers=auth_b).status_code == 404


def test_site_override_is_consistent_for_single_and_paginated_list(owner_client):
    a, auth_a = site(owner_client, "site-a")
    _, auth_b = site(owner_client, "site-b")
    publish(owner_client, create(owner_client, "home.title", value="库级"))
    owned = publish(owner_client, create(owner_client, "home.title", value="本站", site_id=a["id"]))
    assert owner_client.get(f"{PUBLIC}/home.title").json()["value"] == "库级"
    assert owner_client.get(f"{PUBLIC}/home.title", headers=auth_a).json()["value"] == "本站"
    assert owner_client.get(f"{PUBLIC}/home.title", headers=auth_b).json()["value"] == "库级"
    assert owner_client.get(PUBLIC, headers=auth_a).json()["items"][0]["value"] == "本站"
    assert len(owner_client.get(PUBLIC, headers=auth_a).json()["items"]) == 1
    action(owner_client, owned, "unpublish")
    assert owner_client.get(f"{PUBLIC}/home.title", headers=auth_a).json()["value"] == "库级"


def test_unique_published_scope_and_language(owner_client):
    publish(owner_client, create(owner_client))
    duplicate = create(owner_client)
    assert action(owner_client, duplicate).status_code == 409
    assert owner_client.get(f"{API}/entries/{duplicate['id']}").json()["status"] == "draft"
    publish(owner_client, create(owner_client, language="en"))
    a, _ = site(owner_client, "site-a")
    publish(owner_client, create(owner_client, site_id=a["id"]))
    assert action(owner_client, create(owner_client, site_id=a["id"])).status_code == 409


def test_database_partial_indexes_reject_duplicate_publication(db, owner, settings):
    first = service.create(
        db,
        settings=settings,
        fields={"key": "nav.about", "value": "one"},
        actor=owner,
    )
    first = service.publish(db, first.id, revision=first.revision, actor=owner)
    db.commit()
    duplicate = Entry(
        key="draft",
        value="two",
        status="published",
        published_key="nav.about",
        published_language="zh-CN",
        published_value="two",
        published_visibility="public",
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


@pytest.mark.parametrize(
    "fields",
    [
        {"key": "Home.title"},
        {"key": "home/title"},
        {"key": "home..title"},
        {"key": "home.-title"},
        {"key": " home.title"},
        {"key": ""},
        {"key": "a" * 129},
        {"value": "a" * 2001},
        {"value": "x\x00y"},
        {"value": "x\ry"},
        {"value": "x\x7fy"},
        {"value": "x\x85y"},
        {"value": "\ud800"},
        {"language": ""},
        {"visibility": "private"},
        {"site_id": "unknown"},
        {"published_value": "越权快照"},
    ],
)
def test_invalid_entry_never_writes(owner_client, db, fields):
    # surrogate 通过转义后的 JSON 发送，验证服务不会在 UTF-8 编码时抛 500。
    payload = json.dumps({"key": "test.valid", "value": "text", **fields})
    response = owner_client.post(
        f"{API}/entries",
        headers={**admin_headers(owner_client), "Content-Type": "application/json"},
        content=payload,
    )
    assert response.status_code in (413, 422), response.text
    assert db.scalar(select(func.count()).select_from(Entry)) == 0


def test_plain_text_and_unicode_boundaries(owner_client):
    value = '<script>alert("x")</script>\n下一行\t制表符😀'
    publish(owner_client, create(owner_client, value=value))
    payload = owner_client.get(f"{PUBLIC}/nav.about").json()
    assert payload["value"] == value
    assert "html" not in payload and "body" not in payload
    assert "/p/" not in json.dumps(payload)
    assert create(owner_client, "emoji", value="😀" * 2000)["value"] == "😀" * 2000
    assert create(owner_client, "empty", value="")["value"] == ""


def test_prefix_pagination_cache_and_draft_independence(owner_client):
    entries = [
        publish(owner_client, create(owner_client, key))
        for key in (
            "home.a",
            "home.b",
            "homepage",
            "home-title",
            "home.c",
        )
    ]
    hidden = create(owner_client, "home.hidden")
    before = owner_client.get(f"{PUBLIC}?prefix=home&limit=2")
    assert [item["key"] for item in before.json()["items"]] == ["home.a", "home.b"]
    assert before.headers["cache-control"] == "public, max-age=60"
    assert before.headers["etag"].startswith('W/"')
    assert (
        owner_client.get(
            f"{PUBLIC}?prefix=home&limit=2", headers={"If-None-Match": before.headers["etag"]}
        ).status_code
        == 304
    )
    cursor = before.json()["next_cursor"]
    next_page = owner_client.get(PUBLIC, params={"prefix": "home", "limit": 2, "cursor": cursor})
    assert [item["key"] for item in next_page.json()["items"]] == ["home.c"]
    assert next_page.json()["next_cursor"] is None
    update(owner_client, entries[0], value="仅草稿")
    update(owner_client, hidden, value="隐藏草稿")
    publish(owner_client, create(owner_client, "unrelated", value="其他发布"))
    after = owner_client.get(f"{PUBLIC}?prefix=home&limit=2")
    assert after.json() == before.json()
    assert after.headers["etag"] == before.headers["etag"]
    assert before.json()["updated_at"] == max(item["updated_at"] for item in before.json()["items"])
    assert owner_client.get(f"{PUBLIC}?cursor=invalid!").status_code == 422
    assert owner_client.get(f"{API}/entries?cursor=invalid!").status_code == 422
    assert owner_client.get(f"{PUBLIC}?limit=201").status_code == 422
    _, auth = site(owner_client, "cache-site")
    private = owner_client.get(PUBLIC, headers=auth)
    assert private.headers["cache-control"] == "private, no-store"
    assert (
        owner_client.get(
            PUBLIC, headers={**auth, "If-None-Match": private.headers["etag"]}
        ).status_code
        == 200
    )
    assert owner_client.get(PUBLIC, headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_idempotency_revision_and_expired_key_reuse(owner_client, db):
    entry = create(owner_client)
    first = action(owner_client, entry, key="publish-once")
    second = action(owner_client, entry, key="publish-once")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["revision"] == 2
    assert action(owner_client, first.json(), key="publish-once").status_code == 409
    assert action(owner_client, entry).status_code == 409
    entry = update(owner_client, first.json(), value="第二份")
    record = db.scalar(select(IdempotencyKey).where(IdempotencyKey.key == "publish-once"))
    record.created_at = utcnow() - timedelta(hours=25)
    db.commit()
    third = action(owner_client, entry, key="publish-once")
    assert third.status_code == 200
    assert third.json()["published_value"] == "第二份"
    assert action(owner_client, entry, key="publish-once").json() == third.json()
    withdrawn = action(owner_client, third.json(), "unpublish", key="withdraw-once")
    assert withdrawn.status_code == 200
    assert (
        action(owner_client, third.json(), "unpublish", key="withdraw-once").json()
        == withdrawn.json()
    )


def test_limit_audit_and_asset_inventory_are_independent(owner_client, settings, db):
    settings.entry_limit = 1
    original_assets = db.scalar(select(func.count()).select_from(Asset))
    entry = create(owner_client)
    response = owner_client.post(
        f"{API}/entries", headers=admin_headers(owner_client), json={"key": "second"}
    )
    assert response.status_code == 409 and "上限" in response.json()["error"]["message"]
    entry = update(owner_client, entry, value="修改")
    entry = publish(owner_client, entry)
    entry = action(owner_client, entry, "unpublish").json()
    owner_client.delete(f"{API}/entries/{entry['id']}", headers=admin_headers(owner_client))
    actions = set(db.scalars(select(AuditLog.action).where(AuditLog.target_type == "entry")))
    assert actions == {
        "entry_created",
        "entry_updated",
        "entry_published",
        "entry_unpublished",
        "entry_deleted",
    }
    assert db.scalar(select(func.count()).select_from(Asset)) == original_assets
    assert db.scalar(text("SELECT COUNT(*) FROM asset_search")) == 0
    assert create(owner_client, "replacement")["status"] == "draft"


def test_transaction_rollback_and_concurrent_limit(settings, database, owner):
    owner_id = owner.id
    barrier = Barrier(2)
    settings.entry_limit = 1

    def attempt(key):
        engine = create_engine(settings)
        try:
            with Session(engine) as db:
                actor = db.get(User, owner_id)
                barrier.wait(timeout=5)
                try:
                    row = service.create(db, settings=settings, fields={"key": key}, actor=actor)
                    db.commit()
                    return row.key
                except ApiError as error:
                    db.rollback()
                    return error.status_code
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ["first", "second"]))
    assert results.count(409) == 1
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Entry)) == 1
        row = db.scalar(select(Entry))
        revision = row.revision
        actor = db.get(User, owner_id)
        service.update(db, row.id, revision=revision, fields={"value": "rolled back"}, actor=actor)
        db.rollback()
        assert db.scalar(select(Entry)).value == ""


def test_presets_and_entry_cli(tmp_path, monkeypatch, capsys):
    assert permissions.from_preset("copywriter") == {"entry.update", "entry.publish"}
    assert len(permissions.from_preset("editor")) == 8
    for key, value in TEST_SECRETS.items():
        monkeypatch.setenv(f"CTRLSTORE_{key.upper()}", value)
    config = tmp_path / "config.toml"
    config.write_text(f"data_dir = '{(tmp_path / 'data').as_posix()}'\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO("owner-password-0001\n"))
    prefix = ["--config", str(config)]
    assert main([*prefix, "init", "--password-stdin"]) == 0
    assert main([*prefix, "entry", "set", "nav.about", "--value", "关于我们"]) == 0
    assert main([*prefix, "entry", "publish", "nav.about"]) == 0
    assert main([*prefix, "entry", "set", "nav.about", "--value", "新草稿"]) == 0
    settings = Settings(data_dir=tmp_path / "data", **TEST_SECRETS)
    engine = create_engine(settings)
    with Session(engine) as db:
        entry = db.scalar(select(Entry))
        assert entry.value == "新草稿" and entry.published_value == "关于我们"
    engine.dispose()
    assert main([*prefix, "entry", "unpublish", "nav.about"]) == 0
    assert main([*prefix, "entry", "publish", "missing"]) == 1
    assert main([*prefix, "entry", "set", "BAD/key", "--value", "invalid"]) == 1
    assert main([*prefix, "entry", "set", "scoped", "--site", "missing", "--value", "x"]) == 1
    assert "已保存文案草稿" in capsys.readouterr().out


def test_admin_pagination_and_invalid_patch_do_not_change_revision(owner_client):
    first = create(owner_client, "home.a")
    create(owner_client, "home.b")
    create(owner_client, "homepage")
    create(owner_client, "home.english", language="en")
    page = owner_client.get(
        f"{API}/entries", params={"prefix": "home", "language": "zh-CN", "limit": 1}
    )
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.json()["items"][0]["key"] == "home.a"
    second = owner_client.get(
        f"{API}/entries",
        params={
            "prefix": "home",
            "language": "zh-CN",
            "limit": 1,
            "cursor": page.json()["next_cursor"],
        },
    )
    assert second.json()["items"][0]["key"] == "home.b"
    assert second.json()["next_cursor"] is None
    assert owner_client.get(f"{API}/entries?status=published").json()["items"] == []
    for fields in ({"key": None}, {"language": None}, {"value": None}, {"visibility": None}):
        response = owner_client.patch(
            f"{API}/entries/{first['id']}",
            headers=admin_headers(owner_client),
            json={"revision": first["revision"], **fields},
        )
        assert response.status_code == 422, response.text
    assert owner_client.get(f"{API}/entries/{first['id']}").json()["revision"] == 1


def test_public_full_page_etag_ignores_new_records_after_cursor(owner_client):
    publish(owner_client, create(owner_client, "home.a"))
    before = owner_client.get(PUBLIC, params={"prefix": "home", "limit": 1})
    assert before.json()["next_cursor"] is not None
    publish(owner_client, create(owner_client, "home.z"))
    after = owner_client.get(PUBLIC, params={"prefix": "home", "limit": 1})
    assert after.json() == before.json()
    assert after.headers["etag"] == before.headers["etag"]
    tail = owner_client.get(
        PUBLIC,
        params={
            "prefix": "home",
            "limit": 1,
            "cursor": before.json()["next_cursor"],
        },
    ).json()
    assert tail["items"][0]["key"] == "home.z"
    empty = owner_client.get(
        PUBLIC,
        params={
            "prefix": "home",
            "limit": 1,
            "cursor": tail["next_cursor"],
        },
    ).json()
    assert empty["items"] == [] and empty["next_cursor"] is None
