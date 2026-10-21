"""集合管理接口的请求与响应结构。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..models import Collection, CollectionItem


class CollectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slug: str
    title: str
    site_id: str | None = None
    allowed_kinds: list[str] = Field(default_factory=list)
    created_at: datetime

    @classmethod
    def of(cls, collection: Collection) -> CollectionOut:
        return cls(
            id=collection.id,
            slug=collection.slug,
            title=collection.title,
            site_id=collection.site_id,
            allowed_kinds=collection.kinds,
            created_at=collection.created_at,
        )


class CollectionItemOut(BaseModel):
    id: str
    asset_id: str
    position: int
    caption: str

    @classmethod
    def of(cls, item: CollectionItem) -> CollectionItemOut:
        return cls(id=item.id, asset_id=item.asset_id, position=item.position, caption=item.caption)


class CreateCollectionRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=128, description="例如 home.hero")
    title: str = Field(default="", max_length=255)
    site_id: str | None = None
    allowed_kinds: list[str] = Field(default_factory=list)


class UpdateCollectionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    allowed_kinds: list[str] | None = None


class AddItemRequest(BaseModel):
    asset_id: str
    caption: str = Field(default="", max_length=255)
    position: int | None = Field(default=None, ge=0, description="不给就放到末尾")


class ReorderRequest(BaseModel):
    """按给定顺序整体重排；列表必须与集合现有条目完全一致。"""

    asset_ids: list[str]


class CaptionRequest(BaseModel):
    caption: str = Field(default="", max_length=255)


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    site_id: str
    prefix: str
    can_read_unlisted: bool
    created_at: datetime


class CreateGrantRequest(BaseModel):
    prefix: str = Field(min_length=1, max_length=128, description="例如 home")
    can_read_unlisted: bool = False
