"""审计记录。

只追加。每条记录都属于「谁、做了什么、对谁、什么时候」。
登录失败没有合法主体，用 ``actor_type="anonymous"`` 表示。

调用方负责提交事务：审计与应用状态在同一事务里，避免出现「改了数据但没留痕」。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session as DbSession

from .models import AuditLog, User, utcnow

ACTOR_ANONYMOUS = "anonymous"
ACTOR_USER = "user"
ACTOR_SITE = "site"
ACTOR_SYSTEM = "system"

TARGET_USER = "user"
TARGET_SITE = "site"
TARGET_TOKEN = "token"
TARGET_UPLOAD = "upload"
TARGET_FOLDER = "folder"
TARGET_TAG = "tag"
TARGET_ASSET = "asset"
TARGET_COLLECTION = "collection"
TARGET_ENTRY = "entry"
ENTRY_CREATED = "entry_created"
ENTRY_UPDATED = "entry_updated"
ENTRY_PUBLISHED = "entry_published"
ENTRY_UNPUBLISHED = "entry_unpublished"
ENTRY_DELETED = "entry_deleted"
TARGET_SECTION = "section"
SECTION_CREATED = "section_created"
SECTION_UPDATED = "section_updated"
SECTION_PUBLISHED = "section_published"
SECTION_UNPUBLISHED = "section_unpublished"
SECTION_DELETED = "section_deleted"

#: 已知动作。集中列出，避免日志和界面里出现拼错的动作名。
LOGIN = "login"
LOGIN_FAILED = "login_failed"
LOGOUT = "logout"
USER_CREATED = "user_created"
USER_DISABLED = "user_disabled"
PASSWORD_RESET = "password_reset"
PERMISSIONS_CHANGED = "permissions_changed"
SITE_CREATED = "site_created"
TOKEN_CREATED = "token_created"
TOKEN_REVOKED = "token_revoked"
UPLOAD_CREATED = "upload_created"
UPLOAD_COMPLETED = "upload_completed"
UPLOAD_CANCELLED = "upload_cancelled"
UPLOAD_FAILED = "upload_failed"
FOLDER_CREATED = "folder_created"
FOLDER_RENAMED = "folder_renamed"
FOLDER_MOVED = "folder_moved"
FOLDER_DELETED = "folder_deleted"
TAG_CREATED = "tag_created"
TAG_MERGED = "tag_merged"
ASSET_CREATED = "asset_created"
ASSET_UPDATED = "asset_updated"
ASSET_VERSION_ADDED = "asset_version_added"
ASSET_VERSION_PRUNED = "asset_version_pruned"
ASSET_PUBLISHED = "asset_published"
ASSET_UNPUBLISHED = "asset_unpublished"
ASSET_ARCHIVED = "asset_archived"
ASSET_TRASHED = "asset_trashed"
ASSET_RESTORED = "asset_restored"
ASSET_PURGED = "asset_purged"
ASSET_PUBLISH_SCHEDULED = "asset_publish_scheduled"
ASSET_PUBLISH_FAILED = "asset_publish_failed"
COLLECTION_CREATED = "collection_created"
COLLECTION_UPDATED = "collection_updated"
COLLECTION_ITEM_ADDED = "collection_item_added"
COLLECTION_ITEM_REMOVED = "collection_item_removed"
SITE_GRANT_ADDED = "site_grant_added"
SITE_GRANT_REMOVED = "site_grant_removed"


def record(
    db: DbSession,
    *,
    action: str,
    actor_type: str = ACTOR_SYSTEM,
    actor_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """写入一条审计记录（调用方提交）。"""
    entry = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id or "",
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else None,
        created_at=utcnow(),
    )
    db.add(entry)
    return entry


def record_for_user(
    db: DbSession,
    actor: User,
    *,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """以某个用户的身份记一条。"""
    return record(
        db,
        action=action,
        actor_type=ACTOR_USER,
        actor_id=actor.id,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
    )
