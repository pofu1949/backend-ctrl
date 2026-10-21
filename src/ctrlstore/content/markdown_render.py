"""Markdown 渲染与 HTML 消毒（企划第 8.4 节 T3、T4）。

两条硬规则：

* **文内图片只能来自本库**。外站地址、``data:``、相对上级路径一律去掉；
  引用的图片未发布时，这张图也从 HTML 里去掉——文章照常发布。
* **消毒结果为准**。脚本、内联事件、``javascript:`` 链接都清掉；
  允许的标签与属性写在下面的白名单里，新增标签要改这里。

图片地址输出绝对地址（``public_base_url`` 拼出来），这样网站无论部署在哪个域名下
都不依赖相对路径解析。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape as html_escape
from html import unescape as html_unescape

import markdown as markdown_lib
import nh3

#: 允许保留的标签：排版够用，不包含 iframe、script、style、form。
ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}

#: 每个标签允许的属性。图片只给 src/alt/尺寸，链接只给 href/title。
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "width", "height"},
    "td": {"align", "colspan", "rowspan"},
    "th": {"align", "colspan", "rowspan"},
    "code": {"class"},
}

#: 允许的链接协议。图片在消毒之后还要再过一道「必须指向 /p/」的检查。
ALLOWED_SCHEMES = {"http", "https", "mailto"}

#: Markdown 的图片语法与 HTML 里的 img 标签。
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC = re.compile(r'\bsrc="([^"]*)"', re.IGNORECASE)
#: 正文里指向本库公开地址的下载链接。
_HREF = re.compile(r'href="(/p/[^"]*)"', re.IGNORECASE)
_ASSET_SCHEME = re.compile(r"^asset:([0-9a-fA-F-]{36})$")
#: 本库公开地址，兼容绝对与相对两种写法。
_PUBLIC_PATH = re.compile(r"^(?:https?://[^/]+)?/p/([0-9a-fA-F-]{36})/")


@dataclass(frozen=True)
class Rendered:
    """渲染结果。``asset_ids`` 是最终出现在 HTML 里的本库内容。"""

    html: str
    asset_ids: list[str] = field(default_factory=list)


def render(
    body: str, *, image_url: Callable[[str], str | None], base_url: str | None = None
) -> Rendered:
    """把 Markdown 渲染成消毒后的 HTML。

    ``image_url`` 负责把内容 ID 变成公开地址；图片不可用（未发布、已撤回、
    不是图片）时返回 ``None``，那张图会从 HTML 里去掉。

    允许的写法都会收敛到同一个地址：``asset:<id>``、``/p/<id>/...``（含作者写的
    带域名的完整地址）。写法不同、行为不同是上一版的缺陷。

    ``base_url`` 有值时，正文里指向 ``/p/`` 的下载链接也补成绝对地址——网站可能
    不在同一个域名下，相对路径会打到网站自己身上。
    """
    allowed: dict[str, str] = {}

    def resolve(asset_id: str) -> str | None:
        url = image_url(asset_id)
        if url:
            allowed[url] = asset_id
        return url

    html = markdown_lib.markdown(body, extensions=["extra", "sane_lists", "fenced_code"])
    html = _rewrite_image_sources(html, resolve)
    if base_url:
        html = _absolutise_links(html, base_url.rstrip("/"))
    cleaned = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )
    return _filter_images(cleaned, allowed)


def render_plain_text(text: str) -> str:
    """纯文本 → ``<pre>`` 包住的 HTML。**不经过 Markdown 解析。**

    走 Markdown 会让正文里的反引号、``#``、``[x](y)`` 被当成语法：三个反引号
    提前结束围栏，后面的文字就被当成 Markdown 渲染了。这里先做 HTML 转义，
    再照常过一遍消毒，保证和 Markdown 输出同一套规则。
    """
    escaped = html_escape(text)
    return nh3.clean(
        f"<pre>{escaped}</pre>",
        tags={"pre"},
        attributes={},
        url_schemes=set(),
        strip_comments=True,
    )


def excerpt(body: str, limit: int = 160) -> str:
    """摘要：去掉 Markdown 记号后取前若干字。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*_`~\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _rewrite_image_sources(html: str, resolve: Callable[[str], str | None]) -> str:
    """把图片地址统一换成公开地址。

    认两种写法：``asset:<id>``（nh3 不认这个协议，必须先换掉）与
    ``/p/<id>/...``（含带域名的完整地址）。认不出内容 ID 的图片留着原样，
    后面按"不是本库地址"整张去掉。
    """

    def replace(match: re.Match[str]) -> str:
        value = match.group(1).strip()
        asset_id = _asset_id_of(value)
        if asset_id is None:
            return match.group(0)
        url = resolve(asset_id)
        return f'src="{url}"' if url else 'src="#unavailable"'

    def replace_tag(tag: re.Match[str]) -> str:
        return _SRC.sub(replace, tag.group(0))

    return _IMG_TAG.sub(replace_tag, html)


def _asset_id_of(url: str) -> str | None:
    """从图片地址里取内容 ID；不是本库地址就返回 ``None``。"""
    scheme = _ASSET_SCHEME.match(url)
    if scheme is not None:
        return scheme.group(1).lower()
    path = _PUBLIC_PATH.match(url)
    if path is not None:
        return path.group(1).lower()
    return None


def _absolutise_links(html: str, base_url: str) -> str:
    """把 ``href="/p/…"`` 补成绝对地址。图片在解析阶段就已经是绝对地址了。"""

    def replace(match: re.Match[str]) -> str:
        return f'href="{base_url}{match.group(1)}"'

    return _HREF.sub(replace, html)


def _filter_images(html: str, allowed: dict[str, str]) -> Rendered:
    """消毒之后只保留解析成功的图片。

    ``allowed`` 是本次渲染真正产出的地址表：解析失败的图（外站、非图片、
    已撤回）不会出现在里面，因此这里整张去掉。

    比对前先还原 HTML 实体：消毒会把地址里的 ``&`` 写成 ``&amp;``，签名地址一定
    带查询串，按整段字符串直接比会把还该保留的图片删掉。
    """
    kept: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        source = _SRC.search(tag)
        if source is None:
            return ""
        asset_id = allowed.get(html_unescape(source.group(1)))
        if asset_id is None:
            return ""
        kept.append(asset_id)
        return tag

    filtered = _IMG_TAG.sub(replace, html)
    # 图片被去掉后常留下空段落，顺手清掉，输出更干净。
    filtered = re.sub(r"<p>\s*</p>", "", filtered)
    return Rendered(html=filtered, asset_ids=sorted(set(kept)))
