"""集合、集合条目与站点授权。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20

三条约束写在库层：集合别名全局唯一、同一集合里同一内容只出现一次、位置唯一。
位置唯一意味着重排要先让旧值腾地方，见 ``collections.service.reorder``。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID = sa.String(36)
TIMESTAMP = sa.DateTime()


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "site_id",
            ID,
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("slug", sa.String(128), nullable=False, unique=True),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("allowed_kinds", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
    )
    op.create_index("ix_collections_site_id", "collections", ["site_id"])

    op.create_table(
        "collection_items",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "collection_id",
            ID,
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            ID,
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("caption", sa.String(255), nullable=False, server_default=""),
        sa.UniqueConstraint("collection_id", "asset_id", name="ux_collection_asset"),
        sa.UniqueConstraint("collection_id", "position", name="ux_collection_position"),
    )
    op.create_index("ix_collection_items_collection_id", "collection_items", ["collection_id"])
    op.create_index("ix_collection_items_asset_id", "collection_items", ["asset_id"])

    op.create_table(
        "site_grants",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "site_id",
            ID,
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prefix", sa.String(128), nullable=False),
        sa.Column("can_read_unlisted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint("site_id", "prefix", name="ux_site_grant"),
    )
    op.create_index("ix_site_grants_site_id", "site_grants", ["site_id"])


def downgrade() -> None:
    for table in ("site_grants", "collection_items", "collections"):
        op.drop_table(table)
