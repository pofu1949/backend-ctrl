"""目录、标签、内容与版本。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20

建六张表：folders、tags、assets、asset_versions、asset_tags、asset_references。

两条约束写在库层而不是只写在代码里：
* 目录名在同一父目录下唯一；根目录用部分唯一索引（多个 NULL 不算重复）；
* 同一内容的版本号唯一，替换只能新增行，不能改写历史。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID = sa.String(36)
TIMESTAMP = sa.DateTime()


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "parent_id",
            ID,
            sa.ForeignKey("folders.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint("parent_id", "name", name="ux_folder_parent_name"),
    )
    op.create_index("ix_folders_parent_id", "folders", ["parent_id"])
    op.create_index("ix_folders_path", "folders", ["path"])
    # 普通 UNIQUE 会把多个 NULL 当成不同值，因此根目录单独用部分索引。
    op.create_index(
        "ux_folder_root_name",
        "folders",
        ["name"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NULL"),
    )

    op.create_table(
        "tags",
        sa.Column("id", ID, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
    )

    op.create_table(
        "assets",
        sa.Column("id", ID, primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "folder_id",
            ID,
            sa.ForeignKey("folders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "owner_id",
            ID,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(128), nullable=True),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("summary", sa.String(1024), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("alt", sa.String(512), nullable=False, server_default=""),
        sa.Column("language", sa.String(16), nullable=False, server_default="zh-CN"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="private"),
        sa.Column("cover_asset_id", ID, nullable=True),
        sa.Column("current_version_id", ID, nullable=True),
        sa.Column("published_version_id", ID, nullable=True),
        sa.Column("published_title", sa.String(255), nullable=True),
        sa.Column("published_summary", sa.String(1024), nullable=True),
        sa.Column("published_cover_asset_id", ID, nullable=True),
        sa.Column("published_tags", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("published_at", TIMESTAMP, nullable=True),
        sa.Column("trashed_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.Column("custom", sa.Text(), nullable=True),
    )
    op.create_index("ix_assets_created", "assets", ["created_at", "id"])
    op.create_index("ix_assets_kind_status", "assets", ["kind", "status"])
    op.create_index("ix_assets_folder_id", "assets", ["folder_id"])
    op.create_index("ix_assets_owner_id", "assets", ["owner_id"])
    op.create_index("ix_assets_slug", "assets", ["slug"])
    op.create_index("ix_assets_status", "assets", ["status"])

    op.create_table(
        "asset_versions",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "asset_id",
            ID,
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False, unique=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("text_extract", sa.Text(), nullable=True),
        sa.Column("text_truncated", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("extract_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("playback", sa.String(16), nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint("asset_id", "version_no", name="ux_asset_version"),
    )
    op.create_index("ix_asset_versions_asset_id", "asset_versions", ["asset_id"])
    op.create_index("ix_asset_versions_sha256", "asset_versions", ["sha256"])

    op.create_table(
        "asset_tags",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "asset_id",
            ID,
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            ID,
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("asset_id", "tag_id", name="ux_asset_tag"),
    )
    op.create_index("ix_asset_tags_asset_id", "asset_tags", ["asset_id"])
    op.create_index("ix_asset_tags_tag_id", "asset_tags", ["tag_id"])

    op.create_table(
        "asset_references",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "from_asset_id",
            ID,
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_asset_id",
            ID,
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("from_asset_id", "to_asset_id", name="ux_asset_reference"),
    )
    op.create_index("ix_asset_references_from", "asset_references", ["from_asset_id"])
    op.create_index("ix_asset_references_to", "asset_references", ["to_asset_id"])


def downgrade() -> None:
    for table in (
        "asset_references",
        "asset_tags",
        "asset_versions",
        "assets",
        "tags",
        "folders",
    ):
        op.drop_table(table)
