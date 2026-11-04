"""区块管理接口的请求与响应。公开接口另建只含快照的响应。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AssetBrief(BaseModel):
    id: str
    title: str
    kind: str
    status: str
    visibility: str


class CreateSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    language: str = "zh-CN"
    title: str = ""
    template: str = "stack"
    visibility: Literal["public", "site"] = "public"
    site_id: str | None = None
    body: dict[str, Any] | str | None = None
    #: 能读本区块所引用「仅站点」素材的站点。不放进正文。
    reader_ids: list[str] | None = None


class UpdateSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    slug: str | None = None
    language: str | None = None
    title: str | None = None
    visibility: Literal["public", "site"] | None = None
    site_id: str | None = None
    body: dict[str, Any] | str | None = None
    reader_ids: list[str] | None = None


class SectionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    reader_ids: list[str] | None = None


class SectionOut(BaseModel):
    id: str
    slug: str
    language: str
    site_id: str | None
    title: str
    template: str
    visibility: Literal["public", "site"]
    status: Literal["draft", "published"]
    body: dict[str, Any] | str
    reader_ids: list[str] = Field(default_factory=list)
    published_slug: str | None
    published_language: str | None
    published_site_id: str | None
    published_title: str | None
    published_template: str | None
    published_visibility: str | None
    published_body: dict[str, Any] | str | None
    published_reader_ids: list[str] = Field(default_factory=list)
    revision: int
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    assets: dict[str, AssetBrief] = Field(default_factory=dict)


class SectionList(BaseModel):
    items: list[SectionOut]
    next_cursor: str | None = None
