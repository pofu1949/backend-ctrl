"""API 与健康接口（企划第 13 章、规划第 1 部分）。"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient

from ctrlstore.config import Settings
from ctrlstore.main import create_app

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _stub_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定成一台空间和内存都充裕的机器，避免测试结果受本机状态影响。"""
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def test_healthz_returns_only_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_healthy(monkeypatch)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert list(response.json()) == ["status"]


def test_healthz_reports_degraded(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: _DiskUsage(1000, 1000, 0))
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded"}


def test_healthz_needs_no_credentials(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_healthy(monkeypatch)

    assert client.get("/healthz", headers={}).status_code == 200


def test_startup_runs_migrations(settings: Settings) -> None:
    assert not settings.db_path.exists()

    with TestClient(create_app(settings)):
        pass

    assert settings.db_path.is_file()


def test_root_reports_version(client: TestClient) -> None:
    body = client.get("/").json()

    assert body["service"] == "ctrlstore"
    assert body["version"]
