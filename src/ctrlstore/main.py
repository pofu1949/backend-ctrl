"""API 入口。

启动顺序：读配置 → 装日志 → 打开 SQLite 并应用 PRAGMA → 执行迁移 → 开始服务。
迁移失败即启动失败，不会带着结构不明的库对外提供接口。

匿名健康接口只回答进程状态；磁盘、队列等细节留给所有者的管理接口（后续部分实现）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import sessionmaker

from . import __version__
from .admin_api import router as admin_router
from .admin_ui import ASSETS_DIR, INDEX_HTML
from .auth.throttle import LoginThrottle
from .config import Settings, load_settings
from .db import create_engine, run_migrations
from .errors import register_error_handlers
from .health import check_health
from .log import configure_logging
from .public_api import router as public_router
from .stats.throttle import PageviewThrottle

logger = logging.getLogger(__name__)


def _import_legacy_content(app: FastAPI) -> None:
    """把已有文案和集合授权补进区块。只增不删，重复启动不会再插一遍。"""
    from .sections.legacy import import_legacy

    factory = app.state.session_factory
    with factory() as db:
        try:
            stats = import_legacy(db)
            db.commit()
        except Exception:
            db.rollback()
            raise
    if any(stats.values()):
        logger.info("旧文案与集合授权已补进区块", extra=stats)

#: 管理接口前缀（企划第 13 章）。
ADMIN_PREFIX = "/api/admin/v1"

#: 公开接口前缀。
PUBLIC_PREFIX = "/api/public/v1"

#: 管理端页面的安全头。脚本与样式都从本进程的 /admin/assets 加载，
#: 不引入 CDN；内联样式只用于模板里的布局微调，因此 style-src 放开 inline。
ADMIN_CSP = (
    "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "connect-src 'self'; font-src 'self'"
)
ADMIN_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": ADMIN_CSP,
}


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
        _import_legacy_content(app)
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

    @app.middleware("http")
    async def public_files_cors(request, call_next):
        # 页面上的画布和 WebGL 会读取 /p/ 图片。跨端口时浏览器要求公开文件带回源。
        if request.method == "OPTIONS" and request.url.path.startswith("/p/"):
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    "Access-Control-Max-Age": "600",
                },
            )
        response = await call_next(request)
        if request.url.path.startswith("/p/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    # 管理端是同一进程提供的单页应用。静态资源只有应用样式与脚本，
    # HTML 不内联数据；未登录时页面只显示登录框，真实数据仍由 /api/admin 保护。
    app.mount("/admin/assets", StaticFiles(directory=ASSETS_DIR), name="admin-assets")
    # 生产由 Nginx 把 public/ 映射到 /p/。本地关掉 X-Accel 时没有那一层，
    # 公开地址会全部 404。这里只挂已发布目录，原件目录仍然不对外。
    if not settings.x_accel_redirect:
        app.mount(
            "/p",
            StaticFiles(directory=settings.public_dir, check_dir=False),
            name="public-files",
        )

    @app.get("/admin", include_in_schema=False)
    def admin_redirect() -> RedirectResponse:
        return RedirectResponse(url="/admin/", status_code=308)

    @app.get("/admin/", include_in_schema=False)
    def admin_page() -> FileResponse:
        # 公开文件可放在独立域名；只放行配置指定的源，外站图片仍由渲染器移除。
        public = urlsplit(settings.public_base_url)
        image_origin = f"{public.scheme}://{public.netloc}"
        headers = {
            **ADMIN_HEADERS,
            "Content-Security-Policy": ADMIN_CSP.replace(
                "img-src 'self' data: blob:", f"img-src 'self' data: blob: {image_origin}"
            ),
        }
        return FileResponse(
            INDEX_HTML,
            media_type="text/html; charset=utf-8",
            headers=headers,
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """匿名健康检查，响应体只有 ``status``。"""
        return check_health(settings).as_public()

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        """浏览器打开根地址就进入管理端。进程是否存活仍看 ``/healthz``。"""
        return RedirectResponse(url="/admin/", status_code=302)

    return app
