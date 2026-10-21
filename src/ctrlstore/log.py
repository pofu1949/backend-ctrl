"""结构化日志。

日志以 JSON 行输出到 stdout，由 systemd 收进 journald（企划第 17.5 节）。

两条硬规则：

* 不记录密码、令牌、Cookie 和文件正文。字段名命中黑名单时整条记录被丢弃，
  以免调用方无意中把机密塞进 ``extra``。
* 不把异常里的请求体渲染出来；异常只保留类型与消息。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

#: 命中这些关键字（不区分大小写）的字段不会出现在日志里。
FORBIDDEN_FIELD_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "csrf",
    "body",
)

#: ``LogRecord`` 自带字段，其余视为调用方附加的结构化字段。
RESERVED_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """把一条日志渲染成单行 JSON。"""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in RESERVED_FIELDS or key.startswith("_"):
                continue
            if _is_forbidden(key):
                continue
            payload[key] = _serialisable(value)
        if record.exc_info:
            payload["error"] = _describe_exception(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _is_forbidden(field: str) -> bool:
    lowered = field.lower()
    return any(part in lowered for part in FORBIDDEN_FIELD_PARTS)


def _serialisable(value: Any) -> Any:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)


def _describe_exception(exc_info: tuple[type[BaseException], BaseException, Any]) -> dict[str, str]:
    exc_type, exc, _ = exc_info
    return {"type": exc_type.__name__, "message": str(exc)}


def ensure_utf8_stdout() -> None:
    """把标准输出与错误流改成 UTF-8。

    Debian 的 locale 本来就是 UTF-8；Windows 控制台默认跟随本地代码页（如 cp936），
    中文命令输出会变成乱码。这里统一一次，命令行与日志共用。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def configure_logging(level: str = "INFO", *, service: str = "ctrlstore") -> None:
    """安装唯一一个 stdout 处理器。可重复调用，不会叠加处理器。"""
    # 每行 JSON 必须是 UTF-8，否则 journald 与日志采集器会把它当成二进制。
    ensure_utf8_stdout()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter(service))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(level.upper())
