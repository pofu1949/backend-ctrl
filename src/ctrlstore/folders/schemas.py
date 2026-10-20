"""目录接口的请求与响应结构。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    parent_id: str | None = None
    name: str
    #: 物化路径，例如 ``/资料/2026``；根目录为 ``/``。
    path: str
    created_at: datetime


class CreateFolderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    parent_id: str | None = None


class RenameFolderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class MoveFolderRequest(BaseModel):
    """移动到根目录时把 ``parent_id`` 设为 ``null``。"""

    parent_id: str | None = None
