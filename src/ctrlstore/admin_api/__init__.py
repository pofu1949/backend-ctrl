"""管理接口 ``/api/admin/v1``。

只有 ``main.py`` 组装应用时挂载一次；这里汇总各资源的子路由，
新增资源在这张清单里加一行即可。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..assets.router import router as assets_router
from ..collections.router import router as collections_router
from ..folders.router import router as folders_router
from ..tags.router import router as tags_router
from ..uploads.router import router as uploads_router
from . import dashboard, health, search, session, sites, system, users

router = APIRouter()
router.include_router(session.router)
router.include_router(users.router)
router.include_router(sites.router)
router.include_router(uploads_router)
router.include_router(folders_router)
router.include_router(tags_router)
router.include_router(assets_router)
router.include_router(collections_router)
router.include_router(dashboard.router)
router.include_router(search.router)
router.include_router(health.router)
router.include_router(system.router)
