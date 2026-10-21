"""访问统计：事件去重、日汇总、路径次数。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21

四张表（企划第 10.4 节）。都是小表，继续放在同一个 SQLite 里，不引入统计服务：

* ``pageview_daily``    —— 按站点、按上海自然日的人数与次数，长期保留；
* ``pageview_events``   —— 24 小时内的 event_id 去重，避免网站重试把次数翻倍；
* ``pageview_visitors`` —— 当日已计过人数的人，主键冲突就表示人数不再加；
* ``pageview_paths``    —— 按路径的次数，超过 500 条的并入「其他」。

后三张是明细，只保留 400 天；删它们不会改动 ``pageview_daily``（企划第 8.9 节）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SITE_ID = sa.String(36)
DAY = sa.String(10)
TIMESTAMP = sa.DateTime()


def upgrade() -> None:
    op.create_table(
        "pageview_daily",
        sa.Column(
            "site_id",
            SITE_ID,
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", DAY, primary_key=True),
        sa.Column("uv", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pv", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_pageview_daily_day", "pageview_daily", ["day"])

    op.create_table(
        "pageview_events",
        sa.Column(
            "site_id",
            SITE_ID,
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("received_at", TIMESTAMP, nullable=False),
    )
    op.create_index("ix_pageview_events_received", "pageview_events", ["received_at"])

    op.create_table(
        "pageview_visitors",
        sa.Column(
            "site_id",
            SITE_ID,
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", DAY, primary_key=True),
        sa.Column("visitor_hash", sa.String(64), primary_key=True),
    )
    op.create_index("ix_pageview_visitors_day", "pageview_visitors", ["day"])

    op.create_table(
        "pageview_paths",
        sa.Column(
            "site_id",
            SITE_ID,
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", DAY, primary_key=True),
        sa.Column("path", sa.String(512), primary_key=True),
        sa.Column("pv", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_pageview_paths_day", "pageview_paths", ["day"])


def downgrade() -> None:
    op.drop_table("pageview_paths")
    op.drop_table("pageview_visitors")
    op.drop_table("pageview_events")
    op.drop_table("pageview_daily")
