"""Markdown 的 front matter 与正文图片解析。

第 4 部分只做两件事：把 YAML front matter 映射到内容字段，以及把正文里的
**图片**地址解析成内容 ID。渲染与消毒是第 6 部分的事——这里不产生 HTML。

图片的写法（企划第 8.4 节 T4）：

* ``![说明](/p/<uuid>/original.jpg)``，或带域名的完整地址；
* ``![说明](asset:<uuid>)``，便于草稿阶段先占位；
* 直接写在 HTML 里的 ``<img src="…">``。

只认图片：``[手册](/p/<uuid>/original.txt)`` 是下载链接，不受「图片必须已发布」约束。
代码块与行内代码里的同样文字不算引用。
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

#: 围栏代码块：解析引用前先摘掉，避免把示例文字当成真实引用。
_FENCED_CODE = re.compile(r"^(```|~~~).*?^\1\s*$", re.DOTALL | re.MULTILINE)
#: 行内代码。
_INLINE_CODE = re.compile(r"`[^`\n]*`")
#: Markdown 图片语法：``![说明](地址)``。
_IMAGE_SYNTAX = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")
#: HTML 里的 img 标签。
_HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc=\"([^\"]*)\"", re.IGNORECASE)


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


def image_ids(body: str) -> list[str]:
    """正文里作为**图片**引用的内容 ID，按出现顺序去重。

    只解析图片语法与 ``<img src>``：写作 ``[手册](/p/<id>/original.txt)`` 的下载
    链接不会被算进来，因此不会因为「目标不是图片」被挡在发布之外。
    代码块与行内代码先摘掉，里面的示例文字不算引用。
    """
    text = _INLINE_CODE.sub("", _FENCED_CODE.sub("", body))
    found: dict[str, None] = {}
    for pattern in (_IMAGE_SYNTAX, _HTML_IMAGE):
        for match in pattern.finditer(text):
            asset_id = _id_in_url(match.group(1))
            if asset_id is not None:
                found.setdefault(asset_id, None)
    return list(found)


def _id_in_url(url: str) -> str | None:
    """从一个图片地址里取内容 ID；外站地址返回 ``None``。"""
    for pattern in (_ASSET_SCHEME, _PUBLIC_PATH):
        match = pattern.search(url)
        if match:
            return match.group(1).lower()
    return None


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
