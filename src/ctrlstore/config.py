"""配置。

配置分成两层：

* 非机密项（路径、阈值、公开基址）写在 TOML 里，默认路径 ``/etc/ctrlstore/config.toml``。
* 机密项（会话密钥、访客哈希密钥）只来自环境变量或 root 只读的密钥文件。

环境变量优先于 TOML；同名变量加前缀 ``CTRLSTORE_``，例如 ``CTRLSTORE_DATA_DIR``。
主配置 TOML 里出现机密项会被拒绝，避免密钥随配置一起进入版本库或备份。
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MiB = 1024**2
GiB = 1024**3

#: 配置文件的默认位置。
DEFAULT_CONFIG_PATH = Path("/etc/ctrlstore/config.toml")

#: 配置项的环境变量前缀。
ENV_PREFIX = "CTRLSTORE_"

#: 指定配置文件路径的环境变量。
ENV_CONFIG_PATH = f"{ENV_PREFIX}CONFIG"

#: 指定密钥文件路径的环境变量。
ENV_SECRETS_PATH = f"{ENV_PREFIX}SECRETS_FILE"

#: 只允许来自环境变量或独立密钥文件的机密项。
SECRET_FIELDS = ("session_secret", "pageview_hmac_secret")

#: 机密项的最小长度（字符）。
MIN_SECRET_LENGTH = 32

SECRETS_TABLE = "secrets"

#: 是否 POSIX。密钥文件权限只有 POSIX 有语义，单列出来便于测试覆盖。
IS_POSIX = os.name == "posix"


class ConfigError(RuntimeError):
    """配置不完整、不可读或存在安全问题时抛出，进程不应带着它启动。"""


class SizeLimits(BaseModel):
    """各类型的单文件上限（企划第 9 章）。``kind`` 与内容类型一一对应。"""

    image: int = 30 * MiB
    video: int = 2 * GiB
    markdown: int = 2 * MiB
    text: int = 2 * MiB
    pdf: int = 100 * MiB
    docx: int = 50 * MiB
    xlsx: int = 50 * MiB

    @field_validator("image", "video", "markdown", "text", "pdf", "docx", "xlsx")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("上限必须是正数")
        return value

    def for_kind(self, kind: str) -> int:
        """取某类型的上限。未知类型按最小的一档处理，避免放过超大文件。"""
        return int(getattr(self, kind, self.markdown))


class Settings(BaseSettings):
    """运行期配置。字段默认值即是部署基线（见企划第 17.4 节）。"""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="ignore",
        validate_default=True,
    )

    # —— 路径 ——
    data_dir: Path = Path("/var/lib/ctrlstore")
    database_path: Path | None = None
    originals_dir: Path | None = None
    derivatives_dir: Path | None = None
    public_dir: Path | None = None
    tmp_dir: Path | None = None

    # —— 对外地址 ——
    public_base_url: str = "http://127.0.0.1:8000"

    #: 私有下载是否交给 Nginx 发送（``X-Accel-Redirect``）。
    #: 生产必须为 true；开发环境没有 Nginx 时关掉，由进程直接发送文件。
    x_accel_redirect: bool = True

    # —— 健康阈值 ——
    min_free_bytes: int = 10 * GiB
    min_free_ratio: float = 0.05
    min_available_memory_bytes: int = 1 * GiB

    # —— 单文件上限 ——
    limits: SizeLimits = Field(default_factory=SizeLimits)

    # —— 版本保留与回收站（企划第 11.3、8.8 节）——
    #: 图片、文档保留的版本数。两个指针占用的版本不计入这条上限。
    version_retention: int = 5
    #: 视频只留待发布版与已发布版。
    video_version_retention: int = 2
    #: 回收站保留天数。到期由 worker 彻底删除；仍被引用的会跳过。
    trash_retention_days: int = 30

    # —— 派生文件（第 7 部分）——
    #: 缩略图最长边（像素）。展示图留给后续增强，先只定尺寸。
    thumb_max_side: int = 480
    display_max_side: int = 1600

    # —— 访问上报（第 8 部分）——
    #: 每个站点令牌每分钟最多接受多少条上报。网站的转发请求远低于这个量。
    pageview_rate_per_minute: int = 600

    # —— 配额（第 9 部分）。0 表示不限 ——
    #: 单个账号的字节配额。含历史版本、派生文件与回收站。
    user_quota_bytes: int = 0
    #: 全库字节配额。口径同上。
    library_quota_bytes: int = 0
    #: 单个账号的内容条数配额（未彻底删除的都算，含回收站）。
    user_quota_items: int = 0
    #: 全库内容条数配额。
    library_quota_items: int = 0

    #: 文案区块条数上限。和页面区块分开计数，不占内容条数或字节配额。
    entry_limit: int = Field(default=2000, ge=1)
    #: 页面区块条数上限，不含文案区块。
    section_limit: int = Field(default=500, ge=1)

    # —— 运维（第 9 部分）——
    #: 备份根目录。所有者健康接口读这里最新的成功清单；脚本默认 /var/backups/ctrlstore。
    backup_dir: Path | None = None
    #: 站点搜索默认关闭；打开后只搜已发布、且该站点能读到的内容。
    site_search_enabled: bool = False

    # —— 会话与 Cookie ——
    #: 绝对有效期（小时），从登录算起。
    session_absolute_hours: int = 12
    #: 空闲有效期（小时），每次访问刷新。
    session_idle_hours: int = 2
    #: 是否给会话 Cookie 加 Secure。启用 HTTPS 后必须打开。
    cookie_secure: bool = False

    # —— 进程 ——
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # —— 机密项 ——
    session_secret: str = Field(repr=False)
    pageview_hmac_secret: str = Field(repr=False)

    # —— 来源，便于日志和排错 ——
    secrets_file: Path | None = Field(default=None, exclude=True)
    config_path: Path | None = Field(default=None, exclude=True)

    @field_validator(
        "data_dir",
        "database_path",
        "originals_dir",
        "derivatives_dir",
        "public_dir",
        "tmp_dir",
        "backup_dir",
    )
    @classmethod
    def _absolute_path(cls, value: Path | None) -> Path | None:
        """拒绝相对路径：数据目录不应随进程工作目录漂移。"""
        if value is not None and not value.is_absolute():
            raise ValueError("必须是绝对路径")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("必须是 http(s) 绝对地址")
        if parts.query or parts.fragment:
            raise ValueError("不能带查询串或片段")
        return value.rstrip("/")

    @field_validator("min_free_ratio")
    @classmethod
    def _ratio_in_range(cls, value: float) -> float:
        if not 0 < value < 1:
            raise ValueError("必须在 0 与 1 之间")
        return value

    @field_validator(
        "version_retention",
        "video_version_retention",
        "trash_retention_days",
        "thumb_max_side",
        "display_max_side",
        "pageview_rate_per_minute",
    )
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("必须是正整数")
        return value

    @field_validator(
        "user_quota_bytes",
        "library_quota_bytes",
        "user_quota_items",
        "library_quota_items",
    )
    @classmethod
    def _non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("不能为负数；0 表示不限")
        return value

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("未知日志级别")
        return level

    @field_validator(*SECRET_FIELDS)
    @classmethod
    def _strong_secret(cls, value: str) -> str:
        if len(value) < MIN_SECRET_LENGTH:
            raise ValueError(f"至少 {MIN_SECRET_LENGTH} 个字符")
        return value

    @model_validator(mode="after")
    def _session_window_sane(self) -> Settings:
        """空闲窗口必须短于绝对窗口，否则“绝对过期”形同虚设。"""
        if self.session_absolute_hours <= 0 or self.session_idle_hours <= 0:
            raise ValueError("会话时长必须为正数")
        if self.session_idle_hours > self.session_absolute_hours:
            raise ValueError("空闲有效期不能长于绝对有效期")
        return self

    @model_validator(mode="after")
    def _fill_directories(self) -> Settings:
        """未单独指定的目录都落在 ``data_dir`` 下。"""
        defaults = {
            "database_path": self.data_dir / "ctrlstore.db",
            "originals_dir": self.data_dir / "originals",
            "derivatives_dir": self.data_dir / "derivatives",
            "public_dir": self.data_dir / "public",
            "tmp_dir": self.data_dir / "tmp",
        }
        for name, fallback in defaults.items():
            if getattr(self, name) is None:
                object.__setattr__(self, name, fallback)
        return self

    @property
    def db_path(self) -> Path:
        """元数据库文件。值由 :meth:`_fill_directories` 补齐，这里只做类型收窄。"""
        return cast(Path, self.database_path)

    @property
    def db_url(self) -> str:
        """SQLAlchemy 连接串。SQLite 路径用正斜杠，Windows 与 POSIX 一致。"""
        return f"sqlite:///{self.db_path.as_posix()}"

    def managed_directories(self) -> tuple[Path, ...]:
        """``init`` 负责创建的目录。数据库文件在 ``data_dir`` 内一并创建。"""
        return (
            self.data_dir,
            cast(Path, self.originals_dir),
            cast(Path, self.derivatives_dir),
            cast(Path, self.public_dir),
            cast(Path, self.tmp_dir),
        )


def load_settings(config_path: Path | str | None = None) -> Settings:
    """读取配置并校验。

    优先级：环境变量 > 密钥文件 > 主配置 TOML > 字段默认值。
    任何缺失或非法都抛 :class:`ConfigError`，由调用方让进程启动失败。

    显式给出的路径（参数或 ``CTRLSTORE_CONFIG``）必须存在：写错路径时静默退回默认值，
    会让服务在错误的目录上启动。只有隐式的默认路径允许不存在。
    """
    explicit = config_path is not None or ENV_CONFIG_PATH in os.environ
    path = Path(config_path or os.environ.get(ENV_CONFIG_PATH, DEFAULT_CONFIG_PATH))
    values = _read_config_file(path, required=explicit)
    values.update(_read_secrets_file(values.get("secrets_file"), config_path=path))
    values.update(_read_environment(Settings))
    values["config_path"] = path

    try:
        return Settings(**values)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, path)) from exc


def _read_environment(settings_cls: type[Settings]) -> dict[str, Any]:
    """按字段名收集显式设置的环境变量。"""
    collected: dict[str, Any] = {}
    for name in settings_cls.model_fields:
        raw = os.environ.get(f"{ENV_PREFIX}{name.upper()}")
        if raw is not None:
            collected[name] = raw
    return collected


def _read_config_file(path: Path, *, required: bool) -> dict[str, Any]:
    """读取主配置 TOML。显式指定时必须存在，隐式默认路径可以缺失。"""
    values = _load_toml(path, required=required)
    leaked = sorted(set(values) & set(SECRET_FIELDS))
    if leaked:
        raise ConfigError(
            f"{path} 含机密项 {', '.join(leaked)}；请改用环境变量或 secrets_file 指向的密钥文件"
        )
    return values


def _read_secrets_file(secrets_file: Any, *, config_path: Path) -> dict[str, Any]:
    """读取密钥文件。密钥表可嵌在 ``[secrets]`` 下，也可直接写在顶层。

    相对路径按主配置所在目录解析，便于把两份文件放在同一目录里。
    """
    if not secrets_file:
        return {}
    path = Path(secrets_file)
    if not path.is_absolute():
        path = config_path.parent / path
    _ensure_private_permissions(path)
    values = _load_toml(path, required=True)
    nested = values.get(SECRETS_TABLE)
    if isinstance(nested, dict):
        values = {**values, **nested}
    return {key: values[key] for key in SECRET_FIELDS if key in values}


def _load_toml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"配置文件不存在：{path}")
        return {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"无法读取配置：{path}（{exc}）") from exc
    try:
        # 允许 UTF-8 BOM：用 Windows 编辑器改配置时很常见，tomllib 不接受它。
        return tomllib.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"配置不是 UTF-8：{path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置不是合法的 TOML：{path}（{exc}）") from exc


def _ensure_private_permissions(path: Path) -> None:
    """密钥文件不能对同组和其他用户可读。Windows 无此语义，跳过。"""
    if not IS_POSIX:
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ConfigError(f"无法检查密钥文件权限：{path}（{exc}）") from exc
    require_private_mode(mode, path)


def require_private_mode(mode: int, path: Path) -> None:
    """检查权限位。单独成函数，便于不依赖真实文件权限来测试。"""
    if mode & 0o077:
        raise ConfigError(f"密钥文件权限过宽（{mode:o}）：{path} 应为 0600")


def _format_validation_error(exc: ValidationError, path: Path) -> str:
    """把 pydantic 的报错整理成一行一项的中文提示。"""
    lines = [f"配置校验失败（{path}）："]
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "配置"
        lines.append(f"  - {field}: {error['msg']}")
    missing = [
        e for e in exc.errors() if e["type"] == "missing" and str(e["loc"][-1]) in SECRET_FIELDS
    ]
    if missing:
        names = ", ".join(f"{ENV_PREFIX}{str(e['loc'][-1]).upper()}" for e in missing)
        lines.append(f"  机密项需来自环境变量（{names}）或 secrets_file")
    return "\n".join(lines)
