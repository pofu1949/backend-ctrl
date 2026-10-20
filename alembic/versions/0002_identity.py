"""身份、会话与权限。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20

建六张表：users、user_permissions、sessions、api_tokens、sites、audit_logs。
「至多一个所有者」用部分唯一索引在库层强制，不只靠应用代码。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 主键与时间列的统一定义，避免六张表各写一遍。
ID = sa.String(36)
TIMESTAMP = sa.DateTime()


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", ID, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("disabled_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
    )
    op.create_index(
        "ux_users_single_owner",
        "users",
        ["role"],
        unique=True,
        sqlite_where=sa.text("role = 'owner'"),
    )

    op.create_table(
        "user_permissions",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "user_id",
            ID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("permission", sa.String(32), nullable=False),
        sa.UniqueConstraint("user_id", "permission", name="ux_user_permission"),
    )
    op.create_index("ix_user_permissions_user_id", "user_permissions", ["user_id"])

    op.create_table(
        "sessions",
        sa.Column("id", ID, primary_key=True),
        sa.Column(
            "user_id",
            ID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("last_seen_at", TIMESTAMP, nullable=False),
        sa.Column("absolute_expires_at", TIMESTAMP, nullable=False),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "api_tokens",
        sa.Column("id", ID, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", ID, nullable=False),
        sa.Column("expires_at", TIMESTAMP, nullable=True),
        sa.Column("last_used_at", TIMESTAMP, nullable=True),
        sa.Column("revoked_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
    )
    op.create_index("ix_api_tokens_subject", "api_tokens", ["subject_type", "subject_id"])

    op.create_table(
        "sites",
        sa.Column("id", ID, primary_key=True),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", ID, primary_key=True),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", ID, nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", ID, nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    for table in ("audit_logs", "sites", "api_tokens", "sessions", "user_permissions", "users"):
        op.drop_table(table)
