"""备份清单的读取（企划第 21 章）。

备份目录由 ``scripts/backup.sh`` 写入：每次备份一个以时间命名的子目录，
里面放数据库、文件目录与 ``manifest.json``。只有 ``status = complete`` 的
清单才算可恢复；失败的目录不能被当成恢复源。

所有者健康接口用这里读「上次成功备份」，只取展示需要的字段，不返回路径。
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_NAME = "manifest.json"

#: 清单里成功状态的值。
STATUS_COMPLETE = "complete"


def latest_complete(root: Path | None) -> dict | None:
    """最近一次成功备份的摘要；没有就返回 ``None``。"""
    if root is None or not root.is_dir():
        return None
    for directory in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True):
        manifest = _read(directory / MANIFEST_NAME)
        if manifest is None or manifest.get("status") != STATUS_COMPLETE:
            continue
        return {
            "backup": directory.name,
            "completed_at": manifest.get("completed_at"),
            "app_version": manifest.get("app_version"),
            "schema_version": manifest.get("schema_version"),
        }
    return None


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None
