"""后台任务、派生文件与定时发布。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21

第 7 部分的三处新增：

* ``jobs`` —— 唯一 worker 从这里领取任务，租约 15 分钟、最多 3 次；
* ``derivatives`` —— 某个版本的缩略图 / 展示图 / 视频封面的登记与状态；
* ``assets.publish_at`` —— 到点才进公开区的定时发布。

三张表都在同一个 SQLite 里，不引入队列服务（企划第 7、17 章）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID = sa.String(36)
TIMESTAMP = sa.DateTime()


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", ID, primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", TIMESTAMP, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
    )
    op.create_index("ix_jobs_kind", "jobs", ["kind"])
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"])

    op.create_table(
        "derivatives",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "version_id",
            ID,
            sa.ForeignKey("asset_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint("version_id", "purpose", name="ux_derivative_purpose"),
        sa.UniqueConstraint("storage_key", name="uq_derivatives_storage_key"),
    )
    op.create_index("ix_derivatives_version_id", "derivatives", ["version_id"])

    with op.batch_alter_table("assets") as batch:
        batch.add_column(sa.Column("publish_at", TIMESTAMP, nullable=True))
    op.create_index("ix_assets_publish_at", "assets", ["publish_at"])


def downgrade() -> None:
    op.drop_index("ix_assets_publish_at", table_name="assets")
    with op.batch_alter_table("assets") as batch:
        batch.drop_column("publish_at")
    op.drop_table("derivatives")
    op.drop_table("jobs")
