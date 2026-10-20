"""SQLite 连接、PRAGMA 与迁移执行。

设计要点（企划第 16.1、17.1 节）：

* 每个进程只保留一个连接，不做连接池放大。API 两个进程、worker 一个进程，
  三个连接足以支撑单机规模，也让写锁竞争变得可预测。
* 除 ``journal_mode`` 外，PRAGMA 都是连接级设置，必须在每次新建连接时重放。
* 事务保持短：调用方自己提交，本模块不隐藏提交点。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy import create_engine as sa_create_engine

from .config import Settings

#: 每次新建连接都要应用的 PRAGMA。顺序有意义：WAL 先于写入类设置。
PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-8000",
    "PRAGMA mmap_size=0",
    "PRAGMA temp_store=FILE",
)

#: 等待写锁的最长秒数，与 ``busy_timeout`` 保持一致。
BUSY_TIMEOUT_SECONDS = 5.0


def create_engine(settings: Settings) -> Engine:
    """按配置建立引擎。

    用 ``QueuePool`` 且 ``pool_size=1, max_overflow=0``：进程内只有一个连接，
    并发请求在获取连接时排队，而不是各自再开一条 SQLite 连接。
    """
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa_create_engine(
        settings.db_url,
        # SQLite 连接会被不同线程复用，必须放开同线程检查。
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_SECONDS},
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        future=True,
    )
    _install_pragmas(engine)
    return engine


def _install_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _apply(dbapi_connection: sqlite3.Connection, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for statement in PRAGMAS:
                cursor.execute(statement)
        finally:
            cursor.close()
        # Python 的 sqlite3 默认会自己开事务，交给 SQLAlchemy 管理。
        dbapi_connection.isolation_level = None


def migration_script_location() -> Path:
    """迁移脚本目录：优先用随包安装的副本，其次用仓库里的 ``alembic/``。"""
    packaged = Path(__file__).resolve().parent / "migrations"
    if (packaged / "env.py").is_file():
        return packaged
    repository = Path(__file__).resolve().parents[2] / "alembic"
    return repository


def run_migrations(settings: Settings, *, engine: Engine | None = None) -> None:
    """把数据库升到最新迁移版本。

    迁移失败直接抛出：服务不应在结构不明的库上开始处理请求。
    """
    from alembic import command
    from alembic.config import Config

    script_location = migration_script_location()
    if not (script_location / "env.py").is_file():
        raise FileNotFoundError(f"找不到迁移脚本目录：{script_location}")

    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", settings.db_url)
    if engine is not None:
        # 复用应用引擎，避免迁移期间再开一条连接。
        config.attributes["engine"] = engine
    command.upgrade(config, "head")
