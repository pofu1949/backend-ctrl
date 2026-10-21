"""Excel 预览与文本（企划第 9 章：xlsx）。

只读模式打开，最多 20 个工作表、每表 100 行、50 列。预览 JSON 存成派生文件
（``purpose=table_preview``），公开接口按需读取；检索正文是同一份内容的扁平文本。

``read_only=True`` 不会把整本工作簿读进内存；但仍然逐行检查时限。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import openpyxl

from .limits import LIMITS, TextBuilder, check_deadline, deadline


def extract(path: Path, limits=LIMITS) -> tuple[str, bool, dict]:
    """返回 (文本, 是否截断, 预览)。预览形如 ``{"sheets": [...], ...}``。"""
    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    builder = TextBuilder(limits.text_chars)
    at = deadline(limits)
    sheets: list[dict] = []
    truncated_sheets = False

    try:
        for index, sheet in enumerate(workbook.worksheets):
            if index >= limits.table_sheets:
                truncated_sheets = True
                break
            check_deadline(at)
            builder.add(sheet.title)
            header, rows, truncated = _read_sheet(sheet, limits, at, builder)
            sheets.append(
                {
                    "name": sheet.title,
                    "header": header,
                    "rows": rows,
                    "truncated": truncated,
                }
            )
            if builder.truncated:
                break
    finally:
        workbook.close()

    preview = {"sheets": sheets, "truncated_sheets": truncated_sheets}
    text, truncated = builder.result()
    return text, truncated, preview


def _read_sheet(sheet, limits, at: float, builder: TextBuilder) -> tuple[list, list, bool]:
    """读一个表：第一行作表头，之后最多 100 行。"""
    header: list[str] = []
    rows: list[list[str]] = []
    truncated = False

    for row_index, row in enumerate(sheet.iter_rows(values_only=True)):
        if row_index % 50 == 0:
            check_deadline(at)
        values = [_cell(value) for value in row[: limits.table_columns]]
        if len(row) > limits.table_columns:
            truncated = True
        if row_index == 0:
            header = values
            continue
        if len(rows) >= limits.table_rows:
            truncated = True
            break
        rows.append(values)
        builder.add("\t".join(values))
        if builder.truncated:
            truncated = True
            break

    return header, rows, truncated


def _cell(value) -> str:
    """单元格转字符串。日期按 ISO 写，None 为空串，其余用 str。"""
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)
