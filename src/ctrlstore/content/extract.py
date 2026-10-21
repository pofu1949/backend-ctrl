"""抽取调度：按内容类型选一个抽取器（企划第 9 章）。

所有抽取器只读原件，返回文本、截断标记与可选的结构化结果（PDF 页数、Excel 预览）。
具体上限与异常见 :mod:`ctrlstore.content.limits`。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..uploads.inspection import (
    KIND_DOCX,
    KIND_MARKDOWN,
    KIND_PDF,
    KIND_TEXT,
    KIND_XLSX,
)
from . import docx_text, pdf_text, xlsx_preview
from .limits import (
    LIMITS,
    ExtractionFailed,
    ExtractionSkipped,
    TextBuilder,
    check_archive,
)


@dataclass(frozen=True)
class Extracted:
    """一次抽取的结果。``table`` 只有 xlsx 会给。"""

    text: str
    truncated: bool = False
    page_count: int | None = None
    table: dict | None = None


def run(path: Path, *, kind: str, limits=LIMITS) -> Extracted:
    """按类型抽取。已知的「跳过/失败」原样抛出，其他异常统一收成失败。"""
    try:
        if kind in (KIND_MARKDOWN, KIND_TEXT):
            return _plain(path, limits)
        if kind == KIND_DOCX:
            check_archive(path, limits)
            text, truncated = docx_text.extract(path, limits)
            return Extracted(text=text, truncated=truncated)
        if kind == KIND_XLSX:
            check_archive(path, limits)
            text, truncated, preview = xlsx_preview.extract(path, limits)
            return Extracted(text=text, truncated=truncated, table=preview)
        if kind == KIND_PDF:
            text, truncated, pages = pdf_text.extract(path, limits)
            return Extracted(text=text, truncated=truncated, page_count=pages)
    except (ExtractionFailed, ExtractionSkipped):
        raise
    except Exception as exc:  # 损坏、编码异常、库自己抛的错
        raise ExtractionFailed(f"无法读取文件：{exc}") from exc
    raise ExtractionSkipped(f"这类内容不抽取文本：{kind}")


def _plain(path: Path, limits) -> Extracted:
    """Markdown / 纯文本：文件本身就是要索引的正文。"""
    raw = path.read_bytes()
    # 允许带 BOM；解码失败按替换字符处理，不让一行坏字节挡住整篇。
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    builder = TextBuilder(limits.text_chars)
    builder.add(text)
    body, truncated = builder.result()
    return Extracted(text=body, truncated=truncated)
