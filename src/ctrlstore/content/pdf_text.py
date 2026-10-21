"""PDF 文本与首页图（企划第 9 章：pdf）。

文本用 ``pypdf`` 逐页抽取，最多 500 页；首页图调用系统 ``pdftoppm``，
没有这个工具就跳过——不影响上传与发布。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

from .limits import (
    LIMITS,
    ExtractionSkipped,
    TextBuilder,
    check_deadline,
    deadline,
)


def extract(path: Path, limits=LIMITS) -> tuple[str, bool, int]:
    """返回 (文本, 是否截断, 总页数)。"""
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ExtractionSkipped("PDF 已加密，不抽取文本")

    pages = len(reader.pages)
    builder = TextBuilder(limits.text_chars)
    at = deadline(limits)
    truncated = pages > limits.pdf_pages

    for index, page in enumerate(reader.pages):
        if index >= limits.pdf_pages:
            truncated = True
            break
        if index % 10 == 0:
            check_deadline(at)
        builder.add(page.extract_text() or "")
        if builder.truncated:
            truncated = True
            break

    text, text_truncated = builder.result()
    return text, truncated or text_truncated, pages


def render_first_page(source: Path, destination: Path, *, timeout: float = 60) -> bool:
    """把第一页渲染成 JPEG。找不到 ``pdftoppm`` 时返回假。"""
    tool = shutil.which("pdftoppm")
    if tool is None:
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as workspace:
        stem = Path(workspace) / "page"
        try:
            subprocess.run(
                [
                    tool,
                    "-jpeg",
                    "-singlefile",
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-r",
                    "72",
                    str(source),
                    str(stem),
                ],
                check=False,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        produced = stem.with_suffix(".jpg")
        if not produced.is_file():
            return False
        os.replace(produced, destination)
    return True
