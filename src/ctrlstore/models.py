"""数据模型（企划第 10 章）。

时间一律以 UTC 存成不带时区的值：SQLite 没有原生时区类型，若允许两种写法混存，
同一列就会有两种含义。约定「读出来的 datetime 都当 UTC」，展示层再本地化。

主键用 UUID 字符串：SQLite 没有 UUID 类型，字符串在库内可读、可核对。
后续部分继续在同一个 ``Base`` 上加表。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: 角色。只有一行可以是 owner，由部分唯一索引强制（企划第 10.1 节）。
ROLE_OWNER = "owner"
ROLE_EDITOR = "editor"

#: 令牌主体类型。
SUBJECT_USER = "user"
SUBJECT_SITE = "site"


def new_id() -> str:
    """新的主键。"""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """当前 UTC 时间，去掉时区信息，与库内存法一致。"""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """所有表的基类。迁移的 ``target_metadata`` 指向它的 metadata。"""


class User(Base):
    """账号。所有者与编辑各一行，角色不能通过接口修改。"""

    __tablename__ = "users"
    __table_args__ = (
        # SQLite 的普通 UNIQUE 会把多个 NULL 视为不同值，这里要的是「至多一个 owner」。
        Index("ux_users_single_owner", "role", unique=True, sqlite_where=text("role = 'owner'")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    permissions: Mapped[list[UserPermission]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    @property
    def is_active(self) -> bool:
        return self.disabled_at is None


class UserPermission(Base):
    """授予编辑的单项能力。所有者不读这张表，代码里视为拥有全部。"""

    __tablename__ = "user_permissions"
    __table_args__ = (UniqueConstraint("user_id", "permission", name="ux_user_permission"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    permission: Mapped[str] = mapped_column(String(32))

    user: Mapped[User] = relationship(back_populates="permissions")


class Session(Base):
    """管理会话。库里只有令牌哈希，客户端只拿 Cookie 里的原文。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    #: 绝对有效期。到期后无论是否活跃都必须重新登录。
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship(lazy="joined")


class ApiToken(Base):
    """访问令牌。明文只在创建时返回一次，库里只存哈希。"""

    __tablename__ = "api_tokens"
    __table_args__ = (Index("ix_api_tokens_subject", "subject_type", "subject_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Site(Base):
    """网站。第一期只接一个 Next.js 站，但数据模型按多站设计。"""

    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """审计记录。只追加，不修改；登录失败也写在这里。"""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(32), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(36))
    #: 补充信息用 JSON 文本，不参与查询；含 IP 之类不能进正式列的内容。
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# —— 上传会话状态（企划第 11.2 节的状态机）——
UPLOAD_CREATED = "created"
UPLOAD_RECEIVING = "receiving"
UPLOAD_MERGING = "merging"
UPLOAD_STORED = "stored"
UPLOAD_READY = "ready"
UPLOAD_FAILED = "failed"
UPLOAD_EXPIRED = "expired"

#: 已经占住临时目录、还没结束的状态。并发会话上限只数这些。
UPLOAD_OPEN_STATUSES = (UPLOAD_CREATED, UPLOAD_RECEIVING, UPLOAD_MERGING)

# —— 后台任务状态（企划第 10.3 节）——
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"


class UploadSession(Base):
    """一次上传。分片与直传共用，完成后留下足以登记内容的信息。

    第 3 部分不建 ``assets``：完成时只预分配内容 ID 并生成 ``storage_key``，
    第 4 部分读取 ``status='ready'`` 的会话登记内容与版本。
    """

    __tablename__ = "upload_sessions"
    __table_args__ = (Index("ix_upload_sessions_status", "status", "expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    #: 完成后指向即将登记的内容 ID；完成前为空。
    asset_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str | None] = mapped_column(String(16))
    original_filename: Mapped[str] = mapped_column(String(255))
    #: 客户端声明的 Content-Type，只用于一致性检查，不作数。
    content_type: Mapped[str | None] = mapped_column(String(128))
    byte_limit: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    #: 已收到的分片序号，JSON 数组文本。同一序号重传就覆盖它。
    received_parts: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default=UPLOAD_CREATED)
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(255))
    mime: Mapped[str | None] = mapped_column(String(128))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: 失败原因。对外只给稳定描述，不吐内部路径。
    error: Mapped[str | None] = mapped_column(String(255))

    @property
    def parts(self) -> list[int]:
        return json.loads(self.received_parts or "[]")

    @parts.setter
    def parts(self, values: list[int]) -> None:
        self.received_parts = json.dumps(sorted(set(values)))

    @property
    def is_open(self) -> bool:
        return self.status in UPLOAD_OPEN_STATUSES


class Job(Base):
    """后台任务（企划第 10.3 节）。

    只有一个 worker 进程按 ``created_at`` 顺序领取，租约 15 分钟、最多 3 次。
    领取用 ``BEGIN IMMEDIATE``：同一时刻只有一个进程能把 ``pending`` 改成 ``running``。

    这里只存任务本身；做得好不好由业务表体现——缩略图的状态在 ``derivatives``，
    发布的状态在 ``assets``。任务失败重试不会改变这两处已经提交的结果。
    """

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    #: 处理这件事需要的参数，JSON 文本。形状由 ``kind`` 决定。
    payload: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default=JOB_PENDING)
    #: 已经领过几次。领取时加 1，达到上限就停在 ``failed`` 等人查看。
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: 租约到期时间。到期仍是 ``running``，说明 worker 中途退出，可以再领。
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def data(self) -> dict:
        """解析后的参数。坏数据按空参数处理：任务自己会因为缺参数而失败。"""
        try:
            value = json.loads(self.payload or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


class IdempotencyKey(Base):
    """幂等键：同一身份、方法、路径、键，24 小时内返回第一次的结果。"""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("actor_id", "method", "path", "key", name="ux_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(String(36))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(255))
    key: Mapped[str] = mapped_column(String(128))
    #: 请求体（或等价内容）的 SHA-256。同一键换请求体要报冲突。
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# —— 内容状态与可见性（企划第 8.6 节）——
ASSET_DRAFT = "draft"
ASSET_PUBLISHED = "published"
ASSET_ARCHIVED = "archived"
ASSET_TRASHED = "trashed"
ASSET_STATUSES = (ASSET_DRAFT, ASSET_PUBLISHED, ASSET_ARCHIVED, ASSET_TRASHED)

VISIBILITY_PRIVATE = "private"
VISIBILITY_SITE = "site"
VISIBILITY_UNLISTED = "unlisted"
VISIBILITY_PUBLIC = "public"
VISIBILITIES = (VISIBILITY_PRIVATE, VISIBILITY_SITE, VISIBILITY_UNLISTED, VISIBILITY_PUBLIC)

# —— 派生文件（企划第 10.2 节）——
DERIVATIVE_PENDING = "pending"
DERIVATIVE_READY = "ready"
DERIVATIVE_FAILED = "failed"
DERIVATIVE_SKIPPED = "skipped"

#: 派生用途。本部分只生成 thumb；display 与 poster 留好任务类型。
PURPOSE_THUMB = "thumb"
PURPOSE_DISPLAY = "display"
PURPOSE_POSTER = "poster"

#: 派生文件的用途 → 公开区里的文件名。扩展名随用途固定，不随原件变化。
DERIVATIVE_FILENAMES = {
    PURPOSE_THUMB: "thumb.webp",
    PURPOSE_DISPLAY: "display.webp",
    PURPOSE_POSTER: "poster.jpg",
}

#: 抽取状态。第 4 部分只用到 pending；第 9 部分填 ready/failed/skipped。
EXTRACT_PENDING = "pending"
EXTRACT_READY = "ready"
EXTRACT_FAILED = "failed"
EXTRACT_SKIPPED = "skipped"

#: 视频是否可播放。第 9 部分用 ffprobe 判定，其余类型为空。
PLAYBACK_PLAYABLE = "playable"
PLAYBACK_DOWNLOAD_ONLY = "download_only"

#: 不进入任何列表的最终状态。
VISIBLE_STATUSES = (ASSET_DRAFT, ASSET_PUBLISHED, ASSET_ARCHIVED)


class Folder(Base):
    """目录树。``path`` 是物化路径，例如 ``/资料/2026``，根目录为 ``/``。

    路径只用于展示和祖先判断；权限与移动都以 ``id`` 为准。
    """

    __tablename__ = "folders"
    __table_args__ = (
        # 非根目录：同一父目录下名称唯一。
        UniqueConstraint("parent_id", "name", name="ux_folder_parent_name"),
        # 根目录：parent_id 为空时，普通 UNIQUE 不把多个 NULL 视为重复，需要部分索引。
        Index(
            "ux_folder_root_name",
            "name",
            unique=True,
            sqlite_where=text("parent_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("folders.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    path: Mapped[str] = mapped_column(String(1024), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    #: 父目录。移动与改路径都要读它，直接联表取，避免再次查询。
    parent: Mapped[Folder | None] = relationship(remote_side="Folder.id", lazy="joined")

    @property
    def is_root(self) -> bool:
        return self.path == ROOT_PATH


#: 根目录的物化路径。目录本身不以 ``/`` 结尾，根目录是唯一例外。
ROOT_PATH = "/"


class Tag(Base):
    """标签。名称唯一，``slug`` 由名称派生，用于 URL 与筛选。"""

    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Asset(Base):
    """一条内容。

    编辑中的字段（``title`` 等）与已发布快照（``published_*``）分列：
    只有发布才写快照，公开接口只读快照（第 5、6 部分）。
    """

    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_assets_created", "created_at", "id"),
        Index("ix_assets_kind_status", "kind", "status"),
        Index("ix_assets_publish_at", "publish_at"),
        # 已发布的别名全局唯一：撤回会清空该列，所以部分索引即可。
        Index(
            "ux_assets_published_slug",
            "published_slug",
            unique=True,
            sqlite_where=text("published_slug IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))
    folder_id: Mapped[str | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), index=True
    )
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    #: 编辑中的别名。公开读取只看 ``published_slug``。
    slug: Mapped[str | None] = mapped_column(String(128), index=True)

    # —— 编辑中的字段 ——
    title: Mapped[str] = mapped_column(String(255), default="")
    summary: Mapped[str] = mapped_column(String(1024), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    alt: Mapped[str] = mapped_column(String(512), default="")
    language: Mapped[str] = mapped_column(String(16), default="zh-CN")

    status: Mapped[str] = mapped_column(String(16), default=ASSET_DRAFT, index=True)
    visibility: Mapped[str] = mapped_column(String(16), default=VISIBILITY_PRIVATE)
    #: 只允许指向图片内容；不设外键，删除顺序由代码保证。
    cover_asset_id: Mapped[str | None] = mapped_column(String(36))

    #: 最新保存的版本，可能尚未公开。
    current_version_id: Mapped[str | None] = mapped_column(String(36))
    #: 线上版本。第 4 部分恒为空，第 5 部分发布后才有值。
    published_version_id: Mapped[str | None] = mapped_column(String(36))

    # —— 发布快照（第 5 部分写入）——
    published_title: Mapped[str | None] = mapped_column(String(255))
    published_summary: Mapped[str | None] = mapped_column(String(1024))
    published_cover_asset_id: Mapped[str | None] = mapped_column(String(36))
    #: 发布当时的标签名，JSON 数组文本。
    published_tags: Mapped[str | None] = mapped_column(Text)
    #: 发布当时的别名。``/by-slug`` 只认这一个，编辑中的 ``slug`` 不对外生效。
    published_slug: Mapped[str | None] = mapped_column(String(128))
    #: 发布版本正文引用的图片 ID（JSON 数组）。渲染公开 HTML 时只认这一份，
    #: 草稿里增删引用不会改变线上正文。
    published_references: Mapped[str | None] = mapped_column(Text)

    #: 乐观锁：每次写入加 1，请求里的值不一致就 409。
    revision: Mapped[int] = mapped_column(Integer, default=1)
    #: 定时发布：到这个时间之后，worker 把内容按当时的字段发布出去（第 7 部分）。
    #: 立即发布时清空；撤回、归档、进回收站一并取消。
    publish_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    #: 少量扩展字段，不替代正式列。
    custom: Mapped[str | None] = mapped_column(Text)

    versions: Mapped[list[AssetVersion]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
        order_by="AssetVersion.version_no",
        lazy="selectin",
    )
    tags: Mapped[list[Tag]] = relationship(secondary="asset_tags", lazy="selectin")

    @property
    def is_trashed(self) -> bool:
        return self.status == ASSET_TRASHED


class AssetVersion(Base):
    """一次字节快照。写入后不再改变；替换只会产生新行。"""

    __tablename__ = "asset_versions"
    __table_args__ = (UniqueConstraint("asset_id", "version_no", name="ux_asset_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(128))
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    #: 相对键，由服务端生成；禁止绝对路径。
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    #: 有上限的抽取文本，第 9 部分填写。
    text_extract: Mapped[str | None] = mapped_column(Text)
    text_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    extract_status: Mapped[str] = mapped_column(String(16), default=EXTRACT_PENDING)
    playback: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    asset: Mapped[Asset] = relationship(back_populates="versions")
    derivatives: Mapped[list[Derivative]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="Derivative.purpose",
        lazy="selectin",
    )


class Derivative(Base):
    """由某个版本生成的文件：缩略图、展示图、视频封面（企划第 10.2 节）。

    派生文件不是公开文件本身：只有它 ``ready``、且所属版本正处在公开区时，
    ``public/`` 下才会出现对应链接。生成失败只影响这一行，不撤回原图。
    """

    __tablename__ = "derivatives"
    __table_args__ = (UniqueConstraint("version_id", "purpose", name="ux_derivative_purpose"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(16))
    mime: Mapped[str] = mapped_column(String(128))
    #: 相对 ``derivatives/`` 的键，例如 ``aa/<id>/v1/thumb.webp``。
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=DERIVATIVE_PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    version: Mapped[AssetVersion] = relationship(back_populates="derivatives")

    @property
    def filename(self) -> str:
        """公开区里的文件名。用途决定扩展名，不跟着原件走。"""
        return DERIVATIVE_FILENAMES[self.purpose]

    @property
    def is_ready(self) -> bool:
        return self.status == DERIVATIVE_READY


class AssetTag(Base):
    """内容与标签的多对多。"""

    __tablename__ = "asset_tags"
    __table_args__ = (UniqueConstraint("asset_id", "tag_id", name="ux_asset_tag"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    tag_id: Mapped[str] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)


class AssetReference(Base):
    """Markdown 正文里引用的本库图片。发布时据此检查图片是否已发布。"""

    __tablename__ = "asset_references"
    __table_args__ = (UniqueConstraint("from_asset_id", "to_asset_id", name="ux_asset_reference"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    from_asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    to_asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )


class Collection(Base):
    """有序的内容列表，网站按别名读取（企划第 10.2 节）。

    ``site_id`` 为空表示库级集合，授权后多站可读；``allowed_kinds`` 为空表示不限制类型。
    """

    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    site_id: Mapped[str | None] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )
    #: 全局唯一别名，例如 ``home.hero``。
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    #: 允许的类型，JSON 数组文本；空表示不限制。
    allowed_kinds: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    items: Mapped[list[CollectionItem]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        order_by="CollectionItem.position",
        lazy="selectin",
    )

    @property
    def kinds(self) -> list[str]:
        return json.loads(self.allowed_kinds or "[]")

    @kinds.setter
    def kinds(self, values: list[str]) -> None:
        self.allowed_kinds = json.dumps(sorted(set(values))) if values else None


class CollectionItem(Base):
    """集合里的一条。同一集合内同一内容只出现一次，位置唯一。"""

    __tablename__ = "collection_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "asset_id", name="ux_collection_asset"),
        UniqueConstraint("collection_id", "position", name="ux_collection_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    collection_id: Mapped[str] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    #: 覆盖该处的展示标题；为空就用内容自己的标题。
    caption: Mapped[str] = mapped_column(String(255), default="")

    collection: Mapped[Collection] = relationship(back_populates="items")
    asset: Mapped[Asset] = relationship(lazy="joined")


class SiteGrant(Base):
    """站点能读哪些集合。前缀按点号分段匹配：``home`` 匹配 ``home.hero``，

    不匹配 ``homepage``——这是企划第 10.5 节明确要求的边界。
    """

    __tablename__ = "site_grants"
    __table_args__ = (UniqueConstraint("site_id", "prefix", name="ux_site_grant"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    #: 集合别名前缀，例如 ``home`` 或 ``home.hero``。
    prefix: Mapped[str] = mapped_column(String(128))
    #: 是否允许在该前缀下的集合里看到「未列出」内容。
    can_read_unlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
