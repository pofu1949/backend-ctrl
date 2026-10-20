"""标签接口。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session as DbSession

from ..auth import deps
from ..models import Tag
from . import service

router = APIRouter(tags=["tags"])


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    created_at: datetime


class CreateTagRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


@router.get("/tags", response_model=list[TagOut])
def list_tags(
    q: str | None = Query(default=None, description="按名称子串筛选"),
    _: deps.Principal = Depends(deps.require_permission("asset.read")),
    db: DbSession = Depends(deps.get_db),
) -> list[TagOut]:
    return [TagOut.model_validate(tag) for tag in service.list_tags(db, query=q)]


@router.post("/tags", response_model=TagOut)
def create_tag(
    payload: CreateTagRequest,
    principal: deps.Principal = Depends(deps.require_permission("tag.manage")),
    db: DbSession = Depends(deps.get_db),
) -> TagOut:
    """按名称取或建。内容打标签时会自动调用同一函数，因此这里也能当"确保存在"用。"""
    tag: Tag = service.ensure_tag(db, payload.name, actor=principal.user)
    return TagOut.model_validate(tag)
