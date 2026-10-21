"""公开内容读取：单条、按别名、表格预览。

正文一律取**已发布版本**的文件，不是当前版本；图片先确认仍然已发布，
否则从 HTML 里去掉了事——文章照常可读（企划第 8.4 节 T4）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..assets import markdown, publish
from ..auth import deps as auth_deps
from ..content import markdown_render
from ..errors import ApiError
from ..models import ASSET_PUBLISHED, Asset
from ..storage import LocalStorage
from ..uploads.inspection import KIND_MARKDOWN, KIND_TEXT, KIND_XLSX
from . import deps, payloads

router = APIRouter(tags=["public"])


@router.get("/assets/{asset_id}")
def read_asset(
    asset_id: str,
    request: Request,
    format: str | None = Query(default=None, pattern="^(html|markdown)$"),
    db: DbSession = Depends(auth_deps.get_db),
    site=Depends(deps.site_from_request),
) -> Response:
    """单条内容。``format=markdown`` 只对站点令牌开放，匿名只给 HTML。"""
    asset = _readable_asset(db, asset_id, site=site)
    if format == "markdown" and site is None:
        raise ApiError.forbidden("原文只对站点令牌开放")
    payload = _detail_payload(request, db, asset, site=site, want_markdown=format == "markdown")
    return deps.json_response(request, site=site, payload=payload)


@router.get("/by-slug/{slug}")
def read_by_slug(
    slug: str,
    request: Request,
    format: str | None = Query(default=None, pattern="^(html|markdown)$"),
    db: DbSession = Depends(auth_deps.get_db),
    site=Depends(deps.site_from_request),
) -> Response:
    """按别名取文章或纯文本。

    只认**发布时**记下的别名（``published_slug``）：编辑中的 ``slug`` 改了也不影响
    现有地址，直到下一次发布。库层对已发布别名有唯一索引。
    """
    asset = db.scalar(
        select(Asset).where(Asset.published_slug == slug.strip(), Asset.status == ASSET_PUBLISHED)
    )
    if asset is None or not deps.can_read_asset(db, asset, site=site):
        raise ApiError.not_found("内容不存在")
    if format == "markdown" and site is None:
        raise ApiError.forbidden("原文只对站点令牌开放")
    payload = _detail_payload(request, db, asset, site=site, want_markdown=format == "markdown")
    return deps.json_response(request, site=site, payload=payload)


@router.get("/assets/{asset_id}/table")
def read_table(
    asset_id: str,
    request: Request,
    sheet: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: DbSession = Depends(auth_deps.get_db),
    site=Depends(deps.site_from_request),
) -> Response:
    """表格预览。响应形状先定下来，内容在第 9 部分填（企划第 8.5 节 D5）。"""
    asset = _readable_asset(db, asset_id, site=site)
    if asset.kind != KIND_XLSX:
        raise ApiError.invalid("只有 xlsx 有表格预览")

    payload = {
        "asset_id": asset.id,
        "sheets": [],
        "sheet": sheet,
        "limit": limit,
        "note": "表格预览在第 9 部分实现",
    }
    return deps.json_response(request, site=site, payload=payload)


# —— 内部 ——


def _readable_asset(db: DbSession, asset_id: str, *, site) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None or not deps.can_read_asset(db, asset, site=site):
        raise ApiError.not_found("内容不存在")
    return asset


def _detail_payload(
    request: Request, db: DbSession, asset: Asset, *, site, want_markdown: bool
) -> dict:
    """按类型组装响应。正文取已发布版本的文件内容。

    正文里要保留哪些图片，由**发布时记下的引用**决定，而不是草稿的引用表：
    编辑改正文不会改变线上 HTML。
    """
    cover = (
        db.get(Asset, asset.published_cover_asset_id) if asset.published_cover_asset_id else None
    )
    if cover is not None and not deps.can_read_asset(db, cover, site=site):
        cover = None

    body_html = None
    body_markdown = None
    if asset.kind == KIND_MARKDOWN:
        source = _published_body(request, asset)
        # front matter 是给编目用的元数据，不该当成正文出现在页面上；
        # 它已经进了 JSON 的 title / slug / summary / tags。
        source = markdown.parse(source).body
        if want_markdown:
            body_markdown = source
        else:
            images = _images_of_published_asset(db, asset, site=site)
            body_html = payloads.render_markdown(request, source, images=images, site=site).html
    elif asset.kind == KIND_TEXT:
        # 纯文本不走 Markdown：正文里的反引号、井号、方括号都是字面量。
        # 先按 HTML 转义再包进 <pre>，然后照常消毒（script 之类仍然被去掉）。
        source = _published_body(request, asset)
        if want_markdown:
            body_markdown = source
        else:
            body_html = markdown_render.render_plain_text(source)

    return payloads.asset_detail(
        request, asset, site=site, cover=cover, body_html=body_html, body_markdown=body_markdown
    )


def _published_body(request: Request, asset: Asset) -> str:
    """读已发布版本的文件。文件缺失时给 409，而不是半个页面。"""
    version = publish.published_version(asset)
    if version is None:
        raise ApiError.conflict("内容尚无已发布版本")
    storage = LocalStorage.from_settings(request.app.state.settings)
    path = storage.original_path(version.storage_key)
    if not path.is_file():
        raise ApiError.conflict("已发布版本的文件不存在")
    return path.read_text(encoding="utf-8", errors="replace")


def _images_of_published_asset(db: DbSession, asset: Asset, *, site) -> dict[str, Asset]:
    """发布版本正文引用的图片，且此刻仍可读。

    只认发布快照（``published_references``）：草稿里增删图片不会改变线上正文。
    图片后来被撤回时，这里取不到，渲染阶段会把那张图去掉。
    """
    ids = payloads.published_reference_ids(asset)
    if not ids:
        return {}
    rows = db.scalars(select(Asset).where(Asset.id.in_(ids), Asset.kind == "image")).all()
    return {image.id: image for image in rows if deps.can_read_asset(db, image, site=site)}
