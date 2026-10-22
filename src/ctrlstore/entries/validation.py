"""HTTP 与 CLI 共用的文案校验，避免两种入口接受不同数据。"""

import re
import unicodedata

from ..errors import ApiError

KEY_PATTERN = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")
LANGUAGE_PATTERN = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")


def validate(fields: dict) -> dict:
    """不裁剪或转义值；限制长度、控制字符和可编辑字段的形态。"""
    result = dict(fields)
    if "key" in result:
        key = result["key"]
        if not isinstance(key, str) or len(key) > 128 or not KEY_PATTERN.fullmatch(key):
            raise ApiError.invalid("键须为 1–128 位小写字母、数字，以点或连字符分段")
    if "language" in result:
        language = result["language"]
        if (
            not isinstance(language, str)
            or len(language) > 32
            or not LANGUAGE_PATTERN.fullmatch(language)
        ):
            raise ApiError.invalid("语言须为 zh-CN、en 等语言标识，最长 32 字符")
    if "value" in result:
        value = result["value"]
        if not isinstance(value, str):
            raise ApiError.invalid("文案必须是纯文本")
        if len(value) > 2000:
            raise ApiError.too_large("文案最多 2000 个 Unicode 字符")
        if any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\t" for char in value):
            raise ApiError.invalid("文案只允许换行和制表符，不允许其他控制字符")
        if len(value.encode("utf-8")) > 8192:
            raise ApiError.too_large("文案 UTF-8 编码最多 8192 字节")
    if "visibility" in result and result["visibility"] not in {"public", "site"}:
        raise ApiError.invalid("文案可见性只能是 public 或 site")
    if any(result.get(name, "") is None for name in ("key", "language", "value", "visibility")):
        raise ApiError.invalid("键、语言、文字与可见性不能为空值")
    return result
