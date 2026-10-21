"""健康检查。

匿名接口只回答进程状态；所有者接口另给磁盘、队列、迁移版本与上次成功备份。

匿名状态只看三件事：可用内存、数据盘空间、公开区与原件区是否同一文件系统。
检查只读操作系统信息，不查询业务表：数据库还没迁移时也能回答进程是否活着；
迁移失败会以启动失败表现出来，而不是在这里假装 ``ok``。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil
from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from . import __version__
from .backup import latest_complete
from .config import Settings
from .jobs import service as jobs

HealthStatus = Literal["ok", "degraded"]

#: 降级原因。用稳定代号而不是中文句子，便于接口和日志区分。
REASON_DISK_FREE_BYTES = "disk_free_bytes"
REASON_DISK_FREE_RATIO = "disk_free_ratio"
REASON_AVAILABLE_MEMORY = "available_memory"
REASON_DISK_UNAVAILABLE = "disk_unavailable"
#: 公开区与原件区不在同一块盘：硬链接退化为复制，同样的字节要占两份空间。
REASON_PUBLIC_NOT_SAME_FS = "public_not_same_filesystem"


@dataclass(frozen=True)
class HealthReport:
    """一次健康检查的结果。"""

    status: HealthStatus
    reasons: tuple[str, ...] = ()

    def as_public(self) -> dict[str, str]:
        """匿名接口的响应体，只有 ``status``。"""
        return {"status": self.status}


def check_health(settings: Settings) -> HealthReport:
    """按内存与磁盘阈值给出 ``ok`` 或 ``degraded``。"""
    reasons: list[str] = []

    usage = disk_usage(settings.data_dir)
    if usage is None:
        reasons.append(REASON_DISK_UNAVAILABLE)
    else:
        free, total = usage
        if free < settings.min_free_bytes:
            reasons.append(REASON_DISK_FREE_BYTES)
        if total > 0 and free / total < settings.min_free_ratio:
            reasons.append(REASON_DISK_FREE_RATIO)

    if psutil.virtual_memory().available < settings.min_available_memory_bytes:
        reasons.append(REASON_AVAILABLE_MEMORY)

    if not _public_on_same_filesystem(settings):
        reasons.append(REASON_PUBLIC_NOT_SAME_FS)

    return HealthReport(status="degraded" if reasons else "ok", reasons=tuple(reasons))


def _public_on_same_filesystem(settings: Settings) -> bool:
    """公开区与原件区必须在同一块盘，否则发布只能复制，空间翻倍。"""
    assert settings.originals_dir is not None and settings.public_dir is not None
    originals = _existing(settings.originals_dir)
    public = _existing(settings.public_dir)
    if originals is None or public is None:
        return True
    try:
        return os.stat(originals).st_dev == os.stat(public).st_dev
    except OSError:
        return True


def _existing(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def disk_usage(path: Path) -> tuple[int, int] | None:
    """返回 (剩余字节, 总字节)。

    数据目录可能尚未创建（首次 ``init`` 之前），此时向上找最近一个存在的目录，
    量的是同一块盘；整条路径都不存在则视为无法判断。
    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            try:
                usage = shutil.disk_usage(candidate)
            except OSError:
                return None
            return usage.free, usage.total
    return None


def admin_report(db: DbSession, settings: Settings) -> dict:
    """所有者健康接口（企划第 9 部分）。

    匿名接口仍然只有 ``status``；磁盘细节、队列、上次备份与版本只出现在这里。
    """
    report = check_health(settings)
    usage = disk_usage(settings.data_dir)
    schema_version = db.scalar(text("SELECT version_num FROM alembic_version"))
    return {
        "status": report.status,
        "reasons": list(report.reasons),
        "app_version": __version__,
        "schema_version": schema_version,
        "disk": {
            "data_dir": str(settings.data_dir),
            "free_bytes": usage[0] if usage else None,
            "total_bytes": usage[1] if usage else None,
        },
        "jobs": jobs.counts(db),
        "last_backup": latest_complete(settings.backup_dir),
    }
