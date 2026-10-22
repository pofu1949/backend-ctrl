"""管理接口的文案请求与响应。公开接口另建只含快照的响应。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CreateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    language: str = "zh-CN"
    value: str = ""
    visibility: Literal["public", "site"] = "public"
    site_id: str | None = None


class UpdateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    key: str | None = None
    language: str | None = None
    value: str | None = None
    visibility: Literal["public", "site"] | None = None
    site_id: str | None = None


class EntryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)


class EntryOut(CreateEntry):
    model_config = ConfigDict(from_attributes=True)

    id: str
    revision: int
    status: Literal["draft", "published"]
    published_key: str | None
    published_language: str | None
    published_value: str | None
    published_visibility: str | None
    published_site_id: str | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EntryList(BaseModel):
    items: list[EntryOut]
    next_cursor: str | None = None
