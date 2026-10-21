"""用户与全库的配额（企划第 8.8 节 R4、第 11.4 节）。

两条独立的限制：

* **字节**：所有版本文件、所有派生文件，含回收站。公开区硬链接不是新字节，
  不重复计数；临时分片不在库里，只由磁盘阈值约束。
* **条数**：未彻底删除的内容条数，含回收站；一条内容只算 1，不按版本。

计账主体是**内容所有者**，不是本次操作者：编辑替换他人内容时，字节与条数
都算在原所有者名下（新建时二者相同）。

配额是「本次请求之后不能超过」。上传在开会话时按该类型的单文件上限预估一次，
合并完成时再按真实字节数检查一次——早拒绝能省掉整段传输，晚检查兜住实际大小。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .config import Settings
from .errors import ApiError
from .models import Asset, AssetVersion, Derivative


def usage_bytes(db: DbSession, *, owner_id: str | None = None) -> int:
    """已占用字节。``owner_id`` 为空时统计全库。"""
    versions = select(func.coalesce(func.sum(AssetVersion.byte_size), 0)).select_from(
        AssetVersion
    )
    derivatives = select(func.coalesce(func.sum(Derivative.byte_size), 0)).select_from(
        Derivative
    )
    if owner_id is not None:
        versions = versions.join(
            Asset, Asset.id == AssetVersion.asset_id
        ).where(Asset.owner_id == owner_id)
        derivatives = derivatives.join(
            AssetVersion, AssetVersion.id == Derivative.version_id
        ).join(Asset, Asset.id == AssetVersion.asset_id).where(Asset.owner_id == owner_id)
    return int(db.scalar(versions) or 0) + int(db.scalar(derivatives) or 0)


def ensure_capacity(
    db: DbSession, *, settings: Settings, owner_id: str, extra_bytes: int
) -> None:
    """写入 ``extra_bytes`` 之后是否仍在配额内。0 配额的维度不检查。"""
    if extra_bytes <= 0:
        return
    if not settings.library_quota_bytes and not settings.user_quota_bytes:
        # 默认不限：不要让每次上传都多查两次合计。
        return
    if settings.library_quota_bytes and (
        usage_bytes(db) + extra_bytes > settings.library_quota_bytes
    ):
        raise ApiError.insufficient_storage("全库配额不足，无法写入这个文件")
    if settings.user_quota_bytes and (
        usage_bytes(db, owner_id=owner_id) + extra_bytes > settings.user_quota_bytes
    ):
        raise ApiError.insufficient_storage("账号配额不足，无法写入这个文件")


def count_items(db: DbSession, *, owner_id: str | None = None) -> int:
    """未彻底删除的内容条数。含回收站；一条内容只算 1，不按版本。"""
    statement = select(func.count()).select_from(Asset)
    if owner_id is not None:
        statement = statement.where(Asset.owner_id == owner_id)
    return int(db.scalar(statement) or 0)


def ensure_item_capacity(
    db: DbSession, *, settings: Settings, owner_id: str, extra_items: int = 1
) -> None:
    """新建内容前检查条数配额。替换文件不增加条数，不调用这里。"""
    if extra_items <= 0:
        return
    if not settings.library_quota_items and not settings.user_quota_items:
        return
    if settings.library_quota_items and (
        count_items(db) + extra_items > settings.library_quota_items
    ):
        raise ApiError.insufficient_storage("全库内容条数已达配额")
    if settings.user_quota_items and (
        count_items(db, owner_id=owner_id) + extra_items > settings.user_quota_items
    ):
        raise ApiError.insufficient_storage("账号内容条数已达配额")
