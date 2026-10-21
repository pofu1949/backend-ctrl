"""文档抽取、表格预览与 PDF 首页图（规划第 9 部分）。

覆盖：docx 正文与表格、xlsx 表头/前 100 行/预览接口、PDF 文本与页数、
加密 PDF 跳过、压缩包条目超限不重试、Markdown 正文入索引、
公开 JSON 带原始文件名、PDF 首页图进入公开区。
"""

from __future__ import annotations

import shutil
from collections import namedtuple

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import admin_headers
from tests.file_samples import (
    docx_bytes,
    encrypted_pdf_bytes,
    pdf_bytes,
    xlsx_bytes,
    zip_with_many_entries,
)
from tests.test_publish import _publish

from ctrlstore.content import docx_text, pdf_text
from ctrlstore.content.limits import (
    ExtractionFailed,
    ExtractLimits,
    TextBuilder,
    check_archive,
)
from ctrlstore.jobs import service as jobs
from ctrlstore.models import (
    DERIVATIVE_READY,
    EXTRACT_FAILED,
    EXTRACT_READY,
    EXTRACT_SKIPPED,
    PURPOSE_TABLE_PREVIEW,
    AssetVersion,
    Derivative,
    Job,
)
from ctrlstore.storage import LocalStorage
from ctrlstore.worker import run_until_idle

API = "/api/admin/v1"
PUBLIC = "/api/public/v1"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def _upload(client: TestClient, name: str, data: bytes, mime: str) -> dict:
    response = client.post(
        f"{API}/uploads/direct",
        files={"file": (name, data, mime)},
        headers=admin_headers(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _version(db: DbSession, asset_id: str) -> AssetVersion:
    db.expire_all()
    version = db.scalar(
        select(AssetVersion)
        .where(AssetVersion.asset_id == asset_id)
        .order_by(AssetVersion.version_no.desc())
    )
    assert version is not None
    return version


def _derivative(db: DbSession, version_id: str, purpose: str) -> Derivative | None:
    db.expire_all()
    return db.scalar(
        select(Derivative).where(
            Derivative.version_id == version_id, Derivative.purpose == purpose
        )
    )


def _job(db: DbSession, kind: str) -> Job:
    db.expire_all()
    job = db.scalar(select(Job).where(Job.kind == kind).order_by(Job.created_at.desc()))
    assert job is not None
    return job


# —— 各类抽取 ——


def test_docx_text_includes_paragraphs_and_tables(
    owner_client: TestClient, db: DbSession, settings
) -> None:
    created = _upload(
        owner_client,
        "report.docx",
        docx_bytes(
            "第一段：CtrlStore 季度报告",
            "第二段",
            table=[["项目", "金额"], ["服务器", "1200"]],
        ),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    run_until_idle(settings)

    version = _version(db, created["asset_id"])
    assert version.extract_status == EXTRACT_READY
    assert "CtrlStore" in (version.text_extract or "")
    assert "服务器" in (version.text_extract or "")
    assert _job(db, jobs.KIND_EXTRACT).status == "done"


def test_xlsx_preview_and_table_endpoint(
    owner_client: TestClient, client: TestClient, db: DbSession, settings
) -> None:
    created = _upload(
        owner_client,
        "budget.xlsx",
        xlsx_bytes([["项目", "金额"], ["服务器", 1200], ["带宽", 300]], title="预算"),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    _publish(owner_client, created["asset_id"])

    # 抽取任务还没跑：预览接口给出 pending，而不是 404。
    pending = client.get(f"{PUBLIC}/assets/{created['asset_id']}/table")
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"

    run_until_idle(settings)

    version = _version(db, created["asset_id"])
    preview = _derivative(db, version.id, PURPOSE_TABLE_PREVIEW)
    assert preview is not None and preview.status == DERIVATIVE_READY

    response = client.get(
        f"{PUBLIC}/assets/{created['asset_id']}/table", params={"sheet": 0, "limit": 1}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["sheets"] == ["预算"]
    assert body["name"] == "预算"
    assert body["header"] == ["项目", "金额"]
    assert body["rows"] == [["服务器", "1200"]]
    assert body["truncated"] is True

    invalid = client.get(
        f"{PUBLIC}/assets/{created['asset_id']}/table", params={"sheet": 5}
    )
    assert invalid.status_code == 422


def test_pdf_text_and_page_count(
    owner_client: TestClient, db: DbSession, settings
) -> None:
    created = _upload(owner_client, "manual.pdf", pdf_bytes("Hello CtrlStore"), "application/pdf")
    run_until_idle(settings)

    version = _version(db, created["asset_id"])
    assert version.extract_status == EXTRACT_READY
    assert "Hello CtrlStore" in (version.text_extract or "")
    assert version.page_count == 1


def test_encrypted_pdf_is_skipped(
    owner_client: TestClient, db: DbSession, settings
) -> None:
    created = _upload(owner_client, "secret.pdf", encrypted_pdf_bytes(), "application/pdf")
    run_until_idle(settings)

    version = _version(db, created["asset_id"])
    assert version.extract_status == EXTRACT_SKIPPED
    assert version.text_extract in (None, "")
    assert _job(db, jobs.KIND_EXTRACT).status == "done"


def test_archive_with_too_many_entries_fails_without_retry(
    owner_client: TestClient, db: DbSession, settings
) -> None:
    """高压缩比/条目过多是确定性失败：记 failed，任务只尝试一次。"""
    created = _upload(
        owner_client,
        "bomb.docx",
        zip_with_many_entries(2001),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    run_until_idle(settings)

    version = _version(db, created["asset_id"])
    assert version.extract_status == EXTRACT_FAILED
    job = _job(db, jobs.KIND_EXTRACT)
    assert (job.status, job.attempts) == ("done", 1)


def test_markdown_body_is_extracted(
    owner_client: TestClient, db: DbSession, settings
) -> None:
    created = owner_client.post(
        f"{API}/assets",
        json={"kind": "markdown", "body": "# 标题\n\n萤火虫计划的正文说明"},
        headers=admin_headers(owner_client),
    ).json()

    run_until_idle(settings)

    version = _version(db, created["id"])
    assert version.extract_status == EXTRACT_READY
    assert "萤火虫计划" in (version.text_extract or "")


# —— 公开响应与首页图 ——


def test_public_payload_has_published_filename(
    owner_client: TestClient, client: TestClient, settings
) -> None:
    created = _upload(owner_client, "手册.pdf", pdf_bytes("下载用文件"), "application/pdf")
    run_until_idle(settings)
    _publish(owner_client, created["asset_id"])

    payload = client.get(f"{PUBLIC}/assets/{created['asset_id']}").json()

    assert payload["filename"] == "手册.pdf"
    assert payload["page_count"] == 1
    assert payload["extract_status"] == EXTRACT_READY
    assert payload["urls"]["original"].endswith("/original.pdf")


def test_pdf_first_page_becomes_public_poster(
    owner_client: TestClient,
    client: TestClient,
    db: DbSession,
    settings,
    storage: LocalStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_render(source, destination, **kwargs):
        from PIL import Image

        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (10, 10, 10)).save(destination, "JPEG")
        return True

    monkeypatch.setattr(pdf_text, "render_first_page", fake_render)
    created = _upload(owner_client, "scan.pdf", pdf_bytes("扫描件"), "application/pdf")
    run_until_idle(settings)
    _publish(owner_client, created["asset_id"])

    version = _version(db, created["asset_id"])
    poster = _derivative(db, version.id, "poster")
    assert poster is not None and poster.status == DERIVATIVE_READY
    assert storage.public_derivative_path(created["asset_id"], "poster").is_file()
    payload = client.get(f"{PUBLIC}/assets/{created['asset_id']}").json()
    assert payload["urls"]["poster"].endswith("/poster.jpg")


def test_asset_keeps_original_when_extraction_fails(
    owner_client: TestClient, db: DbSession, settings, storage: LocalStorage
) -> None:
    """抽取失败不影响原件：内容仍可读、可发布。"""
    created = _upload(
        owner_client,
        "broken.pdf",
        b"%PDF-1.4 not really a pdf",
        "application/pdf",
    )
    run_until_idle(settings)

    version = _version(db, created["asset_id"])
    assert version.extract_status in (EXTRACT_FAILED, EXTRACT_SKIPPED)
    assert storage.original_path(version.storage_key).is_file()
    assert owner_client.get(f"{API}/assets/{created['asset_id']}").status_code == 200


# —— 上限与截断（单元） ——


def test_text_builder_truncates() -> None:
    builder = TextBuilder(5)
    builder.add("123456789")

    text, truncated = builder.result()

    assert (text, truncated) == ("12345", True)


def test_docx_text_truncates_with_small_limit(tmp_path) -> None:
    path = tmp_path / "long.docx"
    path.write_bytes(docx_bytes("a" * 100))

    text, truncated = docx_text.extract(path, ExtractLimits(text_chars=10))

    assert len(text) == 10
    assert truncated is True


def test_archive_size_limit(tmp_path) -> None:
    path = tmp_path / "big.docx"
    path.write_bytes(docx_bytes("正文"))

    with pytest.raises(ExtractionFailed):
        check_archive(path, ExtractLimits(archive_bytes=10))


def test_archive_entry_limit(tmp_path) -> None:
    path = tmp_path / "many.docx"
    path.write_bytes(zip_with_many_entries(5))

    with pytest.raises(ExtractionFailed):
        check_archive(path, ExtractLimits(archive_entries=2))


def test_archive_compression_ratio_guard(tmp_path) -> None:
    """中央目录可以谎报大小；压缩比异常时直接拒绝。"""
    import zipfile

    path = tmp_path / "ratio.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"0" * 2_000_000)

    with pytest.raises(ExtractionFailed):
        check_archive(path)
