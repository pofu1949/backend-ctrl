"""文件类型识别与校验。

原则：**不信浏览器申报的类型**，只信文件头（企划原则 5、第 9 章）。

一次检查做四件事：

1. 按扩展名确定期望类型，扩展名不在允许名单就直接拒绝（HTML、SVG、zip、exe 都在这里挡掉）；
2. 读文件头判断真实类型，必须与扩展名一致；
3. 客户端声明的 Content-Type 若存在且不是 ``application/octet-stream``，也必须一致；
4. 图片只读尺寸头，超过 4000 万像素拒绝——解码整图会打穿 worker 的内存上限。

字节上限由配置给出，检查放在同一处，避免「接口放过、worker 才发现」。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ..config import Settings

#: 内容类型（kind）。一个文件只有一个主类型。
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_MARKDOWN = "markdown"
KIND_PDF = "pdf"
KIND_DOCX = "docx"
KIND_XLSX = "xlsx"
KIND_TEXT = "text"

#: 允许的扩展名 → (kind, mime)。扩展名决定期望，文件头必须与之相符。
EXTENSIONS: dict[str, tuple[str, str]] = {
    ".jpg": (KIND_IMAGE, "image/jpeg"),
    ".jpeg": (KIND_IMAGE, "image/jpeg"),
    ".png": (KIND_IMAGE, "image/png"),
    ".gif": (KIND_IMAGE, "image/gif"),
    ".webp": (KIND_IMAGE, "image/webp"),
    ".mp4": (KIND_VIDEO, "video/mp4"),
    ".webm": (KIND_VIDEO, "video/webm"),
    ".md": (KIND_MARKDOWN, "text/markdown"),
    ".markdown": (KIND_MARKDOWN, "text/markdown"),
    ".pdf": (KIND_PDF, "application/pdf"),
    ".docx": (KIND_DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xlsx": (KIND_XLSX, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".txt": (KIND_TEXT, "text/plain"),
}

#: 图片像素上限。企划第 17.2 节：超过就拒绝，避免解码峰值打满内存。
MAX_IMAGE_PIXELS = 40_000_000

#: 读文件头用的字节数。ZIP 的中央目录不在头部，另行处理。
HEAD_BYTES = 4096

#: 声明类型里视为「没给」的值。
NEUTRAL_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

#: 声明类型的常见别名。浏览器和 curl 报的值并不完全统一。
DECLARED_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "video/quicktime": "video/mp4",
}

#: 纯文本类内容在文件头里没有签名，用这个哨兵交给扩展名去决定。
_TEXT_SENTINEL = "text/*"


class RejectedFile(ValueError):
    """文件不合规。消息可以直接给用户看。"""


@dataclass(frozen=True)
class Inspection:
    """一次识别结果。字段足以登记一条内容版本。"""

    kind: str
    mime: str
    extension: str
    byte_size: int
    sha256: str
    width: int | None = None
    height: int | None = None

    @property
    def max_side(self) -> int | None:
        if self.width is None or self.height is None:
            return None
        return max(self.width, self.height)


def kind_for_filename(filename: str) -> tuple[str, str]:
    """按扩展名给出 (kind, extension)。不支持的扩展名直接拒绝。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in EXTENSIONS:
        raise RejectedFile(f"不接受的文件类型：{suffix or '（无扩展名）'}")
    kind, _ = EXTENSIONS[suffix]
    return kind, suffix


def inspect(
    path: Path,
    *,
    filename: str,
    declared_type: str | None,
    settings: Settings,
    sha256: str,
    byte_size: int,
) -> Inspection:
    """完整校验一个已落盘的文件。哈希与大小由调用方边写边算，避免再读一遍。"""
    kind, extension = kind_for_filename(filename)
    expected_mime = EXTENSIONS[extension][1]

    detected = _detect(path)
    if detected == _TEXT_SENTINEL:
        # 文本没有文件头签名：只有 markdown 与纯文本可以落到这里。
        if kind not in (KIND_MARKDOWN, KIND_TEXT):
            raise RejectedFile(
                f"扩展名与文件内容不符：{extension} 声明为 {expected_mime}，实际是纯文本"
            )
        detected = expected_mime
    elif detected != expected_mime:
        raise RejectedFile(
            f"扩展名与文件内容不符：{extension} 声明为 {expected_mime}，实际为 {detected}"
        )

    declared = (declared_type or "").split(";")[0].strip().lower()
    declared = DECLARED_ALIASES.get(declared, declared)
    if not _declared_type_matches(declared, kind, expected_mime):
        raise RejectedFile(f"声明类型与文件内容不符：{declared} ≠ {expected_mime}")

    limit = settings.limits.for_kind(kind)
    if byte_size > limit:
        raise RejectedFile(f"超过 {kind} 的单文件上限（{limit} 字节）")

    width = height = None
    if kind == KIND_IMAGE:
        width, height = _image_size(path)
        if width * height > MAX_IMAGE_PIXELS:
            raise RejectedFile(f"图片像素过多：{width}×{height}")

    return Inspection(
        kind=kind,
        mime=expected_mime,
        extension=extension.lstrip("."),
        byte_size=byte_size,
        sha256=sha256,
        width=width,
        height=height,
    )


def _detect(path: Path) -> str:
    """读文件头判定真实类型，返回 mime。

    纯文本类没有签名，返回哨兵 :data:`_TEXT_SENTINEL`，由扩展名决定具体类型。
    """
    with path.open("rb") as handle:
        head = handle.read(HEAD_BYTES)

    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"PK\x03\x04"):
        return _office_mime(path)
    if head.startswith(b"MZ"):
        raise RejectedFile("不接受可执行文件")
    if _looks_like_text(head):
        return _TEXT_SENTINEL
    raise RejectedFile("无法识别的文件类型")


def _declared_type_matches(declared: str, kind: str, expected_mime: str) -> bool:
    """声明类型只做一致性检查，不作数。

    二进制类型要求完全一致；纯文本类只要求 ``text/*``——它们之间没有格式保证，
    浏览器对 ``.md`` 报 ``text/plain`` 很常见，按字面比较只会误伤。
    """
    if declared in NEUTRAL_TYPES:
        return True
    if kind in (KIND_MARKDOWN, KIND_TEXT):
        return declared.startswith("text/")
    return declared == expected_mime


def _office_mime(path: Path) -> str:
    """ZIP 容器只读条目名，不解压内容，避免解压炸弹。"""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise RejectedFile("压缩包已损坏或不是有效的 docx/xlsx") from exc

    if "word/document.xml" in names:
        return EXTENSIONS[".docx"][1]
    if "xl/workbook.xml" in names:
        return EXTENSIONS[".xlsx"][1]
    raise RejectedFile("不接受压缩包：只接收 docx 与 xlsx")


def _image_size(path: Path) -> tuple[int, int]:
    """只读图片头拿尺寸，不解码像素。"""
    try:
        with Image.open(path) as image:
            return image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise RejectedFile("图片已损坏或无法读取尺寸") from exc


def _looks_like_text(head: bytes) -> bool:
    """能按 UTF-8 解码、且没有 NUL 字节，才算文本。"""
    if b"\x00" in head:
        return False
    probe = head
    # 截断可能切在多字节字符中间，丢掉最后一个不完整字符再判断。
    for _ in range(4):
        try:
            probe.decode("utf-8")
            return True
        except UnicodeDecodeError:
            probe = probe[:-1]
    return False
