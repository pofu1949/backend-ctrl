"""上传接口的请求与响应结构。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..models import UploadSession


class CreateUploadRequest(BaseModel):
    """开一个上传会话。``filename`` 的扩展名决定类型与大小上限。"""

    filename: str = Field(min_length=1, max_length=255)
    content_type: str | None = Field(default=None, max_length=128)


class UploadSessionOut(BaseModel):
    """会话状态。完成之后这里带着登记内容所需的全部信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    kind: str | None = None
    original_filename: str
    byte_limit: int
    byte_size: int | None = None
    received_parts: list[int] = Field(default_factory=list)
    asset_id: str | None = None
    storage_key: str | None = None
    mime: str | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    #: 同字节的已有上传。只作提示，不阻止入库（企划第 8.3 节 U5）。
    duplicate_of: str | None = None

    @classmethod
    def of(cls, session: UploadSession, *, duplicate_of: str | None = None) -> UploadSessionOut:
        return cls(
            id=session.id,
            status=session.status,
            kind=session.kind,
            original_filename=session.original_filename,
            byte_limit=session.byte_limit,
            byte_size=session.byte_size,
            received_parts=session.parts,
            asset_id=session.asset_id,
            storage_key=session.storage_key,
            mime=session.mime,
            sha256=session.sha256,
            width=session.width,
            height=session.height,
            created_at=session.created_at,
            expires_at=session.expires_at,
            completed_at=session.completed_at,
            error=session.error,
            duplicate_of=duplicate_of,
        )
