"""公开响应的形状（企划第 12.2、12.3 节）。

**只读发布快照**：``published_title``、``published_summary``、``published_tags``、
``published_cover_asset_id``、``published_slug``、``published_references`` 与已发布版本的正文。
编辑中的字段一个都不出现——这是「线上只随发布变化」的落点。

地址也按发布状态给：只有真正进了 ``public/`` 的内容才有 ``urls.original``；
「仅站点」内容不出现 ``/p/`` 地址，改由持有授权的站点令牌拿到短时签名地址。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request

from ..assets import publish, signing
from ..content import markdown_render
from ..models import (
    VISIBILITY_SITE,
    Asset,
    AssetVersion,
    Collection,
    CollectionItem,
    utcnow,
)
from .deps import absolute_url


def original_url(request: Request, asset: Asset, version: AssetVersion) -> str:
    """原图 / 原件的公开地址。扩展名取该版本的真实类型。"""
    extension = version.storage_key.rsplit(".", 1)[-1]
    return absolute_url(request, f"/p/{asset.id}/original.{extension}")


def signed_url(request: Request, asset: Asset, version: AssetVersion) -> str:
    """短时签名地址。绑定内容 + 已发布版本 + 过期时间，撤回后自动失效。"""
    settings = request.app.state.settings
    signed = signing.sign(
        settings.pageview_hmac_secret,
        asset_id=asset.id,
        version_id=version.id,
        ttl=signing.DEFAULT_TTL,
    )
    query = "&".join(f"{key}={value}" for key, value in signed.query().items())
    return absolute_url(request, f"/api/public/v1/files/{asset.id}?{query}")


def file_urls(request: Request, asset: Asset, *, site) -> dict[str, str]:
    """一条内容能给出的二进制地址。

    * 进了 ``/p/`` 的（已发布 + public/unlisted）：给原图地址。
    * 「仅站点」且调用方是该站点的令牌：给短时签名地址。
    * 其余（含私有、草稿）：什么都不给，避免发一个必然 404 的地址。
    """
    version = publish.published_version(asset)
    if version is None:
        return {}
    if publish.desired_links(asset) is not None:
        return {"original": original_url(request, asset, version)}
    if site is not None and asset.visibility == VISIBILITY_SITE:
        return {"signed": signed_url(request, asset, version)}
    return {}


def asset_summary(request: Request, asset: Asset, *, site) -> dict[str, Any]:
    """集合条目里用的短形态。"""
    version = publish.published_version(asset)
    if version is None:
        return {"id": asset.id, "kind": asset.kind, "title": asset.published_title or ""}

    payload: dict[str, Any] = {
        "id": asset.id,
        "kind": asset.kind,
        "title": asset.published_title or "",
        "mime": version.mime,
    }
    urls = file_urls(request, asset, site=site)
    if urls:
        payload["urls"] = urls
    if version.width is not None:
        payload["width"] = version.width
    if version.height is not None:
        payload["height"] = version.height
    if asset.published_slug:
        payload["slug"] = asset.published_slug
    return payload


def asset_detail(
    request: Request,
    asset: Asset,
    *,
    site,
    cover: Asset | None,
    body_html: str | None,
    body_markdown: str | None = None,
) -> dict[str, Any]:
    """单条内容的完整形态：文章用这里。"""
    payload: dict[str, Any] = {
        "id": asset.id,
        "kind": asset.kind,
        "slug": asset.published_slug,
        "title": asset.published_title or "",
        "summary": asset.published_summary or "",
        "updated_at": _timestamp(asset.published_at or asset.updated_at),
        "tags": _published_tags(asset),
    }
    urls = file_urls(request, asset, site=site)
    if urls:
        payload["urls"] = urls

    if cover is not None:
        cover_urls = file_urls(request, cover, site=site)
        if cover_urls:
            payload["cover"] = {"id": cover.id, "urls": cover_urls}

    if body_html is not None:
        payload["body"] = {"format": "html", "html": body_html}
    elif body_markdown is not None:
        payload["body"] = {"format": "markdown", "markdown": body_markdown}
    return payload


def collection_payload(
    request: Request,
    collection: Collection,
    items: list[CollectionItem],
    *,
    assets: list[Asset],
    site,
) -> dict[str, Any]:
    """集合响应。位置从 0 起，只列本次真正返回的条目。

    ``updated_at`` 只由**可见条目**的发布时间决定：看不见的内容被改动，
    不该让公开缓存失效，也不该让匿名调用方看出「还有人在改别的东西」。
    """
    asset_of = {asset.id: asset for asset in assets}
    entries: list[dict[str, Any]] = []
    stamps = []
    for item in items:
        asset = asset_of.get(item.asset_id)
        if asset is None:
            continue
        entry: dict[str, Any] = {
            "position": item.position,
            "asset": asset_summary(request, asset, site=site),
        }
        if item.caption:
            entry["caption"] = item.caption
        entries.append(entry)
        stamps.append(asset.published_at or asset.updated_at)

    return {
        "slug": collection.slug,
        "title": collection.title,
        "updated_at": _timestamp(max(stamps) if stamps else None),
        "items": entries,
    }


def render_markdown(
    request: Request, body: str, *, images: dict[str, Asset], site
) -> markdown_render.Rendered:
    """渲染正文。图片只能来自本库且此刻可读，其余整张去掉。"""

    def image_url(asset_id: str) -> str | None:
        image = images.get(asset_id)
        if image is None:
            return None
        version = publish.published_version(image)
        if version is None:
            return None
        urls = file_urls(request, image, site=site)
        return urls.get("original") or urls.get("signed")

    return markdown_render.render(
        body,
        image_url=image_url,
        base_url=request.app.state.settings.public_base_url,
    )


def published_reference_ids(asset: Asset) -> set[str]:
    """发布版本正文引用的图片 ID。草稿里的增删不会出现在这里。"""
    if not asset.published_references:
        return set()
    try:
        values = json.loads(asset.published_references)
    except ValueError:
        return set()
    return {str(value) for value in values}


def _published_tags(asset: Asset) -> list[str]:
    if not asset.published_tags:
        return []
    try:
        values = json.loads(asset.published_tags)
    except ValueError:
        return []
    return [str(value) for value in values]


def _timestamp(value) -> str:
    moment = value or utcnow()
    return moment.isoformat(timespec="seconds") + "Z"
