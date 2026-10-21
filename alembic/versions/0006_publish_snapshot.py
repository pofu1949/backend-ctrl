"""发布快照补两列：别名与正文引用的图片。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21

返工原因：公开读取曾经混用了编辑中的状态。

* ``published_slug`` —— ``/by-slug`` 只认发布时的别名，编辑中的 ``slug`` 不对外生效；
  已发布的别名用部分唯一索引兜底。
* ``published_references`` —— 发布版本正文引用的图片 ID。渲染公开 HTML 时只认这一份，
  草稿里增删图片不会改变访客看到的正文。

已有数据按当前状态回填一次：已发布内容的别名取编辑中的 ``slug``，
引用留空（回填后这些内容的图片会被视为"未声明"，下一次发布会写入正确值）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("assets") as batch:
        batch.add_column(sa.Column("published_slug", sa.String(128), nullable=True))
        batch.add_column(sa.Column("published_references", sa.Text(), nullable=True))

    op.execute(
        "UPDATE assets SET published_slug = slug WHERE status = 'published' AND slug IS NOT NULL"
    )
    op.create_index(
        "ux_assets_published_slug",
        "assets",
        ["published_slug"],
        unique=True,
        sqlite_where=sa.text("published_slug IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_assets_published_slug", table_name="assets")
    with op.batch_alter_table("assets") as batch:
        batch.drop_column("published_references")
        batch.drop_column("published_slug")
