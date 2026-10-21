"""全文检索、派生文件字节与运维所需的列。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-21

两件事：

* ``asset_search`` —— SQLite FTS5 虚拟表，trigram 分词，三字及以上按子串匹配。
  每个资产最多两行：``admin`` 用编辑中的标题/文件名/标签与当前版本正文；
  ``published`` 用发布快照与已发布版本正文。站点搜索只读后者。
* ``derivatives.byte_size`` —— 派生文件字节数，配额统计用。

已有数据在迁移里回填一次；之后由 ``search.refresh`` 在内容变更时维护。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("derivatives") as batch:
        batch.add_column(
            sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0")
        )

    # trigram 分词支持中文按子串匹配，不需要额外安装分词插件（企划第 16.1 节）。
    op.execute(
        "CREATE VIRTUAL TABLE asset_search USING fts5("
        "asset_id UNINDEXED, scope UNINDEXED, title, keywords, body, tokenize='trigram')"
    )
    op.execute(
        "INSERT INTO asset_search (asset_id, scope, title, keywords, body) "
        "SELECT a.id, 'admin', COALESCE(a.title, ''), "
        "TRIM(COALESCE(v.original_filename, '') || ' ' || COALESCE(a.slug, '') || ' ' || "
        "COALESCE((SELECT group_concat(t.name, ' ') FROM asset_tags atg "
        "JOIN tags t ON t.id = atg.tag_id WHERE atg.asset_id = a.id), '')), "
        "COALESCE(v.text_extract, '') "
        "FROM assets a LEFT JOIN asset_versions v ON v.id = a.current_version_id"
    )
    op.execute(
        "INSERT INTO asset_search (asset_id, scope, title, keywords, body) "
        "SELECT a.id, 'published', COALESCE(a.published_title, ''), "
        "TRIM(COALESCE(a.published_slug, '') || ' ' || COALESCE(a.published_tags, '') || ' ' || "
        "COALESCE(v.original_filename, '')), "
        "COALESCE(v.text_extract, '') "
        "FROM assets a LEFT JOIN asset_versions v ON v.id = a.published_version_id "
        "WHERE a.status = 'published'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS asset_search")
    with op.batch_alter_table("derivatives") as batch:
        batch.drop_column("byte_size")
