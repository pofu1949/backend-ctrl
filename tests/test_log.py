"""日志格式与脱敏（企划第 17.5 节）。"""

from __future__ import annotations

import json
import logging

from ctrlstore.log import JsonFormatter, configure_logging


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="ctrlstore.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="已处理 %s",
        args=("上传",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _parse(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter("ctrlstore").format(record))


def test_payload_has_expected_core_fields() -> None:
    payload = _parse(_record(asset_id="abc"))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "ctrlstore.test"
    assert payload["service"] == "ctrlstore"
    assert payload["message"] == "已处理 上传"
    assert payload["asset_id"] == "abc"
    assert payload["ts"].endswith("+00:00")


def test_secret_like_fields_are_dropped() -> None:
    record = _record(
        password="p",
        session_token="t",
        Authorization="Bearer x",
        cookie="c",
        request_body="{}",
        csrf_token="z",
    )

    payload = _parse(record)

    for key in (
        "password",
        "session_token",
        "Authorization",
        "cookie",
        "request_body",
        "csrf_token",
    ):
        assert key not in payload


def test_non_ascii_is_not_escaped() -> None:
    assert "上传" in JsonFormatter("ctrlstore").format(_record())


def test_configure_logging_replaces_handlers() -> None:
    root = logging.getLogger()
    configure_logging("DEBUG")
    configure_logging("WARNING")

    assert len(root.handlers) == 1
    assert root.level == logging.WARNING
