"""文案字典：独立的短文案及其发布快照。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("language", sa.String(32), nullable=False),
        sa.Column("site_id", sa.String(36), sa.ForeignKey("sites.id", ondelete="RESTRICT")),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("published_key", sa.String(128)),
        sa.Column("published_language", sa.String(32)),
        sa.Column("published_value", sa.Text()),
        sa.Column("published_visibility", sa.String(16)),
        sa.Column(
            "published_site_id", sa.String(36), sa.ForeignKey("sites.id", ondelete="RESTRICT")
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ux_entries_library_published",
        "entries",
        ["published_language", "published_key"],
        unique=True,
        sqlite_where=sa.text("status = 'published' AND published_site_id IS NULL"),
    )
    op.create_index(
        "ux_entries_site_published",
        "entries",
        ["published_site_id", "published_language", "published_key"],
        unique=True,
        sqlite_where=sa.text("status = 'published' AND published_site_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("entries")
