"""本地文件系统存储。

第一期只有这一种实现，但接口按「对象键」来设计：所有路径都由
:meth:`LocalStorage.original_path` 从键推导，换 MinIO 时只需要替换这个类
（企划第 16.2 节）。

写入策略：先落到 ``tmp/uploads/<session>/``，校验通过后再 ``os.replace`` 到
``originals/``。两个目录在同一个数据盘上，移动是原子的，不会出现半个文件被当成原件。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from . import keys


class TooLarge(Exception):
    """写入超过上限。已写入的部分由调用方清理。"""


class PartWriter:
    """分片写入器。

    边写边检查整份文件的累计上限，超了就停手并删掉已写的这一段——不留半截分片，
    这样「缺片」与「超限」在状态上是两件事。
    """

    def __init__(self, path: Path, *, limit: int, already_used: int) -> None:
        self.path = path
        self.limit = limit
        self.already_used = already_used
        self.written = 0
        self._handle = None

    def __enter__(self) -> PartWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("wb")
        return self

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.written += len(chunk)
        if self.already_used + self.written > self.limit:
            raise TooLarge("超过单文件上限")
        assert self._handle is not None
        self._handle.write(chunk)

    def close(self) -> int:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        return self.written

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
        if exc_type is not None:
            self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class LocalStorage:
    """数据目录上的读写。无状态，可随请求创建。"""

    originals_dir: Path
    tmp_dir: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> LocalStorage:
        assert settings.originals_dir is not None and settings.tmp_dir is not None
        return cls(originals_dir=settings.originals_dir, tmp_dir=settings.tmp_dir)

    # —— 路径推导 ——

    def original_path(self, key: str) -> Path:
        """键到原件路径。先校验键，再拼目录，避免出现越界写入。"""
        return self.originals_dir / keys.validate(key)

    def session_dir(self, session_id: str) -> Path:
        return self.tmp_dir / "uploads" / _safe_segment(session_id)

    def parts_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "parts"

    def part_path(self, session_id: str, index: int) -> Path:
        return self.parts_dir(session_id) / f"{index:06d}.part"

    def merged_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "merged"

    # —— 写入 ——

    def prepare_session(self, session_id: str) -> None:
        self.parts_dir(session_id).mkdir(parents=True, exist_ok=True)

    def open_part(self, session_id: str, index: int, limit: int) -> PartWriter:
        """打开一片用于写入。同一序号重传就从零覆盖。"""
        path = self.part_path(session_id, index)
        # 先记下别的分片已占用的字节，避免和正在写的这一段重复计算。
        others = self.parts_size(session_id) - (path.stat().st_size if path.is_file() else 0)
        return PartWriter(path, limit=limit, already_used=others)

    def write_part(self, session_id: str, index: int, chunks: Iterable[bytes], limit: int) -> int:
        """同步写入一段字节。命令行导入这类本地路径走这里。"""
        with self.open_part(session_id, index, limit) as writer:
            for chunk in chunks:
                writer.write(chunk)
        return writer.written

    def parts_size(self, session_id: str) -> int:
        """已收到的分片总字节。数量很少，直接遍历目录。"""
        directory = self.parts_dir(session_id)
        if not directory.is_dir():
            return 0
        return sum(item.stat().st_size for item in directory.iterdir() if item.is_file())

    def merge_parts(self, session_id: str, indices: list[int]) -> tuple[Path, str, int]:
        """按序号顺序合并分片，边合并边算 SHA-256。

        只读一遍磁盘就把「合并」和「哈希」做完，视频这种大文件不会因此读两次。
        """
        merged = self.merged_path(session_id)
        merged.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with merged.open("wb") as target:
            for index in indices:
                part = self.part_path(session_id, index)
                if not part.is_file():
                    raise FileNotFoundError(f"缺少分片 {index}")
                with part.open("rb") as source:
                    while chunk := source.read(_CHUNK):
                        digest.update(chunk)
                        size += len(chunk)
                        target.write(chunk)
        return merged, digest.hexdigest(), size

    def store_original(self, source: Path, key: str) -> Path:
        """把校验通过的文件放到原位。同盘移动是原子的。"""
        destination = self.original_path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        return destination

    def write_original(self, data: bytes, key: str) -> Path:
        """直接把内存里的字节写成原件（直接创建的 Markdown / 纯文本）。

        先写临时文件再改名，和上传一样不会留下半个文件。
        """
        destination = self.original_path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging_dir = self.tmp_dir / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging = staging_dir / f"{uuid.uuid4().hex}.tmp"
        try:
            staging.write_bytes(data)
            os.replace(staging, destination)
        finally:
            staging.unlink(missing_ok=True)
        return destination

    def hash_file(self, path: Path) -> tuple[str, int]:
        """流式计算哈希与字节数。"""
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    # —— 清理 ——

    def discard_session(self, session_id: str) -> None:
        """删掉整个临时目录。失败不抛：清理失败不应掩盖真正的错误。"""
        shutil.rmtree(self.session_dir(session_id), ignore_errors=True)

    def discard_original(self, key: str) -> None:
        with contextlib.suppress(OSError, keys.InvalidStorageKey):
            self.original_path(key).unlink(missing_ok=True)

    def free_bytes(self) -> int:
        """数据盘剩余空间。数据目录还没建立时向上找已有的父目录。"""
        for candidate in (self.originals_dir, *self.originals_dir.parents):
            if candidate.exists():
                return shutil.disk_usage(candidate).free
        return 0


#: 读写块大小。1 MB 在内存与系统调用次数之间比较平衡。
_CHUNK = 1024 * 1024


def _safe_segment(value: str) -> str:
    """会话 ID 是服务端生成的 UUID；这里仍挡一次，避免拼出越界路径。"""
    if not value or not all(char.isalnum() or char == "-" for char in value):
        raise ValueError("非法的会话标识")
    return value
