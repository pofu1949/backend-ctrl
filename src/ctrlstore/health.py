"""健康检查。

两项判据（企划第 17.2 节）：可用内存和数据库所在数据盘的空间。

检查只读操作系统信息，不查询业务表：数据库还没迁移时也能回答进程是否活着；
迁移失败会以启动失败表现出来，而不是在这里假装 ``ok``。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil

from .config import Settings

HealthStatus = Literal["ok", "degraded"]

#: 降级原因。用稳定代号而不是中文句子，便于接口和日志区分。
REASON_DISK_FREE_BYTES = "disk_free_bytes"
REASON_DISK_FREE_RATIO = "disk_free_ratio"
REASON_AVAILABLE_MEMORY = "available_memory"
REASON_DISK_UNAVAILABLE = "disk_unavailable"


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

    usage = _disk_usage(settings.data_dir)
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

    return HealthReport(status="degraded" if reasons else "ok", reasons=tuple(reasons))


def _disk_usage(path: Path) -> tuple[int, int] | None:
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
