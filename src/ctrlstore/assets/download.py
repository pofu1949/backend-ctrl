"""文件发送：管理下载与站点签名下载。

生产环境走 Nginx 内部跳转（企划第 7.1 节）：API 只校验权限，回一个
``X-Accel-Redirect`` 头，字节由 Nginx 用 sendfile 发送，支持 Range。
``originals/`` 在 Nginx 里只配 ``internal`` location，外部 URL 进不去。

开发环境没有 Nginx，``x_accel_redirect = false`` 时直接由进程发送文件。
两种模式共用同一套权限与状态判断。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import FileResponse

from ..config import Settings
from ..errors import ApiError
from ..models import Asset, AssetVersion
from ..storage import LocalStorage, keys

#: Nginx 内部跳转前缀，与 deploy/nginx 示例里的 location 对应。
INTERNAL_PREFIX = "/_internal/originals/"


def send_file(
    request: Request,
    *,
    settings: Settings,
    storage: LocalStorage,
    asset: Asset,
    version: AssetVersion,
) -> Response:
    """把某个版本的原件发出去。调用方负责先做权限与状态检查。"""
    path = storage.original_path(version.storage_key)
    if not path.is_file():
        raise ApiError.not_found("文件不存在")

    headers = {
        "Content-Type": version.mime,
        # 原始文件名要按 RFC 5987 编码；磁盘路径里不出现它（企划第 9 章）。
        "Content-Disposition": _disposition(version.original_filename),
        # 私有内容不进任何共享缓存。
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }

    if settings.x_accel_redirect:
        return Response(
            status_code=200,
            headers={**headers, "X-Accel-Redirect": _internal_uri(version.storage_key)},
        )
    return FileResponse(
        path,
        media_type=version.mime,
        headers=headers,
    )


def file_path(storage: LocalStorage, version: AssetVersion) -> Path:
    return storage.original_path(keys.validate(version.storage_key))


def _internal_uri(storage_key: str) -> str:
    """内部 location 的 URI。片段逐个转义，避免文件名里的特殊字符改变路径。"""
    return INTERNAL_PREFIX + "/".join(quote(part, safe="") for part in storage_key.split("/"))


def _disposition(filename: str) -> str:
    """RFC 5987：ASCII 回退 + UTF-8 文件名。"""
    fallback = filename.encode("ascii", "ignore").decode() or "download"
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


def now_seconds() -> int:
    return int(datetime.now(UTC).timestamp())
