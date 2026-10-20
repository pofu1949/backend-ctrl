"""文件类型识别与存储键（规划第 3 部分的核心规则）。"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from ctrlstore.config import Settings
from ctrlstore.storage import keys
from ctrlstore.uploads import inspection


def minimal_png(width: int = 8, height: int = 8) -> bytes:
    """造一个最小合法 PNG，不需要 Pillow 参与。"""

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


def write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def inspect_file(tmp_path: Path, *, filename: str, payload: bytes, declared: str | None = None):
    path = write(tmp_path / "payload", payload)
    return inspection.inspect(
        path,
        filename=filename,
        declared_type=declared,
        settings=Settings(
            data_dir=tmp_path / "data",
            session_secret="s" * 40,
            pageview_hmac_secret="h" * 40,
        ),
        sha256="0" * 64,
        byte_size=len(payload),
    )


# —— 存储键 ——


def test_storage_key_is_derived_from_asset_id() -> None:
    key = keys.build("abcdef01-2345-6789-abcd-ef0123456789", 1, "jpg")

    assert key == "ab/abcdef01-2345-6789-abcd-ef0123456789/v1/original.jpg"
    assert keys.validate(key) == key


@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd",
        "aa/../v1/original.jpg",
        "/abs/aa/v1/original.jpg",
        "aa\\bb\\v1\\original.jpg",
        "aa/id/v1/original",
        "aa//v1/original.jpg",
        "zzz/id/v1/original.jpg",
        "aa/id/v0/original.jpg",
    ],
)
def test_bad_storage_keys_are_rejected(key: str) -> None:
    with pytest.raises(keys.InvalidStorageKey):
        keys.validate(key)


def test_storage_key_rejects_path_traversal_when_building(tmp_path: Path) -> None:
    with pytest.raises(keys.InvalidStorageKey):
        keys.build("id", 1, "jpg/../../etc")


# —— 类型识别 ——


def test_png_with_png_extension_passes(tmp_path: Path) -> None:
    result = inspect_file(tmp_path, filename="a.png", payload=minimal_png())

    assert result.kind == inspection.KIND_IMAGE
    assert result.mime == "image/png"
    assert (result.width, result.height) == (8, 8)


def test_extension_must_match_file_header(tmp_path: Path) -> None:
    with pytest.raises(inspection.RejectedFile, match="不符"):
        inspect_file(tmp_path, filename="a.jpg", payload=minimal_png())


def test_declared_type_must_match(tmp_path: Path) -> None:
    with pytest.raises(inspection.RejectedFile, match="声明类型"):
        inspect_file(tmp_path, filename="a.png", payload=minimal_png(), declared="image/gif")


def test_neutral_declared_type_is_ignored(tmp_path: Path) -> None:
    result = inspect_file(
        tmp_path, filename="a.png", payload=minimal_png(), declared="application/octet-stream"
    )

    assert result.kind == inspection.KIND_IMAGE


def test_text_declared_as_text_plain_is_accepted(tmp_path: Path) -> None:
    result = inspect_file(tmp_path, filename="a.md", payload=b"# hi\n", declared="text/plain")

    assert result.kind == inspection.KIND_MARKDOWN
    assert result.mime == "text/markdown"


@pytest.mark.parametrize("filename", ["a.html", "a.svg", "a.zip", "a.exe", "a.doc", "a"])
def test_unsupported_extensions_are_rejected(tmp_path: Path, filename: str) -> None:
    with pytest.raises(inspection.RejectedFile):
        inspect_file(tmp_path, filename=filename, payload=b"whatever")


def test_executable_header_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(inspection.RejectedFile, match="可执行"):
        inspect_file(tmp_path, filename="a.md", payload=b"MZ\x90\x00binary")


def test_plain_zip_is_not_an_office_file(tmp_path: Path) -> None:
    import zipfile

    path = tmp_path / "x.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hello.txt", "hi")

    with pytest.raises(inspection.RejectedFile, match="压缩包"):
        inspect_file(tmp_path, filename="x.docx", payload=path.read_bytes())


def test_binary_with_txt_extension_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(inspection.RejectedFile):
        inspect_file(tmp_path, filename="a.txt", payload=b"\x00\x01\x02\x03")


def test_non_utf8_text_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(inspection.RejectedFile):
        inspect_file(tmp_path, filename="a.txt", payload=b"\xff\xfe\xff\xfe")


def test_markdown_extension_variants(tmp_path: Path) -> None:
    assert inspect_file(tmp_path, filename="a.markdown", payload=b"x").kind == "markdown"
    assert inspect_file(tmp_path, filename="a.txt", payload=b"x").kind == "text"


def test_oversized_image_is_rejected_by_pixels(tmp_path: Path) -> None:
    # 8000×6000 = 4800 万像素，超过 4000 万上限；体积很小，只靠尺寸判定。
    payload = minimal_png(width=8000, height=6000)

    with pytest.raises(inspection.RejectedFile, match="像素"):
        inspect_file(tmp_path, filename="big.png", payload=payload)


def test_file_over_size_limit_is_rejected(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        session_secret="s" * 40,
        pageview_hmac_secret="h" * 40,
        limits={"text": 4},
    )
    path = write(tmp_path / "payload", b"hello world")

    with pytest.raises(inspection.RejectedFile, match="上限"):
        inspection.inspect(
            path,
            filename="a.txt",
            declared_type=None,
            settings=settings,
            sha256="0" * 64,
            byte_size=path.stat().st_size,
        )
