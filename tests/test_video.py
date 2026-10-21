"""视频探测与封面（规划第 9 部分）。

覆盖：编码 → 可播放标记的判定、没有 ffprobe/ffmpeg 时跳过、探测失败不改原件、
封面只在已发布版本上进入公开区、WebM 地址仍是 .webm、仅站点不出现在 /p。
"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import admin_headers
from tests.file_samples import fake_video
from tests.test_public_api import (
    _add,
    _collection,
    _grant,
    _site,
    _site_client,
    _token,
)
from tests.test_publish import _public_files, _publish, _set_visibility

from ctrlstore.content import video
from ctrlstore.content.limits import ExtractionFailed
from ctrlstore.content.video import Probe
from ctrlstore.jobs import service as jobs
from ctrlstore.models import (
    DERIVATIVE_READY,
    EXTRACT_FAILED,
    EXTRACT_READY,
    EXTRACT_SKIPPED,
    AssetVersion,
    Derivative,
    Job,
)
from ctrlstore.storage import LocalStorage
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


def _upload(client: TestClient, name: str, data: bytes, mime: str) -> dict:
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": (name, data, mime)},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _version(db: DbSession, asset_id: str) -> AssetVersion:
    db.expire_all()
    version = db.scalar(
        select(AssetVersion)
        .where(AssetVersion.asset_id == asset_id)
        .order_by(AssetVersion.version_no.desc())
    )
    assert version is not None
    return version


def _poster(db: DbSession, version_id: str) -> Derivative | None:
    db.expire_all()
    return db.scalar(
        select(Derivative).where(
            Derivative.version_id == version_id, Derivative.purpose == "poster"
        )
    )


def _fake_poster(source, destination, **kwargs):
    from PIL import Image

    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (0, 0, 0)).save(destination, "JPEG")
    return True


# —— 编码判定 ——


@pytest.mark.parametrize(
    ("codec", "audio", "container", "expected"),
    [
        ("h264", "aac", ".mp4", "playable"),
        ("h264", None, ".mp4", "playable"),
        ("hevc", "aac", ".mp4", "download_only"),
        ("vp9", "opus", ".webm", "playable"),
        ("av1", "opus", ".webm", "playable"),
        ("vp8", "vorbis", ".webm", "playable"),
        ("vp9", "aac", ".webm", "download_only"),
        ("h264", "aac", ".avi", "download_only"),
    ],
)
def test_playback_markers(codec, audio, container, expected) -> None:
    assert video.playback_for(codec, audio, container=container) == expected


# —— 没有工具 ——


def test_missing_ffprobe_skips_probe(
    owner_client: TestClient, db: DbSession, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video, "probe", lambda path, container: None)
    created = _upload(owner_client, "clip.mp4", fake_video(".mp4"), "video/mp4")
    run_until_idle(settings)

    version = _version(db, created["asset_id"])
    assert version.extract_status == EXTRACT_SKIPPED
    assert version.playback is None
    assert _poster(db, version.id) is None
    db.expire_all()
    job = db.scalar(select(Job).where(Job.kind == jobs.KIND_VIDEO))
    assert (job.status, job.attempts) == ("done", 1)


def test_missing_ffmpeg_leaves_no_poster(
    owner_client: TestClient,
    client: TestClient,
    db: DbSession,
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video, "probe", lambda path, container: Probe("playable", 640, 360, 1000))
    monkeypatch.setattr(video, "render_poster", lambda source, destination, **kwargs: False)
    created = _upload(owner_client, "clip.mp4", fake_video(".mp4"), "video/mp4")
    run_until_idle(settings)
    _publish(owner_client, created["asset_id"])

    version = _version(db, created["asset_id"])
    assert _poster(db, version.id) is None
    payload = client.get(f"{PUBLIC}/assets/{created['asset_id']}").json()
    assert "poster" not in payload["urls"]


# —— 探测与封面 ——


def test_probe_result_and_poster_are_published(
    owner_client: TestClient,
    client: TestClient,
    db: DbSession,
    settings,
    storage: LocalStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video, "probe", lambda path, container: Probe("playable", 1280, 720, 5000))
    monkeypatch.setattr(video, "render_poster", _fake_poster)
    created = _upload(owner_client, "clip.mp4", fake_video(".mp4"), "video/mp4")
    run_until_idle(settings)
    _publish(owner_client, created["asset_id"])

    version = _version(db, created["asset_id"])
    assert version.extract_status == EXTRACT_READY
    assert (version.playback, version.duration_ms, version.width) == ("playable", 5000, 1280)
    poster = _poster(db, version.id)
    assert poster is not None and poster.status == DERIVATIVE_READY
    assert storage.public_derivative_path(created["asset_id"], "poster").is_file()

    payload = client.get(f"{PUBLIC}/assets/{created['asset_id']}").json()
    assert payload["playback"] == "playable"
    assert payload["duration_ms"] == 5000
    assert payload["filename"] == "clip.mp4"
    assert payload["urls"]["original"].endswith("/original.mp4")
    assert payload["urls"]["poster"].endswith("/poster.jpg")


def test_webm_keeps_webm_extension(
    owner_client: TestClient,
    client: TestClient,
    db: DbSession,
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video, "probe", lambda path, container: Probe("playable", 640, 360, 1000))
    monkeypatch.setattr(video, "render_poster", _fake_poster)
    created = _upload(owner_client, "clip.webm", fake_video(".webm"), "video/webm")
    run_until_idle(settings)
    _publish(owner_client, created["asset_id"])

    payload = client.get(f"{PUBLIC}/assets/{created['asset_id']}").json()

    assert payload["urls"]["original"].endswith("/original.webm")


def test_site_only_video_stays_out_of_public_dir(
    owner_client: TestClient,
    client: TestClient,
    db: DbSession,
    settings,
    storage: LocalStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video, "probe", lambda path, container: Probe("playable", 640, 360, 1000))
    monkeypatch.setattr(video, "render_poster", _fake_poster)
    site = _site(owner_client, "main")
    token = _token(owner_client, "main")
    created = _upload(owner_client, "clip.webm", fake_video(".webm"), "video/webm")
    collection = _collection(owner_client, "videos.internal")
    _add(owner_client, collection["id"], created["asset_id"])
    _grant(owner_client, site["id"], "videos")
    run_until_idle(settings)
    detail = owner_client.get(f"{API}/assets/{created['asset_id']}").json()
    detail = _set_visibility(owner_client, detail, "site")
    owner_client.post(
        f"{API}/assets/{created['asset_id']}/publish",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    # 探测与封面都完成了，但公开区没有它；站点拿到的是签名地址。
    assert _public_files(storage, created["asset_id"]) == set()
    payload = _site_client(client, token).get(
        f"{PUBLIC}/assets/{created['asset_id']}"
    ).json()
    assert set(payload["urls"]) == {"signed"}
    assert client.get(f"{PUBLIC}/assets/{created['asset_id']}").status_code == 404


def test_probe_failure_keeps_original(
    owner_client: TestClient,
    client: TestClient,
    db: DbSession,
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(path, container):
        raise ExtractionFailed("ffprobe 无法解析该视频")

    monkeypatch.setattr(video, "probe", broken)
    created = _upload(owner_client, "broken.mp4", fake_video(".mp4"), "video/mp4")
    run_until_idle(settings)

    version = _version(db, created["asset_id"])
    assert version.extract_status == EXTRACT_FAILED
    _publish(owner_client, created["asset_id"])
    assert client.get(f"{PUBLIC}/assets/{created['asset_id']}").status_code == 200
