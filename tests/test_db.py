"""SQLite 连接设置与迁移（规划第 1 部分）。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from ctrlstore.config import Settings
from ctrlstore.db import PRAGMAS, create_engine, migration_script_location, run_migrations


def _head_revision() -> str:
    """迁移链的最新版本。硬编码版本号会让每加一份迁移都要改测试。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(migration_script_location()))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def _pragma(engine, name: str):
    with engine.connect() as connection:
        return connection.exec_driver_sql(f"PRAGMA {name}").scalar()


def test_migration_script_location_exists() -> None:
    location = migration_script_location()

    assert (location / "env.py").is_file()
    assert any((location / "versions").glob("*.py"))


def test_migration_upgrades_empty_database(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        run_migrations(settings, engine=engine)
        with engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()

    assert revision == _head_revision()
    assert settings.db_path.is_file()


def test_migration_is_idempotent(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        run_migrations(settings, engine=engine)
        run_migrations(settings, engine=engine)
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).all()
    finally:
        engine.dispose()

    assert rows == [(_head_revision(),)]


def test_pragmas_are_applied(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        run_migrations(settings, engine=engine)

        assert str(_pragma(engine, "journal_mode")).lower() == "wal"
        assert _pragma(engine, "foreign_keys") == 1
        assert _pragma(engine, "synchronous") == 1  # NORMAL
        assert _pragma(engine, "cache_size") == -8000
        assert _pragma(engine, "mmap_size") == 0
        assert _pragma(engine, "temp_store") == 1  # FILE
    finally:
        engine.dispose()


def test_pragmas_survive_new_connection(settings: Settings) -> None:
    """连接级 PRAGMA 必须在每次建连时重放，不能只在第一条连接上生效。"""
    engine = create_engine(settings)
    try:
        run_migrations(settings, engine=engine)
        engine.dispose()

        assert _pragma(engine, "foreign_keys") == 1
        assert _pragma(engine, "busy_timeout") == 5000
    finally:
        engine.dispose()


def test_pragma_list_covers_plan(settings: Settings) -> None:
    statements = {statement.split("=")[0].split()[-1] for statement in PRAGMAS}

    assert {
        "journal_mode",
        "busy_timeout",
        "foreign_keys",
        "synchronous",
        "cache_size",
        "mmap_size",
        "temp_store",
    } == statements


def test_engine_creates_missing_parent_directory(tmp_path: Path, settings: Settings) -> None:
    nested = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "nested" / "ctrlstore.db",
        session_secret=settings.session_secret,
        pageview_hmac_secret=settings.pageview_hmac_secret,
    )

    engine = create_engine(nested)
    try:
        run_migrations(nested, engine=engine)
    finally:
        engine.dispose()

    assert nested.db_path.is_file()
