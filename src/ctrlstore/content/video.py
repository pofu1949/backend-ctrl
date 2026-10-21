"""视频探测与封面（企划第 9 章：video）。

不转码、不改写原件。有 ``ffprobe`` 时判断能不能在浏览器里直接播；有
``ffmpeg`` 时截一张封面。两者都不是 Python 依赖，缺失就跳过，内容照常入库。

可播放的判定按容器分别看编码：

* MP4：H.264（音频 AAC 或没有音频）；
* WebM：VP8 / VP9 / AV1（音频 Opus、Vorbis 或没有音频）。

其余标成 ``download_only``——Range 能取到字节，不等于浏览器能播。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..models import PLAYBACK_DOWNLOAD_ONLY, PLAYBACK_PLAYABLE
from .limits import ExtractionFailed

#: 探测与截图的超时。视频很大时 ffprobe 读文件头也很快。
PROBE_TIMEOUT = 30
POSTER_TIMEOUT = 60

#: 封面最长边。够列表和详情页用，不必保留 4K 静帧。
POSTER_WIDTH = 1280

#: 各容器的可播放编码组合。
PLAYABLE: dict[str, tuple[frozenset[str], frozenset[str | None]]] = {
    ".mp4": (frozenset({"h264"}), frozenset({None, "aac"})),
    ".webm": (frozenset({"vp8", "vp9", "av1"}), frozenset({None, "opus", "vorbis"})),
}


@dataclass(frozen=True)
class Probe:
    """一次探测的结果。``playback`` 取 ``playable`` / ``download_only``。"""

    playback: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


def probe(path: Path, *, container: str) -> Probe | None:
    """读编码信息。没有 ``ffprobe`` 时返回 ``None``；文件读不了抛 ``ExtractionFailed``。"""
    tool = shutil.which("ffprobe")
    if tool is None:
        return None

    try:
        result = subprocess.run(
            [
                tool,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            check=False,
            capture_output=True,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExtractionFailed(f"ffprobe 无法读取视频：{exc}") from exc
    if result.returncode != 0:
        raise ExtractionFailed("ffprobe 无法解析该视频")

    try:
        data = json.loads(result.stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError as exc:
        raise ExtractionFailed("ffprobe 输出不是 JSON") from exc
    return _from_streams(data, container=container)


def render_poster(source: Path, destination: Path, *, timeout: float = POSTER_TIMEOUT) -> bool:
    """截第一帧成 JPEG。没有 ``ffmpeg``、或截图失败时返回假。"""
    tool = shutil.which("ffmpeg")
    if tool is None:
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f"{destination.name}.tmp.jpg")
    staging.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [
                tool,
                "-y",
                "-threads",
                "1",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale={POSTER_WIDTH}:-2",
                str(staging),
            ],
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        staging.unlink(missing_ok=True)
        return False

    if result.returncode != 0 or not staging.is_file():
        staging.unlink(missing_ok=True)
        return False
    os.replace(staging, destination)
    return True


def _from_streams(data: dict, *, container: str) -> Probe:
    streams = data.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video is None:
        raise ExtractionFailed("视频没有画面流")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)

    playback = playback_for(
        video.get("codec_name"),
        audio.get("codec_name") if audio else None,
        container=container,
    )
    duration = (data.get("format") or {}).get("duration")
    return Probe(
        playback=playback,
        width=_positive_int(video.get("width")),
        height=_positive_int(video.get("height")),
        duration_ms=_duration_ms(duration),
    )


def playback_for(
    video_codec: str | None, audio_codec: str | None, *, container: str
) -> str:
    """按容器与编码给出可播放标记。未知容器一律「仅下载」。"""
    allowed = PLAYABLE.get(container.lower())
    if allowed is None:
        return PLAYBACK_DOWNLOAD_ONLY
    video_codecs, audio_codecs = allowed
    if video_codec in video_codecs and audio_codec in audio_codecs:
        return PLAYBACK_PLAYABLE
    return PLAYBACK_DOWNLOAD_ONLY


def _positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _duration_ms(value) -> int | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return int(seconds * 1000)
