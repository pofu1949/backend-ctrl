"""区块正文校验。HTTP 与以后的命令行共用，避免两种入口接受不同形状。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..errors import ApiError
from .template import TEMPLATES, FieldSpec, Template

SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")
LANGUAGE_PATTERN = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")
ASSET_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

#: 正文上限。模板本身已经限制了条数和字数，这里再挡住异常大的请求。
MAX_BODY_BYTES = 65_536


def get_template(template_id: str) -> Template:
    found = TEMPLATES.get(template_id)
    if found is None:
        raise ApiError.invalid("未知的区块模板")
    return found


def validate_meta(fields: dict[str, Any]) -> dict[str, Any]:
    """校验别名、语言、标题、可见性和模板名。不改正文。"""
    result = dict(fields)
    if "slug" in result:
        slug = result["slug"]
        if not isinstance(slug, str) or len(slug) > 128 or not SLUG_PATTERN.fullmatch(slug):
            raise ApiError.invalid("别名须为 1–128 位小写字母、数字，以点或连字符分段")
    if "language" in result:
        language = result["language"]
        if (
            not isinstance(language, str)
            or len(language) > 32
            or not LANGUAGE_PATTERN.fullmatch(language)
        ):
            raise ApiError.invalid("语言须为 zh-CN、en 等语言标识，最长 32 字符")
    if "title" in result:
        title = result["title"]
        if not isinstance(title, str) or len(title) > 255:
            raise ApiError.invalid("区块名称最长 255 个字符")
        _reject_controls(title, "区块名称", newlines=False)
    if "visibility" in result and result["visibility"] not in {"public", "site"}:
        raise ApiError.invalid("区块可见性只能是 public 或 site")
    if "template" in result:
        get_template(str(result["template"]))
    return result


def empty_body(template_id: str) -> dict[str, Any]:
    return _empty(get_template(template_id).fields)


def normalize(template_id: str, body: Any, *, strict: bool) -> dict[str, Any]:
    """把正文收成模板规定的形状。

    ``strict`` 只在发布时打开：草稿允许主标题还空着，发布则不行。
    """
    if not isinstance(body, dict):
        raise ApiError.invalid("区块正文必须是对象")
    return _object(get_template(template_id).fields, body, strict=strict)


def asset_ids(template_id: str, body: dict[str, Any]) -> list[str]:
    """按出现顺序收集图片、视频的内容 ID。同一张图可以在不同槽位各出现一次。"""
    found: list[str] = []
    _walk(get_template(template_id).fields, body, found)
    return found


def _object(fields: tuple[FieldSpec, ...], body: dict[str, Any], *, strict: bool) -> dict[str, Any]:
    unknown = sorted(set(body) - {field.key for field in fields})
    if unknown:
        raise ApiError.invalid("区块包含未知字段：" + "、".join(unknown))
    result: dict[str, Any] = {}
    for field in fields:
        present = body[field.key] if field.key in body else _default(field)
        result[field.key] = _value(field, present, strict=strict)
    return result


def _value(field: FieldSpec, value: Any, *, strict: bool) -> Any:
    if field.type in {"text", "longtext", "link"}:
        return _text(field, value, strict=strict)
    if field.type == "assets":
        return _assets(field, value)
    if field.type == "repeat":
        return _repeat(field, value, strict=strict)
    raise ApiError.invalid(f"模板槽位 {field.key} 的类型不受支持")


def _text(field: FieldSpec, value: Any, *, strict: bool) -> str:
    if not isinstance(value, str):
        raise ApiError.invalid(f"{field.label}必须是纯文本")
    if len(value) > field.max_length:
        raise ApiError.too_large(f"{field.label}最多 {field.max_length} 个字符")
    _reject_controls(value, field.label, newlines=field.type == "longtext")
    if field.type == "link":
        _link(value)
    if len(value.encode("utf-8")) > field.max_length * 4:
        raise ApiError.too_large(f"{field.label}过长")
    if strict and field.required and not value.strip():
        raise ApiError.invalid(f"请填写{field.label}")
    return value


def _link(value: str) -> None:
    if value == "":
        return
    internal = value.startswith("/") and not value.startswith("//")
    external = value.startswith("https://") or value.startswith("http://")
    if not internal and not external:
        raise ApiError.invalid("按钮链接只能是站内路径（以 / 开头）或 http(s) 地址")


def _assets(field: FieldSpec, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ApiError.invalid(f"{field.label}必须是内容 ID 列表")
    if len(value) > field.max_items:
        raise ApiError.too_large(f"{field.label}最多 {field.max_items} 个")
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not ASSET_ID_PATTERN.fullmatch(item):
            raise ApiError.invalid(f"{field.label}的每一项都必须是内容 ID")
        if item in seen:
            raise ApiError.invalid(f"{field.label}里不能重复选择同一内容")
        seen.add(item)
        result.append(item)
    return result


def _repeat(field: FieldSpec, value: Any, *, strict: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ApiError.invalid(f"{field.label}必须是列表")
    if len(value) > field.max_items:
        raise ApiError.too_large(f"{field.label}最多 {field.max_items} 项")
    result = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ApiError.invalid(f"{field.label}的第 {index} 项格式不正确")
        try:
            result.append(_object(field.fields, item, strict=strict))
        except ApiError as exc:
            raise ApiError(
                exc.status_code, exc.code, f"{field.label}第 {index} 项：{exc.message}"
            ) from exc
    return result


def _default(field: FieldSpec) -> Any:
    if field.type in {"assets", "repeat"}:
        return []
    return ""


def _empty(fields: tuple[FieldSpec, ...]) -> dict[str, Any]:
    return {field.key: _default(field) for field in fields}


def _walk(fields: tuple[FieldSpec, ...], body: Any, found: list[str]) -> None:
    if not isinstance(body, dict):
        return
    for field in fields:
        value = body.get(field.key)
        if field.type == "assets" and isinstance(value, list):
            found.extend(item for item in value if isinstance(item, str))
        elif field.type == "repeat" and isinstance(value, list):
            for item in value:
                _walk(field.fields, item, found)


def _reject_controls(value: str, label: str, *, newlines: bool) -> None:
    allowed = "\n\t" if newlines else ""
    if any(unicodedata.category(char) in {"Cc", "Cs"} and char not in allowed for char in value):
        message = f"{label}只允许换行和制表符" if newlines else f"{label}不能包含换行或控制字符"
        raise ApiError.invalid(message)
