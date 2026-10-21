"""验收清单与建议项的回归测试（`plan/问题总结.md`）。

覆盖：命令行发布、条数配额、令牌来源 IP、标签合并、展示图、备份脚本关键顺序、
Nginx 主配置与 Docker Compose 样例、README 进度。
"""

from __future__ import annotations

import shutil
from collections import namedtuple
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import admin_headers
from tests.conftest import TEST_SECRETS
from tests.inspection_helpers import minimal_png
from tests.test_public_api import _site
from tests.test_publish import _text

from ctrlstore.cli import main
from ctrlstore.models import Asset
from ctrlstore.worker import run_until_idle

API = "/api/admin/v1"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def _config(tmp_path: Path, settings) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(f"data_dir = '{settings.data_dir.as_posix()}'\n", encoding="utf-8")
    return config


def _cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTRLSTORE_SESSION_SECRET", TEST_SECRETS["session_secret"])
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])


# —— 命令行发布/撤回 ——


def test_cli_can_publish_and_unpublish(
    owner_client: TestClient,
    db: DbSession,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_env(monkeypatch)
    article = _text(owner_client, body="# CLI\n\n正文")
    config = _config(tmp_path, settings)

    assert main(["--config", str(config), "publish", article["id"]]) == 0
    db.expire_all()
    assert db.get(Asset, article["id"]).status == "published"

    capsys.readouterr()
    assert main(["--config", str(config), "unpublish", article["id"]]) == 0
    db.expire_all()
    assert db.get(Asset, article["id"]).status == "draft"


# —— 条数配额 ——


def test_item_quota_counts_trashed(
    owner_client: TestClient, client: TestClient
) -> None:
    client.app.state.settings.user_quota_items = 1
    first = _text(owner_client, body="# 一\n\n正文")
    second = owner_client.post(
        f"{API}/assets",
        json={"kind": "text", "body": "第二条"},
        headers=admin_headers(owner_client),
    )
    assert second.status_code == 507

    owner_client.post(
        f"{API}/assets/{first['id']}/trash",
        json={"revision": first["revision"]},
        headers=admin_headers(owner_client),
    )
    # 回收站仍占条数；彻底删除后才释放。
    still = owner_client.post(
        f"{API}/assets",
        json={"kind": "text", "body": "第三条"},
        headers=admin_headers(owner_client),
    )
    assert still.status_code == 507


# —— 令牌来源 IP ——


def test_token_allowed_ip(owner_client: TestClient, client: TestClient) -> None:
    _site(owner_client, "main")
    issued = owner_client.post(
        f"{API}/tokens",
        json={
            "subject_type": "site",
            "subject_key": "main",
            "allowed_ip": "10.0.0.5",
        },
        headers=admin_headers(owner_client),
    ).json()
    token = issued["token"]
    assert issued["allowed_ip"] == "10.0.0.5"
    # 丢掉 owner 会话 Cookie，否则请求先按会话认证，走不到令牌的 IP 检查。
    client.cookies.clear()

    allowed = client.get(
        f"{API}/me",
        headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "10.0.0.5"},
    )
    assert allowed.status_code == 200

    blocked = client.get(
        f"{API}/me",
        headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "10.0.0.6"},
    )
    assert blocked.status_code == 401


# —— 标签合并 ——


def test_owner_can_merge_tags(
    owner_client: TestClient, settings
) -> None:
    source = owner_client.post(
        f"{API}/tags", json={"name": "旧标签"}, headers=admin_headers(owner_client)
    ).json()
    target = owner_client.post(
        f"{API}/tags", json={"name": "新标签"}, headers=admin_headers(owner_client)
    ).json()
    article = _text(owner_client, body="# 标签\n\n正文", tags=["旧标签"])
    run_until_idle(settings)

    response = owner_client.post(
        f"{API}/tags/{source['id']}/merge",
        json={"into": target["id"]},
        headers=admin_headers(owner_client),
    )
    assert response.status_code == 200, response.text

    detail = owner_client.get(f"{API}/assets/{article['id']}").json()
    assert [tag["name"] for tag in detail["tags"]] == ["新标签"]
    tags = owner_client.get(f"{API}/tags").json()
    assert "旧标签" not in {tag["name"] for tag in tags}

    # 索引里的关键词也同步更新：旧名不再命中，新名可以。
    assert owner_client.get(f"{API}/search", params={"q": "旧标签"}).json()["items"] == []
    assert article["id"] in {
        item["id"]
        for item in owner_client.get(f"{API}/search", params={"q": "新标签"}).json()["items"]
    }


def test_tag_merge_rejects_self(owner_client: TestClient) -> None:
    tag = owner_client.post(
        f"{API}/tags", json={"name": "自己"}, headers=admin_headers(owner_client)
    ).json()

    response = owner_client.post(
        f"{API}/tags/{tag['id']}/merge",
        json={"into": tag["id"]},
        headers=admin_headers(owner_client),
    )

    assert response.status_code == 422


# —— 展示图 ——


def test_display_derivative_is_enqueued(
    owner_client: TestClient, client: TestClient, settings
) -> None:
    created = owner_client.post(
        f"{API}/uploads/direct",
        files={"file": ("wide.png", minimal_png(2000, 1000), "image/png")},
        headers=admin_headers(owner_client),
    ).json()
    run_until_idle(settings)
    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    purposes = {
        item["purpose"] for item in detail["current_version"]["derivatives"]
    }
    assert {"thumb", "display"} <= purposes


# —— 运维与部署样例 ——


def test_backup_script_restarts_even_if_stop_fails() -> None:
    script = Path("scripts/backup.sh").read_text(encoding="utf-8")
    stop = script.index("systemctl stop")
    flag = script.index("RESTARTED=1")
    assert flag < stop, "必须先置位 RESTARTED 再停服务，stop 失败也要尝试拉起"
    assert "install -m 0600" in script and "secrets.env" in script


def test_restore_script_restores_secrets() -> None:
    script = Path("scripts/restore.sh").read_text(encoding="utf-8")
    stop = script.index("systemctl stop")
    flag = script.index("RESTARTED=1")
    assert flag < stop
    assert "secrets.env" in script and "install -m 0600" in script


def test_deployment_samples_exist() -> None:
    assert Path("deploy/nginx/nginx.conf.example").is_file()
    nginx = Path("deploy/nginx/nginx.conf.example").read_text(encoding="utf-8")
    assert "worker_processes 2;" in nginx

    assert Path("deploy/Dockerfile").is_file()
    compose = Path("deploy/docker-compose.example.yml").read_text(encoding="utf-8")
    assert "worker:" in compose and "api:" in compose


def test_readme_progress_is_current() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "第 9 部分" in readme
    assert "第 6 部分（集合与网站只读契约）已完成" not in readme
