"""内容接口的请求与响应结构。

公开接口（第 6 部分）只读 ``published_*`` 快照；这里的响应是管理视图，
带编辑中的字段与全部版本。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import Asset, AssetVersion


class TagRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str


class VersionOut(BaseModel):
    """一个版本。``storage_key`` 只给管理员看，公开接口不会出现。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    version_no: int
    original_filename: str
    mime: str
    byte_size: int
    sha256: str
    storage_key: str
    width: int | None = None
    height: int | None = None
    extract_status: str
    playback: str | None = None
    created_at: datetime

    @classmethod
    def of(cls, version: AssetVersion) -> VersionOut:
        return cls.model_validate(version)


class AssetOut(BaseModel):
    id: str
    kind: str
    title: str
    summary: str
    description: str
    alt: str
    language: str
    slug: str | None = None
    status: str
    visibility: str
    folder_id: str | None = None
    cover_asset_id: str | None = None
    revision: int
    current_version: VersionOut | None = None
    #: 线上版本 id。第 4 部分恒为空，发布之后才有值。
    published_version_id: str | None = None
    tags: list[TagRefOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, asset: Asset, *, current: AssetVersion | None) -> AssetOut:
        return cls(
            id=asset.id,
            kind=asset.kind,
            title=asset.title,
            summary=asset.summary,
            description=asset.description,
            alt=asset.alt,
            language=asset.language,
            slug=asset.slug,
            status=asset.status,
            visibility=asset.visibility,
            folder_id=asset.folder_id,
            cover_asset_id=asset.cover_asset_id,
            revision=asset.revision,
            current_version=VersionOut.of(current) if current else None,
            published_version_id=asset.published_version_id,
            tags=[TagRefOut.model_validate(tag) for tag in asset.tags],
            created_at=asset.created_at,
            updated_at=asset.updated_at,
        )


class AssetDetailOut(AssetOut):
    """详情多带全部保留版本，按版本号升序。"""

    versions: list[VersionOut] = Field(default_factory=list)

    @classmethod
    def detailed(cls, asset: Asset, *, current: AssetVersion | None) -> AssetDetailOut:
        base = AssetOut.of(asset, current=current)
        return cls(
            **base.model_dump(),
            versions=[VersionOut.of(version) for version in asset.versions],
        )


class AssetListOut(BaseModel):
    """游标分页：``next_cursor`` 为空表示没有下一页。"""

    items: list[AssetOut]
    next_cursor: str | None = None


class VersionActionRequest(BaseModel):
    """发布、撤回、归档、回收站、恢复都按 ``revision`` 串行（企划第 8.6 节）。"""

    revision: int = Field(ge=1)


class SignedUrlOut(BaseModel):
    """短时签名地址。绑定已发布版本，撤回后自动失效。"""

    url: str
    expires_at: int


class CreateTextAssetRequest(BaseModel):
    """直接创建 Markdown 或纯文本（企划第 8.4 节 T1）。

    文件类内容走上传：``POST /uploads`` 完成后自动登记，不需要再调这里。
    """

    kind: Literal["markdown", "text"]
    body: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=255)
    summary: str | None = Field(default=None, max_length=1024)
    slug: str | None = Field(default=None, max_length=128)
    folder_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class UpdateAssetRequest(BaseModel):
    """改内容必须带 ``revision``（企划第 13.1 节）。

    ``body`` 只对 Markdown 与纯文本有效：改正文会写成新版本（企划第 8.4 节 T1）。
    ``extra="forbid"``：写错字段名要报错，而不是被静默丢掉——
    否则调用方会以为改了（例如误写 ``published_title``）。
    """

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=255)
    summary: str | None = Field(default=None, max_length=1024)
    description: str | None = None
    alt: str | None = Field(default=None, max_length=512)
    language: str | None = Field(default=None, max_length=16)
    slug: str | None = Field(default=None, max_length=128)
    folder_id: str | None = None
    cover_asset_id: str | None = None
    visibility: Literal["private", "site", "unlisted", "public"] | None = None
    tags: list[str] | None = Field(default=None, description="整体替换标签")
    body: str | None = Field(
        default=None,
        description="Markdown / 纯文本的新正文；会写成新版本",
    )

    def changes(self) -> dict[str, object]:
        """只取显式给出的列字段。``tags`` 与 ``body`` 各自单独处理。"""
        payload = self.model_dump(exclude={"revision", "tags", "body"}, exclude_unset=True)
        return payload
