"""Alembic 运行环境。

连接来源有两种：

* ``ctrlstore.db.run_migrations`` 通过 ``config.attributes["engine"]`` 传入应用引擎，
  这样迁移和请求共用同一个连接与同一套 PRAGMA。
* 开发者直接用 ``alembic`` 命令时，按配置文件（或 ``CTRLSTORE_*`` 环境变量）现场建引擎。
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from ctrlstore.config import load_settings
from ctrlstore.db import create_engine
from ctrlstore.models import Base

config = context.config


def _target_metadata():
    """模型元数据。``alembic revision --autogenerate`` 需要它，手工迁移也保持同步。"""
    return Base.metadata


def _connection() -> Connection:
    provided = config.attributes.get("engine")
    if provided is not None:
        return provided.connect()
    return create_engine(load_settings()).connect()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=_target_metadata(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite 改表必须走批处理模式（企划第 16.1 节）。
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = _connection()
    try:
        context.configure(
            connection=connection,
            target_metadata=_target_metadata(),
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
