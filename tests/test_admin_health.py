"""所有者健康接口与备份清单（规划第 9 部分）。

匿名接口仍然只有 ``status``；所有者接口给磁盘、队列、迁移版本与上次成功备份。
备份目录里只有 ``status = complete`` 的清单才算可恢复。
"""

from __future__ import annotations

import json
import shutil
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient
from tests.auth_helpers import EDITOR_PASSWORD, add_editor, login

from ctrlstore import backup

API = "/api/admin/v1"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def _manifest(directory: Path, *, status: str, completed_at: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / backup.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "status": status,
                "app_version": "0.1.0",
                "schema_version": "0009",
                "started_at": completed_at,
                "completed_at": completed_at,
            }
        ),
        encoding="utf-8",
    )


def test_anonymous_health_keeps_only_status(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_owner_health_reports_details(owner_client: TestClient) -> None:
    response = owner_client.get(f"{API}/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_version"]
    assert body["schema_version"] == "0009"
    assert set(body["jobs"]) == {"pending", "running", "done", "failed"}
    assert body["disk"]["free_bytes"] > 0
    assert body["last_backup"] is None


def test_last_backup_uses_newest_complete(
    owner_client: TestClient, tmp_path: Path
) -> None:
    _manifest(tmp_path / "20261020-1", status="failed", completed_at="2026-10-20T01:00:00Z")
    _manifest(tmp_path / "20261021-1", status="complete", completed_at="2026-10-21T01:00:00Z")
    owner_client.app.state.settings.backup_dir = tmp_path

    body = owner_client.get(f"{API}/health").json()

    assert body["last_backup"]["backup"] == "20261021-1"
    assert body["last_backup"]["completed_at"] == "2026-10-21T01:00:00Z"


def test_health_is_owner_only(
    owner_client: TestClient, client: TestClient, db
) -> None:
    add_editor(db, username="editor", permissions=("asset.read",))
    login(client, "editor", EDITOR_PASSWORD)

    response = client.get(f"{API}/health")

    assert response.status_code == 403


def test_latest_complete_ignores_bad_manifests(tmp_path: Path) -> None:
    _manifest(tmp_path / "20261021-1", status="running", completed_at="2026-10-21T01:00:00Z")
    (tmp_path / "20261021-2").mkdir()
    (tmp_path / "20261021-2" / backup.MANIFEST_NAME).write_text("不是 JSON", encoding="utf-8")
    _manifest(tmp_path / "20261021-3", status="complete", completed_at="2026-10-21T03:00:00Z")

    latest = backup.latest_complete(tmp_path)

    assert latest is not None
    assert latest["backup"] == "20261021-3"


def test_latest_complete_without_directory() -> None:
    assert backup.latest_complete(None) is None
    assert backup.latest_complete(Path("/does/not/exist")) is None


def test_manifest_timestamps_are_strings(tmp_path: Path) -> None:
    moment = datetime.now(UTC).isoformat(timespec="seconds")
    _manifest(tmp_path / "one", status="complete", completed_at=moment)

    latest = backup.latest_complete(tmp_path)

    assert latest["completed_at"] == moment
