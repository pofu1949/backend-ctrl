"""令牌来源 IP 与路径合并账本。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-21

* ``api_tokens.allowed_ip`` —— 令牌可绑定允许的来源 IP（企划第 8.1 节 A4）。
  逗号分隔多个；空表示不限制。检查在解析令牌时做，命令行令牌与站点令牌同规则。
* ``pageview_merged_paths`` —— 被并入「其他」的路径次数账本。路径再次出现时
  可以恢复成独立一行，不会永远沉在「其他」里。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch:
        batch.add_column(sa.Column("allowed_ip", sa.String(128), nullable=True))

    op.create_table(
        "pageview_merged_paths",
        sa.Column(
            "site_id",
            sa.String(36),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("path", sa.String(512), primary_key=True),
        sa.Column("pv", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_pageview_merged_paths_day", "pageview_merged_paths", ["day"]
    )


def downgrade() -> None:
    op.drop_table("pageview_merged_paths")
    with op.batch_alter_table("api_tokens") as batch:
        batch.drop_column("allowed_ip")
