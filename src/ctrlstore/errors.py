"""统一错误响应。

对外格式固定为 ``{"error": {"code": "...", "message": "..."}}``（企划第 13 章）。
``code`` 稳定供程序判断，``message`` 是给人看的中文。

业务层（``auth.service``）也抛 :class:`ApiError`：命令行只需读 ``message``，
HTTP 层把它映射成状态码，两边共用一套错误语义，不各写一份。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

#: 稳定的错误码。
UNAUTHORIZED = "unauthorized"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
CONFLICT = "conflict"
INVALID = "invalid"
RATE_LIMITED = "rate_limited"
TOO_LARGE = "too_large"
INSUFFICIENT_STORAGE = "insufficient_storage"


class ApiError(Exception):
    """带稳定错误码的失败。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    @classmethod
    def unauthorized(cls, message: str = "请先登录") -> ApiError:
        return cls(401, UNAUTHORIZED, message)

    @classmethod
    def forbidden(cls, message: str = "没有执行该操作的权限") -> ApiError:
        return cls(403, FORBIDDEN, message)

    @classmethod
    def not_found(cls, message: str = "对象不存在") -> ApiError:
        return cls(404, NOT_FOUND, message)

    @classmethod
    def conflict(cls, message: str) -> ApiError:
        return cls(409, CONFLICT, message)

    @classmethod
    def invalid(cls, message: str) -> ApiError:
        return cls(422, INVALID, message)

    @classmethod
    def rate_limited(cls, message: str = "尝试过于频繁，请稍后再试") -> ApiError:
        return cls(429, RATE_LIMITED, message)


def register_error_handlers(app: FastAPI) -> None:
    """把所有失败都收成同一种结构。

    包括框架自己产生的两类：路由没匹配上（404/405）和请求体校验失败（422）。
    否则客户端要同时处理 ``{"error": ...}`` 和 FastAPI 默认的 ``{"detail": ...}``。
    """

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers={"Cache-Control": "no-store"} if exc.status_code == 401 else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": INVALID, "message": _describe_validation_error(exc)}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODE_BY_STATUS.get(exc.status_code, INVALID)
        message = str(exc.detail) if exc.detail else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message}},
            headers=getattr(exc, "headers", None),
        )


#: HTTP 状态码到稳定错误码的映射。未列出的按 ``invalid`` 处理。
_CODE_BY_STATUS = {
    400: INVALID,
    401: UNAUTHORIZED,
    403: FORBIDDEN,
    404: NOT_FOUND,
    405: INVALID,
    409: CONFLICT,
    413: TOO_LARGE,
    429: RATE_LIMITED,
}


def _describe_validation_error(exc: RequestValidationError) -> str:
    """把 pydantic 的报错压成一行中文，避免把内部结构整段吐给客户端。"""
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"] if item != "body")
        parts.append(f"{location or '请求体'}: {error['msg']}")
    return "；".join(parts) or "请求格式不正确"
