"""Word 抽取纯文本（企划第 9 章：docx）。

只读，不修改用户文件。段落与表格都进正文；表格按行用制表符连接，
让检索能命中单元格里的词。不做版式还原——第一期 Word 的定位是下载与检索。
"""

from __future__ import annotations

from pathlib import Path

import docx

from .limits import LIMITS, TextBuilder, check_deadline, deadline


def extract(path: Path, limits=LIMITS) -> tuple[str, bool]:
    """返回 (文本, 是否截断)。"""
    document = docx.Document(str(path))
    builder = TextBuilder(limits.text_chars)
    at = deadline(limits)

    for index, paragraph in enumerate(document.paragraphs):
        if index % 200 == 0:
            check_deadline(at)
        builder.add(paragraph.text)
        if builder.truncated:
            return builder.result()

    for table_index, table in enumerate(document.tables):
        if table_index % 20 == 0:
            check_deadline(at)
        for row in table.rows:
            builder.add("\t".join(cell.text for cell in row.cells))
            if builder.truncated:
                return builder.result()
    return builder.result()
