"""上传会话与幂等键。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20

两张表都放在同一个 SQLite 里（企划第 10.3 节），不另起服务。
``upload_sessions`` 同时服务分片上传和一次性上传：完成时留下登记内容所需的一切。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID = sa.String(36)
TIMESTAMP = sa.DateTime()


def upgrade() -> None:
    op.create_table(
        "upload_sessions",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "actor_id",
            ID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_id", ID, nullable=True),
        sa.Column("kind", sa.String(16), nullable=True),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("byte_limit", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("received_parts", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("storage_key", sa.String(255), nullable=True),
        sa.Column("mime", sa.String(128), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=False),
        sa.Column("completed_at", TIMESTAMP, nullable=True),
        sa.Column("error", sa.String(255), nullable=True),
    )
    op.create_index("ix_upload_sessions_actor_id", "upload_sessions", ["actor_id"])
    op.create_index("ix_upload_sessions_status", "upload_sessions", ["status", "expires_at"])

    op.create_table(
        "idempotency_keys",
        sa.Column("id", ID, primary_key=True),
        sa.Column("actor_id", ID, nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.UniqueConstraint("actor_id", "method", "path", "key", name="ux_idempotency_key"),
    )
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("upload_sessions")
