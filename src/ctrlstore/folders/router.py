"""目录管理 ``/api/admin/v1/folders``。

浏览目录要 ``asset.read``，改动目录要 ``folder.manage``。
树结构由 ``path`` 表达，客户端按 ``path`` 排序即可渲染。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from ..auth import deps
from . import service
from .schemas import CreateFolderRequest, FolderOut, MoveFolderRequest, RenameFolderRequest

router = APIRouter(tags=["folders"])


@router.get("/folders", response_model=list[FolderOut])
def list_folders(
    _: deps.Principal = Depends(deps.require_permission("asset.read")),
    db: DbSession = Depends(deps.get_db),
) -> list[FolderOut]:
    return [FolderOut.model_validate(folder) for folder in service.list_folders(db)]


@router.post("/folders", response_model=FolderOut, status_code=201)
def create_folder(
    payload: CreateFolderRequest,
    principal: deps.Principal = Depends(deps.require_permission("folder.manage")),
    db: DbSession = Depends(deps.get_db),
) -> FolderOut:
    folder = service.create_folder(
        db, name=payload.name, parent_id=payload.parent_id, actor=principal.user
    )
    return FolderOut.model_validate(folder)


@router.patch("/folders/{folder_id}", response_model=FolderOut)
def rename_folder(
    folder_id: str,
    payload: RenameFolderRequest,
    principal: deps.Principal = Depends(deps.require_permission("folder.manage")),
    db: DbSession = Depends(deps.get_db),
) -> FolderOut:
    folder = service.rename_folder(
        db, service.get_folder(db, folder_id), name=payload.name, actor=principal.user
    )
    return FolderOut.model_validate(folder)


@router.post("/folders/{folder_id}/move", response_model=FolderOut)
def move_folder(
    folder_id: str,
    payload: MoveFolderRequest,
    principal: deps.Principal = Depends(deps.require_permission("folder.manage")),
    db: DbSession = Depends(deps.get_db),
) -> FolderOut:
    """移动目录，子孙跟着走。不能移到自己或子孙下面。"""
    folder = service.move_folder(
        db, service.get_folder(db, folder_id), parent_id=payload.parent_id, actor=principal.user
    )
    return FolderOut.model_validate(folder)
