"""用途标签：由「集合成员 + 区块配图引用」实时派生，不写标签表。

管理端的标签列表、内容筛选与详情展示都读这里；加入/移出集合或区块后
下一次请求自然反映，无需同步。slug 带类型前缀，避免集合与区块别名相撞。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .models import Collection, CollectionItem, Section
from .sections.validation import asset_ids as section_asset_ids


@dataclass
class UsageTag:
    slug: str
    name: str
    kind: str
    asset_ids: set[str] = field(default_factory=set)


def build(db: DbSession) -> dict[str, UsageTag]:
    """全量扫一遍集合与区块。数据量小，按请求现算，不做缓存。"""
    tags: dict[str, UsageTag] = {}

    def entry(kind: str, slug: str, name: str) -> UsageTag:
        key = f"{kind}:{slug}"
        if key not in tags:
            tags[key] = UsageTag(slug=key, name=name or slug, kind=kind)
        return tags[key]

    members = db.execute(
        select(Collection.slug, Collection.title, CollectionItem.asset_id).join(
            CollectionItem, CollectionItem.collection_id == Collection.id
        )
    ).all()
    for slug, title, asset_id in members:
        entry("collection", slug, title).asset_ids.add(asset_id)

    for section in db.scalars(select(Section)).all():
        ids = set(section_asset_ids(section.template, json.loads(section.body)))
        if section.published_body and section.published_template:
            ids.update(
                section_asset_ids(section.published_template, json.loads(section.published_body))
            )
        # 没有引用任何素材的区块不生成标签：筛选不出内容的标签只是噪音。
        if ids:
            entry("section", section.slug, section.title).asset_ids.update(ids)
    return tags


def sorted_tags(index: dict[str, UsageTag]) -> list[UsageTag]:
    return sorted(index.values(), key=lambda tag: (tag.kind, tag.name))


def for_asset(index: dict[str, UsageTag], asset_id: str) -> list[UsageTag]:
    return sorted(
        (tag for tag in index.values() if asset_id in tag.asset_ids),
        key=lambda tag: (tag.kind, tag.name),
    )


def asset_ids(index: dict[str, UsageTag], slug: str) -> set[str]:
    tag = index.get(slug)
    return tag.asset_ids if tag else set()