"""区块读者名单。不改动文案表和集合表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "section_readers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "section_id",
            sa.String(36),
            sa.ForeignKey("sections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            sa.String(36),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("section_id", "site_id", "published", name="ux_section_reader"),
    )
    op.create_index("ix_section_readers_section", "section_readers", ["section_id"])
    op.create_index("ix_section_readers_site", "section_readers", ["site_id", "published"])


def downgrade() -> None:
    op.drop_table("section_readers")
