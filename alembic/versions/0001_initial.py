"""基线：确认空库已带上必要的连接级开关。

Revision ID: 0001
Revises:
Create Date: 2026-10-20

SQLite 的 PRAGMA 除 ``journal_mode`` 外都是连接级设置，无法写进库结构，
因此由 ``ctrlstore.db`` 在每次建连时统一应用；这里只做一次断言，
把「连接带着 ``foreign_keys=ON`` 与 WAL」变成迁移过程中可见的失败点。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    foreign_keys = bind.exec_driver_sql("PRAGMA foreign_keys").scalar()
    if foreign_keys != 1:
        raise RuntimeError("foreign_keys 未开启：请通过 ctrlstore 的引擎执行迁移")

    journal_mode = bind.exec_driver_sql("PRAGMA journal_mode").scalar()
    if str(journal_mode).lower() not in {"wal", "memory"}:
        raise RuntimeError(f"journal_mode 不是 WAL：{journal_mode}")


def downgrade() -> None:
    """空库基线没有结构，回退只删除版本记录。"""
