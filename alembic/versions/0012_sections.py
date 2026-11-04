"""分层区块：一份 JSON 正文及其发布快照。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("language", sa.String(32), nullable=False),
        sa.Column("site_id", sa.String(36), sa.ForeignKey("sites.id", ondelete="RESTRICT")),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("template", sa.String(32), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("published_slug", sa.String(128)),
        sa.Column("published_language", sa.String(32)),
        sa.Column(
            "published_site_id", sa.String(36), sa.ForeignKey("sites.id", ondelete="RESTRICT")
        ),
        sa.Column("published_title", sa.String(255)),
        sa.Column("published_template", sa.String(32)),
        sa.Column("published_visibility", sa.String(16)),
        sa.Column("published_body", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ux_sections_library_slug",
        "sections",
        ["language", "slug"],
        unique=True,
        sqlite_where=sa.text("site_id IS NULL"),
    )
    op.create_index(
        "ux_sections_site_slug",
        "sections",
        ["site_id", "language", "slug"],
        unique=True,
        sqlite_where=sa.text("site_id IS NOT NULL"),
    )
    op.create_index(
        "ux_sections_library_published",
        "sections",
        ["published_language", "published_slug"],
        unique=True,
        sqlite_where=sa.text("status = 'published' AND published_site_id IS NULL"),
    )
    op.create_index(
        "ux_sections_site_published",
        "sections",
        ["published_site_id", "published_language", "published_slug"],
        unique=True,
        sqlite_where=sa.text("status = 'published' AND published_site_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("sections")
