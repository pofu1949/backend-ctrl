"""存储键。

键只由服务端生成，形如 ``aa/<asset_id>/v1/original.jpg``：

* 前两段取内容 ID 的前两位，避免单个目录堆积过多文件；
* 第三段是版本号，版本文件写完就不再改；
* 最后一段固定 ``original`` 加一个扩展名，扩展名与真实类型一致。

用户输入永远不进入键。读取用 :func:`validate` 再挡一次，防止历史数据或人为写入
带进 ``..``、反斜杠和绝对路径。
"""

from __future__ import annotations

import re

#: 允许的键形状。故意写死：换布局必须改这里和迁移。
KEY_PATTERN = re.compile(r"^[0-9a-z]{2}/[0-9a-z-]{36}/v[1-9][0-9]*/original\.[a-z0-9]{2,5}$")

#: 键的最大长度，与数据库列宽一致。
MAX_KEY_LENGTH = 255

ALLOWED_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class InvalidStorageKey(ValueError):
    """键不合法。调用方应把它当成内部错误，而不是用户输入错误。"""


def build(asset_id: str, version_no: int, extension: str) -> str:
    """生成存储键。``extension`` 不带点。"""
    if version_no < 1:
        raise InvalidStorageKey("版本号从 1 起")
    suffix = extension.lower().lstrip(".")
    if not suffix.isalnum():
        raise InvalidStorageKey(f"扩展名不合法：{extension}")
    return f"{asset_id[:2]}/{asset_id}/v{version_no}/original.{suffix}"


def validate(key: str) -> str:
    """校验并原样返回键；不合法就抛 :class:`InvalidStorageKey`。"""
    if not key or len(key) > MAX_KEY_LENGTH:
        raise InvalidStorageKey("存储键为空或过长")
    if ".." in key or "\\" in key or key.startswith("/") or "\x00" in key:
        raise InvalidStorageKey("存储键含非法字符")
    segments = key.split("/")
    if len(segments) != 4 or any(not segment for segment in segments):
        raise InvalidStorageKey("存储键段数不正确")
    if not all(ALLOWED_SEGMENT.match(segment) for segment in segments):
        raise InvalidStorageKey("存储键含非法字符")
    if not KEY_PATTERN.match(key):
        raise InvalidStorageKey("存储键形状不正确")
    return key
