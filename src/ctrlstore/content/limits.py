"""抽取的上限与共用工具（企划第 9 章、第 17.2 节）。

这些数字是硬上限，写在一处：

* 压缩包解压后总体积 ≤ 200 MB、条目 ≤ 2000；
* 单个文件处理 ≤ 60 秒；
* PDF ≤ 500 页；
* Excel ≤ 20 个表、每个表 100 行、50 列；
* 抽取文本 ≤ 20000 字符，超出截断并标记。

超限属于确定性失败：记 ``failed`` 或 ``skipped`` 后结束任务，不重试——
重试解决不了压缩炸弹或损坏文件，只会把队列堵住。
"""

from __future__ import annotations

import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

MiB = 1024**2


class ExtractionSkipped(Exception):
    """没有可抽取的文本、格式不支持或文件加密。保留原件，状态记 skipped。"""


class ExtractionFailed(Exception):
    """文件损坏、超限或超时。保留原件，状态记 failed。"""


@dataclass(frozen=True)
class ExtractLimits:
    """一次抽取的上限。测试可以替换成更小的值。"""

    text_chars: int = 20_000
    timeout_seconds: float = 60.0
    archive_bytes: int = 200 * MiB
    archive_entries: int = 2000
    pdf_pages: int = 500
    table_sheets: int = 20
    table_rows: int = 100
    table_columns: int = 50


LIMITS = ExtractLimits()


class TextBuilder:
    """按字符上限累积文本，超出后置位 ``truncated`` 并提前结束。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._parts: list[str] = []
        self._length = 0
        self.truncated = False

    def add(self, text: str) -> None:
        if self.truncated or not text:
            return
        remaining = self.limit - self._length
        if len(text) > remaining:
            self._parts.append(text[:remaining])
            self._length = remaining
            self.truncated = True
            return
        self._parts.append(text)
        self._length += len(text)

    def result(self) -> tuple[str, bool]:
        return "\n".join(part for part in self._parts if part), self.truncated


def deadline(limits: ExtractLimits = LIMITS) -> float:
    return time.monotonic() + limits.timeout_seconds


def check_deadline(at: float) -> None:
    if time.monotonic() > at:
        raise ExtractionFailed("处理超过 60 秒，已停止")


def check_archive(path: Path, limits: ExtractLimits = LIMITS) -> None:
    """打开 Office 文件之前先看 ZIP 目录：条目数、解压后体积与压缩比。

    中央目录可以谎报 ``file_size``，所以再加一道压缩比上限；真正的兜底是
    抽取时的硬超时（POSIX 上由 ``SIGALRM`` 打断单次库调用）。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > limits.archive_entries:
                raise ExtractionFailed(
                    f"压缩包条目过多：{len(entries)} > {limits.archive_entries}"
                )
            total = sum(entry.file_size for entry in entries)
            if total > limits.archive_bytes:
                raise ExtractionFailed(
                    f"解压后体积过大：{total} 字节 > {limits.archive_bytes} 字节"
                )
            compressed = sum(entry.compress_size for entry in entries)
            if compressed and total / compressed > 1000:
                raise ExtractionFailed("压缩比异常，拒绝解压")
    except zipfile.BadZipFile as exc:
        raise ExtractionFailed("压缩包已损坏，无法读取") from exc
