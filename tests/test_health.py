"""健康判据（企划第 17.2 节）。"""

from __future__ import annotations

import shutil
from collections import namedtuple
from pathlib import Path

import psutil
import pytest

from ctrlstore.config import Settings
from ctrlstore.health import (
    REASON_AVAILABLE_MEMORY,
    REASON_DISK_FREE_BYTES,
    REASON_DISK_FREE_RATIO,
    REASON_DISK_UNAVAILABLE,
    check_health,
)

GiB = 1024**3

#: 与 ``shutil.disk_usage`` 返回结构一致的最小替身。
_DiskUsage = namedtuple("_DiskUsage", "total used free")


class _Memory:
    def __init__(self, available: int) -> None:
        self.available = available


def _stub_disk(monkeypatch: pytest.MonkeyPatch, free: int, total: int) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: _DiskUsage(total, total - free, free))


def _stub_memory(monkeypatch: pytest.MonkeyPatch, available: int) -> None:
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(available))


def test_healthy_machine_reports_ok(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    _stub_disk(monkeypatch, free=500 * GiB, total=1000 * GiB)
    _stub_memory(monkeypatch, available=4 * GiB)

    report = check_health(settings)

    assert report.status == "ok"
    assert report.reasons == ()


def test_low_free_bytes_degrades(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    # 9 GB 剩余、100 GB 总量：绝对空间不足，比例仍高于 5%。
    _stub_disk(monkeypatch, free=9 * GiB, total=100 * GiB)
    _stub_memory(monkeypatch, available=4 * GiB)

    report = check_health(settings)

    assert report.status == "degraded"
    assert report.reasons == (REASON_DISK_FREE_BYTES,)


def test_low_free_ratio_degrades(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    # 剩余 4%：绝对空间很充裕，但比例低于 5%。
    _stub_disk(monkeypatch, free=40 * GiB, total=1000 * GiB)
    _stub_memory(monkeypatch, available=4 * GiB)

    report = check_health(settings)

    assert report.reasons == (REASON_DISK_FREE_RATIO,)


def test_low_memory_degrades(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    _stub_disk(monkeypatch, free=500 * GiB, total=1000 * GiB)
    _stub_memory(monkeypatch, available=800 * 1024**2)

    report = check_health(settings)

    assert report.reasons == (REASON_AVAILABLE_MEMORY,)


def test_reasons_accumulate(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    _stub_disk(monkeypatch, free=1 * GiB, total=1000 * GiB)
    _stub_memory(monkeypatch, available=100 * 1024**2)

    report = check_health(settings)

    assert report.status == "degraded"
    assert set(report.reasons) == {
        REASON_DISK_FREE_BYTES,
        REASON_DISK_FREE_RATIO,
        REASON_AVAILABLE_MEMORY,
    }


def test_missing_data_dir_measures_existing_parent(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _stub_disk(monkeypatch, free=500 * GiB, total=1000 * GiB)
    _stub_memory(monkeypatch, available=4 * GiB)
    assert not settings.data_dir.exists()

    report = check_health(settings)

    assert report.status == "ok"


def test_public_payload_has_only_status(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _stub_disk(monkeypatch, free=1 * GiB, total=1000 * GiB)
    _stub_memory(monkeypatch, available=4 * GiB)

    payload = check_health(settings).as_public()

    assert payload == {"status": "degraded"}
    assert list(payload) == ["status"]


def test_unreachable_disk_is_reported(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    _stub_memory(monkeypatch, available=4 * GiB)

    def _boom(_path: Path) -> None:
        raise PermissionError("injected")

    monkeypatch.setattr(shutil, "disk_usage", _boom)

    report = check_health(settings)

    assert report.status == "degraded"
    assert REASON_DISK_UNAVAILABLE in report.reasons
