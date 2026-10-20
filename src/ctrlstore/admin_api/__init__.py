"""管理接口 ``/api/admin/v1``。

只有 ``main.py`` 组装应用时挂载一次；这里汇总各资源的子路由，
新增资源（内容、目录、集合……）在这张清单里加一行即可。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..uploads.router import router as uploads_router
from . import session, sites, users

router = APIRouter()
router.include_router(session.router)
router.include_router(users.router)
router.include_router(sites.router)
router.include_router(uploads_router)
