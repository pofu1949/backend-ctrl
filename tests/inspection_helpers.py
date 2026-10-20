"""测试用的最小合法文件。

图片用标准库拼出来，不依赖 Pillow 生成，也不把二进制样例放进仓库。
"""

from __future__ import annotations

import struct
import zlib


def minimal_png(width: int = 8, height: int = 8) -> bytes:
    """一个最小合法 PNG（RGB，无压缩优化）。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def minimal_docx() -> bytes:
    """只含必要条目的 docx 容器，足够被识别成 Word 文档。"""
    return _office_zip({"word/document.xml": b"<w:document/>"})


def minimal_xlsx() -> bytes:
    return _office_zip({"xl/workbook.xml": b"<workbook/>"})


def _office_zip(entries: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()
