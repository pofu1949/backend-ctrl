"""公开接口 ``/api/public/v1``（企划第 12 章）。

网站只消费这里与 ``/p/``，不读磁盘、不打开数据库。调用方要么匿名，
要么带站点令牌；差别只在「能看哪些可见性」，判定全在 ``deps.can_read_asset``。
"""

from __future__ import annotations

from fastapi import APIRouter

from .assets import router as assets_router
from .entries import router as entries_router
from .files import router as files_router
from .pageviews import router as pageviews_router
from .search import router as search_router
from .sections import router as sections_router

router = APIRouter()
# 网站按区块别名读取。集合不再作为公开主路径；旧表仍保留，管理端仍可查看。
router.include_router(entries_router)
router.include_router(sections_router)
router.include_router(assets_router)
router.include_router(files_router)
router.include_router(pageviews_router)
router.include_router(search_router)
