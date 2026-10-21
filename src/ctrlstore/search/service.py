"""全文索引与检索（企划第 8.7 节）。

索引是 SQLite 的 FTS5 虚拟表，用 **trigram** 分词：三字及以上按子串匹配，
中文不需要另装分词插件。一条内容最多两行：

* ``admin``     —— 编辑中的标题、文件名、标签与**当前版本**正文；
* ``published`` —— 发布快照（标题、别名、标签）与**已发布版本**正文。

站点搜索只读 ``published`` 行，并且之后还要过一遍 ``can_read_asset``：
索引里出现过的内容，不等于这个站点能读。待发布的替换与草稿不会进入
``published`` 行，所以站点永远搜不到它们。

一两个字不走 trigram（它匹配不了），只查标题、文件名、标签，最多 50 条（企划 M11）。
"""

from __future__ import annotations

import json

from sqlalchemy import column, delete, insert, select, table, text
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiError
from ..models import ASSET_PUBLISHED, ASSET_TRASHED, Asset, AssetVersion

SCOPE_ADMIN = "admin"
SCOPE_PUBLISHED = "published"

#: 短查询的阈值：少于 3 个字只查标题/文件名/标签。
SHORT_QUERY_LENGTH = 3

#: 两种查询最多返回多少条（企划 S2/M11）。
MAX_RESULTS = 50

#: 查询串长度上限，与路由一致。
MAX_QUERY_LENGTH = 128

#: FTS 表。它不是 ORM 模型：写入与删除都按 ``asset_id + scope`` 整体替换。
ASSET_SEARCH = table(
    "asset_search",
    column("asset_id"),
    column("scope"),
    column("title"),
    column("keywords"),
    column("body"),
)


def refresh(db: DbSession, asset: Asset) -> None:
    """按当前状态重建该资产的两行索引。调用方负责提交。

    内容、标签、版本或发布状态变化后都要调用；整体替换而不是增量更新，
    避免漏掉某个字段的旧值。
    """
    rows = [_admin_row(asset)]
    published = _published_row(asset)
    if published is not None:
        rows.append(published)

    db.execute(delete(ASSET_SEARCH).where(ASSET_SEARCH.c.asset_id == asset.id))
    db.execute(insert(ASSET_SEARCH), rows)


def remove(db: DbSession, asset_id: str) -> None:
    """彻底删除内容时清掉它的索引行。"""
    db.execute(delete(ASSET_SEARCH).where(ASSET_SEARCH.c.asset_id == asset_id))


def admin(db: DbSession, *, query: str, limit: int = MAX_RESULTS) -> tuple[str, list[Asset]]:
    """管理端搜索：返回 (模式, 内容)。回收站在取前 50 条之前就排除。"""
    term = clean_query(query)
    limit = _clamp(limit)
    mode, ids = _search(
        db, term, scope=SCOPE_ADMIN, limit=limit, where_sql="assets.status != 'trashed'"
    )
    assets = _load(db, ids, status_filter=ASSET_TRASHED, invert=True)
    return mode, assets


def published(
    db: DbSession, *, query: str, site_id: str, limit: int = MAX_RESULTS
) -> tuple[str, list[Asset]]:
    """站点搜索：只取该站点能读的已发布内容，再截到 50 条。

    可见性在 SQL 里先收窄：公开可读；仅站点要求出现在该站已授权的集合里；
    未列出、私有、草稿都不出现在发现型结果里。这样前面的私有命中不会挤掉
    后面的公开内容（企划第 8.7 节）。
    """
    term = clean_query(query)
    limit = _clamp(limit)
    mode, ids = _search(
        db,
        term,
        scope=SCOPE_PUBLISHED,
        limit=limit,
        where_sql=_published_where(),
        params={"site_id": site_id},
    )
    assets = _load(db, ids, status_filter=ASSET_PUBLISHED, invert=False)
    return mode, assets


def clean_query(query: str) -> str:
    """规整查询串。空串或控制字符直接拒绝：这类输入没有检索意义。"""
    term = (query or "").strip()
    if not term or len(term) > MAX_QUERY_LENGTH:
        raise ApiError.invalid(f"查询词需为 1–{MAX_QUERY_LENGTH} 个字符")
    if any(ord(char) < 32 for char in term):
        raise ApiError.invalid("查询词不能包含控制字符")
    return term


# —— 内部 ——

#: 站点可见性过滤：公开可读；仅站点必须在已授权集合里。
#: 与 ``public_api.deps.can_read_asset`` 的规则一致，只是提前到 SQL 里，
#: 这样私有的已发布内容不会占满 50 条名额。
_PUBLISHED_WHERE = (
    "assets.status = 'published' AND (assets.visibility = 'public' OR "
    "(assets.visibility = 'site' AND EXISTS ("
    "SELECT 1 FROM collection_items ci "
    "JOIN collections c ON c.id = ci.collection_id "
    "JOIN site_grants g ON g.site_id = :site_id "
    "WHERE ci.asset_id = assets.id "
    "AND (c.slug = g.prefix OR c.slug LIKE g.prefix || '.%'))))"
)

_SEARCH_HEAD = (
    "SELECT asset_search.asset_id FROM asset_search "
    "JOIN assets ON assets.id = asset_search.asset_id "
)

_FTS_MATCH = "asset_search MATCH :query AND asset_search.scope = :scope AND "
_SHORT_MATCH = (
    "asset_search.scope = :scope AND "
    "(instr(lower(asset_search.title), :needle) > 0 OR "
    "instr(lower(asset_search.keywords), :needle) > 0) AND "
)


def _published_where() -> str:
    return _PUBLISHED_WHERE


def _search(
    db: DbSession,
    term: str,
    *,
    scope: str,
    limit: int,
    where_sql: str,
    params: dict | None = None,
) -> tuple[str, list[str]]:
    """三字及以上走 FTS，一两个字只匹配标题/文件名/标签。

    ``where_sql`` 在 LIMIT 之前应用：先按状态与授权收窄，再取前 N 条。
    """
    values = {"scope": scope, "limit": limit, **(params or {})}
    if len(term) >= SHORT_QUERY_LENGTH:
        statement = (
            _SEARCH_HEAD
            + "WHERE "
            + _FTS_MATCH
            + where_sql
            + " ORDER BY rank, asset_search.asset_id LIMIT :limit"
        )
        values["query"] = _phrase(term)
        mode = "fts"
    else:
        statement = (
            _SEARCH_HEAD
            + "WHERE "
            + _SHORT_MATCH
            + where_sql
            + " ORDER BY asset_search.rowid LIMIT :limit"
        )
        values["needle"] = term.lower()
        mode = "title"

    rows = db.execute(text(statement), values).all()
    return mode, [row[0] for row in rows]


def _phrase(term: str) -> str:
    """FTS5 短语：整段作为一个子串匹配，内部的引号按 FTS5 规则转义。"""
    return '"' + term.replace('"', '""') + '"'


def _load(
    db: DbSession, ids: list[str], *, status_filter: str, invert: bool
) -> list[Asset]:
    """按索引给出的顺序取内容，并丢掉不该出现在结果里的状态。"""
    if not ids:
        return []
    statement = select(Asset).where(Asset.id.in_(ids))
    if invert:
        statement = statement.where(Asset.status != status_filter)
    else:
        statement = statement.where(Asset.status == status_filter)
    found = {asset.id: asset for asset in db.scalars(statement).all()}
    return [found[asset_id] for asset_id in ids if asset_id in found]


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_RESULTS))


def _admin_row(asset: Asset) -> dict:
    version = _version(asset, asset.current_version_id)
    return {
        "asset_id": asset.id,
        "scope": SCOPE_ADMIN,
        "title": asset.title or "",
        "keywords": _keywords(
            version.original_filename if version else "",
            asset.slug or "",
            [tag.name for tag in asset.tags],
        ),
        "body": (version.text_extract or "") if version else "",
    }


def _published_row(asset: Asset) -> dict | None:
    if asset.status != ASSET_PUBLISHED:
        return None
    version = _version(asset, asset.published_version_id)
    if version is None:
        return None
    return {
        "asset_id": asset.id,
        "scope": SCOPE_PUBLISHED,
        "title": asset.published_title or "",
        "keywords": _keywords(
            version.original_filename,
            asset.published_slug or "",
            _published_tags(asset),
        ),
        "body": version.text_extract or "",
    }


def _keywords(*parts) -> str:
    values = []
    for part in parts:
        if isinstance(part, list):
            values.extend(str(item) for item in part if item)
        elif part:
            values.append(str(part))
    return " ".join(values)


def _published_tags(asset: Asset) -> list[str]:
    if not asset.published_tags:
        return []
    try:
        values = json.loads(asset.published_tags)
    except ValueError:
        return []
    return [str(value) for value in values]


def _version(asset: Asset, version_id: str | None) -> AssetVersion | None:
    if version_id is None:
        return None
    for version in asset.versions:
        if version.id == version_id:
            return version
    return None
