"""API 入口。

启动顺序：读配置 → 装日志 → 打开 SQLite 并应用 PRAGMA → 执行迁移 → 开始服务。
迁移失败即启动失败，不会带着结构不明的库对外提供接口。

匿名健康接口只回答进程状态；磁盘、队列等细节留给所有者的管理接口（后续部分实现）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from . import __version__
from .admin_api import router as admin_router
from .auth.throttle import LoginThrottle
from .config import Settings, load_settings
from .db import create_engine, run_migrations
from .errors import register_error_handlers
from .health import check_health
from .log import configure_logging
from .public_api import router as public_router
from .stats.throttle import PageviewThrottle

logger = logging.getLogger(__name__)

#: 管理接口前缀（企划第 13 章）。
ADMIN_PREFIX = "/api/admin/v1"

#: 公开接口前缀。
PUBLIC_PREFIX = "/api/public/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用。测试可注入配置，生产由 ``ctrlstore serve`` 调用。"""
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        logger.info(
            "启动 CtrlStore",
            extra={
                "version": __version__,
                "data_dir": str(settings.data_dir),
                "config_path": str(settings.config_path or ""),
            },
        )
        run_migrations(settings, engine=app.state.engine)
        logger.info("数据库已迁移到最新版本", extra={"database": str(settings.db_path)})
        try:
            yield
        finally:
            app.state.engine.dispose()
            logger.info("CtrlStore 已停止")

    app = FastAPI(
        title="CtrlStore",
        version=__version__,
        lifespan=lifespan,
        # 第一期不对外暴露交互式文档；接口契约写在企划第 12、13 章。
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    # 每个请求一个 Session；连接仍只有一条，由引擎的 pool_size=1 保证。
    app.state.session_factory = sessionmaker(bind=app.state.engine, expire_on_commit=False)
    app.state.login_throttle = LoginThrottle()
    app.state.pageview_throttle = PageviewThrottle(
        max_per_window=settings.pageview_rate_per_minute
    )

    register_error_handlers(app)
    app.include_router(admin_router, prefix=ADMIN_PREFIX)
    app.include_router(public_router, prefix=PUBLIC_PREFIX)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """匿名健康检查，响应体只有 ``status``。"""
        return check_health(settings).as_public()

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        """最小自述，供部署后确认连到了哪个版本。"""
        return {"service": "ctrlstore", "version": __version__}

    return app
