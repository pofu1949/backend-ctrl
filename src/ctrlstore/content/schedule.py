"""把抽取任务排进队列。

在版本登记时调用一次。抽取失败不影响内容本身；发布也不等抽取完成。
"""

from __future__ import annotations

from ..jobs import service as jobs
from ..models import Asset, AssetVersion
from ..uploads.inspection import (
    KIND_DOCX,
    KIND_MARKDOWN,
    KIND_PDF,
    KIND_TEXT,
    KIND_VIDEO,
    KIND_XLSX,
)

#: 需要文本抽取的类型。图片只生成缩略图，视频只探测与截封面。
EXTRACT_KINDS = frozenset({KIND_MARKDOWN, KIND_TEXT, KIND_DOCX, KIND_XLSX, KIND_PDF})


def schedule(db, *, asset: Asset, version: AssetVersion) -> None:
    """按类型入队。同一版本只应调用一次（登记新版本时）。"""
    if asset.kind == KIND_VIDEO:
        jobs.enqueue(db, jobs.KIND_VIDEO, {"version_id": version.id})
    elif asset.kind in EXTRACT_KINDS:
        jobs.enqueue(db, jobs.KIND_EXTRACT, {"version_id": version.id})
