"""公开接口 ``/api/public/v1``（企划第 12 章）。

网站只消费这里与 ``/p/``，不读磁盘、不打开数据库。调用方要么匿名，
要么带站点令牌；差别只在「能看哪些可见性」，判定全在 ``deps.can_read_asset``。
"""

from __future__ import annotations

from fastapi import APIRouter

from .assets import router as assets_router
from .collections import router as collections_router
from .entries import router as entries_router
from .files import router as files_router
from .pageviews import router as pageviews_router
from .search import router as search_router

router = APIRouter()
router.include_router(collections_router)
router.include_router(entries_router)
router.include_router(assets_router)
router.include_router(files_router)
router.include_router(pageviews_router)
router.include_router(search_router)
