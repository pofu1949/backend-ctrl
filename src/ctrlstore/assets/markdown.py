"""Markdown 的 front matter 与正文引用解析。

第 4 部分只做两件事：把 YAML front matter 映射到内容字段，以及把正文里的
本库图片地址解析成内容 ID。渲染与消毒是第 6 部分的事——这里不产生 HTML。

引用的写法（企划第 8.4 节 T4）：
* ``/p/<uuid>/original.jpg``，或带域名的完整地址；
* ``asset:<uuid>``，便于草稿阶段先占位。
外站图片与外链不在本库范围内，第 6 部分渲染时会被去掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import frontmatter

#: 内容 ID 的形态（UUID）。
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

#: 公开原件地址里的内容 ID。
_PUBLIC_PATH = re.compile(rf"/p/({_UUID})/")
#: 草稿阶段用的占位写法。
_ASSET_SCHEME = re.compile(rf"asset:({_UUID})")


@dataclass(frozen=True)
class Parsed:
    """front matter 的映射结果。字段都可能是 None：以调用方给的值为准。"""

    body: str = ""
    title: str | None = None
    slug: str | None = None
    summary: str | None = None
    language: str | None = None
    cover: str | None = None
    tags: list[str] = field(default_factory=list)


def parse(text: str) -> Parsed:
    """解析 front matter。没有 front matter 时原样返回正文。"""
    document = frontmatter.loads(text)
    metadata = document.metadata or {}
    return Parsed(
        body=document.content,
        title=_text(metadata.get("title")),
        slug=_text(metadata.get("slug")),
        summary=_text(metadata.get("excerpt")) or _text(metadata.get("summary")),
        language=_text(metadata.get("language") or metadata.get("lang")),
        cover=_text(metadata.get("cover")),
        tags=_tag_list(metadata.get("tags")),
    )


def referenced_ids(body: str) -> list[str]:
    """正文中引用的内容 ID，按出现顺序去重。"""
    found: dict[str, None] = {}
    for pattern in (_PUBLIC_PATH, _ASSET_SCHEME):
        for match in pattern.finditer(body):
            found.setdefault(match.group(1).lower(), None)
    return list(found)


def cover_id(value: str | None) -> str | None:
    """封面的写法可以是内容 ID、公开地址或 ``asset:`` 占位。"""
    if not value:
        return None
    stripped = value.strip()
    if re.fullmatch(_UUID, stripped):
        return stripped.lower()
    for pattern in (_PUBLIC_PATH, _ASSET_SCHEME):
        match = pattern.search(stripped)
        if match:
            return match.group(1).lower()
    return None


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _tag_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，、\s]+", value)
    elif isinstance(value, list | tuple):
        parts = [str(item) for item in value]
    else:
        return []
    return [item.strip() for item in parts if item and item.strip()]
