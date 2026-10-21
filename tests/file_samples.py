"""测试用的真实文档样例。

用各库自己的写入器生成，而不是把二进制放进仓库：读取方（python-docx、
openpyxl、pypdf）看到的就是标准结构，不会因为样例太简陋而绕过真实解析路径。
"""

from __future__ import annotations

import io
import zipfile

import docx
import openpyxl
from pypdf import PdfWriter


def docx_bytes(*paragraphs: str, table: list[list[str]] | None = None) -> bytes:
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for column_index, value in enumerate(row):
                grid.rows[row_index].cells[column_index].text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes(rows: list[list[object]], *, title: str = "Sheet1") -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pdf_bytes(text: str = "Hello CtrlStore", *, pages: int = 1) -> bytes:
    """最小可解析 PDF。每个页面一段文字，用来测 pypdf 的抽取路径。"""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    page_count = max(1, pages)
    contents_number = 3 + page_count
    font_number = contents_number + 1
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{index} 0 R".encode() for index in range(3, 3 + page_count))
        + b"] /Count "
        + str(page_count).encode()
        + b" >>",
    ]
    for _ in range(page_count):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents "
            + str(contents_number).encode()
            + b" 0 R /Resources << /Font << /F1 "
            + str(font_number).encode()
            + b" 0 R >> >> >>"
        )
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def encrypted_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def fake_video(container: str = ".mp4") -> bytes:
    """只有文件头的假视频。类型识别只看头部；探测会因内容不完整而失败/跳过。"""
    if container == ".mp4":
        return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
    if container == ".webm":
        return b"\x1aE\xdf\xa3" + b"\x00" * 64
    raise ValueError(container)


def zip_with_many_entries(count: int) -> bytes:
    """条目数超过上限的 Office 容器：迁移/上传能看到 document.xml，但抽取必须拒绝。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<w:document/>")
        for index in range(count):
            archive.writestr(f"word/extra/{index}.xml", b"")
    return buffer.getvalue()
