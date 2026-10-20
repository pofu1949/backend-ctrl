# CtrlStore

一台 Linux 服务器上的内容库：接收、存放、编目、发布图片、视频、Markdown、PDF、Word、Excel 和纯文本，
并向同一台机器上的网站提供稳定的读取方式。文件字节落在磁盘，SQLite 只保存索引、权限、发布状态与检索文本。

* 范围与决议：[plan/项目企划书.md](plan/项目企划书.md)
* 实施顺序：[plan/分步实现规划.md](plan/分步实现规划.md)

当前进度：**第 2 部分（身份、会话与权限）已完成**。上传、发布、公开读取、页面尚未实现。

## 目录

```text
backend_ctrl/
  plan/                     企划书与分步实现规划
  src/ctrlstore/
    config.py               配置分层与校验
    db.py                   SQLite 连接、PRAGMA、迁移执行
    models.py               数据模型（表结构）
    errors.py               统一错误码与响应
    audit.py                审计记录
    health.py               健康判据（内存、磁盘）
    log.py                  JSON 日志
    main.py                 API 入口与路由挂载
    cli.py                  ctrlstore 命令行
    auth/                   密码、会话、令牌、权限、限速、业务操作、依赖
      passwords.py          Argon2id
      sessions.py           Cookie 会话（库里存哈希）
      tokens.py             访问令牌
      permissions.py        八项能力与语义
      throttle.py           登录失败限速
      service.py            接口与命令行共用的业务规则
      deps.py               身份解析与权限检查的唯一入口
    admin_api/              管理接口 /api/admin/v1
      session.py users.py sites.py schemas.py
    assets/ uploads/ content/ imaging/ public_api/ storage/
                            后续部分按此分层填充
  alembic/                  迁移脚本（0001 基线、0002 身份）
  deploy/                   Nginx、systemd、示例配置
  scripts/                  备份与恢复
  tests/                    pytest
```

代码约定：模块按职责分目录，一个文件只做一件事；中文注释说明「为什么」而不是「做了什么」；
面向后续接手的人，宁可多一层显式，也不用隐式魔法。

## 本地开发

需要 Python 3.13（部署目标 Debian 13 自带版本）。依赖用 `uv` 锁定，`uv.lock` 进版本库。

```bash
uv sync --all-groups          # 建 .venv 并装齐运行与开发依赖
uv run pytest                 # 跑测试
uv run ruff check .           # 静态检查
```

没有 `uv` 时也可以用 `python -m venv .venv` 加 `pip install -e ".[dev]"`。

开发时不要把数据库放进仓库：配置里的路径指到仓库外的临时目录。

Windows 上开发只用于跑测试与命令行，`/var/lib/ctrlstore` 这类默认值当然不可用，测试一律把数据目录指到临时目录。
生产以 Debian 13 + Python 3.13 为准。

## 配置

配置分两层，这是硬规则：

* 非机密项写在 TOML，默认 `/etc/ctrlstore/config.toml`，可用 `--config` 或 `CTRLSTORE_CONFIG` 指定。
  仓库里只有示例：[deploy/ctrlstore.example.toml](deploy/ctrlstore.example.toml)。
* 机密项（`session_secret`、`pageview_hmac_secret`）**只能**来自环境变量或 root 只读的密钥文件。
  主配置 TOML 里出现这两项会被拒绝，避免密钥随配置进版本库或备份。

优先级：环境变量 > 密钥文件 > 主配置 TOML > 字段默认值。环境变量名为 `CTRLSTORE_` 加字段名大写，
例如 `CTRLSTORE_DATA_DIR`、`CTRLSTORE_SESSION_SECRET`。

密钥文件由主配置的 `secrets_file` 指定，权限必须是 `0600`（POSIX 上检查）：

```toml
[secrets]
session_secret = "…至少 32 字符…"
pageview_hmac_secret = "…至少 32 字符…"
```

## 命令

```bash
ctrlstore check-config                      # 只校验配置，部署前自检
ctrlstore init                              # 建数据目录 + 迁移 + 创建唯一所有者
ctrlstore serve                             # 启动 API（默认 127.0.0.1:8000）

ctrlstore user create <用户名> [--preset editor] [--permission asset.read ...]
ctrlstore user list                         # 账号与权限一览
ctrlstore user grant <用户名> --preset editor
ctrlstore user revoke <用户名> --permission asset.publish
ctrlstore user disable <用户名>              # 立即失效该账号的会话与令牌
ctrlstore owner reset-password              # 只重置现有所有者，不新建账号

ctrlstore site create <标识> [--name 显示名]
ctrlstore site list
ctrlstore token create (--site 标识 | --user 用户名) [--name 备注] [--expires-days N]
ctrlstore token list
ctrlstore token revoke <令牌 ID>
```

`init` 会提示输入两次密码；非交互环境加 `--password-stdin` 从标准输入读一行。
密码一律走标准输入，不进 shell 历史，也不出现在进程列表里。

`serve` 在前台跑一个 Uvicorn 进程。部署基线是 systemd 起两个 `ctrlstore serve` 进程 + 一个 worker，
不靠 Uvicorn 自带的 `--workers`，这样每个进程的配置、日志和资源上限都由 systemd 统一约束（第 9 部分给单元文件）。

## 接口

| 方法 | 路径 | 权限 |
| --- | --- | --- |
| GET | `/healthz` | 匿名；响应体只有 `status` |
| GET | `/` | 匿名；服务名与版本 |
| POST | `/api/admin/v1/session` | 匿名登录，成功后下发会话 Cookie |
| DELETE | `/api/admin/v1/session` | 登录用户；撤销当前会话 |
| GET | `/api/admin/v1/me` | 登录用户；返回身份与 `csrf_token` |
| GET | `/api/admin/v1/users` | 所有者 |
| POST | `/api/admin/v1/users` | 所有者；创建编辑 |
| PATCH | `/api/admin/v1/users/{id}` | 所有者；改密码 / 停用 |
| PUT | `/api/admin/v1/users/{id}/permissions` | 所有者；整体替换权限 |
| GET/POST | `/api/admin/v1/sites` | 所有者 |
| GET/POST | `/api/admin/v1/tokens` | 所有者；签发只返回一次明文 |
| DELETE | `/api/admin/v1/tokens/{id}` | 所有者；撤销令牌 |

错误格式固定为 `{"error":{"code":"...","message":"..."}}`，`code` 稳定（`unauthorized`、`forbidden`、
`not_found`、`conflict`、`invalid`、`rate_limited` ……），`message` 是中文。

### 认证与 CSRF

两条通道二选一：

* **Cookie 会话**（浏览器）：`cs_session`，`HttpOnly`、`SameSite=Lax`，HTTPS 时再加 `Secure`。
  绝对有效期 12 小时，空闲 2 小时；改密码、停用、退出都会让旧会话失效。
  库里只存令牌的 SHA-256。
* **访问令牌**（命令行 / 站点）：请求头 `Authorization: Bearer <id>.<secret>`，不走 Cookie，也不需要 CSRF。

Cookie 会话的所有写操作必须带 `X-CSRF-Token` 头，值取自 `POST /session` 或 `GET /me` 的 `csrf_token`。
校验放在依赖 `writer` 里，新增写接口自动受保护，不会漏。令牌通道跳过 CSRF 校验（没有浏览器环境，也就没有 CSRF 面）。

登录失败按「账号 + IP」限速：窗口 5 分钟内 5 次。计数放在进程内存，两个 API 进程各自计数；
密码用 Argon2id，单次尝试本身就有成本，这个额度够用。

### 权限

八项可授予能力：`asset.read`、`asset.upload`、`asset.update`、`asset.publish`、`asset.trash`、
`folder.manage`、`collection.manage`、`tag.manage`。所有者不读权限表，代码里视为拥有全部；
新编辑默认零权限。预设 `editor` 一次授予八项。检查入口只有一个：`deps.require_permission(name)`。

## 数据库

SQLite，单文件，`journal_mode=WAL`、`busy_timeout=5000`、`foreign_keys=ON`、`synchronous=NORMAL`、
`cache_size=-8000`、`mmap_size=0`、`temp_store=FILE`。这些 PRAGMA 写在
[`db.py`](src/ctrlstore/db.py) 的 `PRAGMAS` 里，每次新建连接时统一重放。

每个进程只保留一个连接（`pool_size=1, max_overflow=0`）：API 两个进程、worker 一个进程，
共三条连接，写锁竞争可预测。只有 API 和 worker 打开库文件，网站只走 HTTP。

表结构见 [models.py](src/ctrlstore/models.py)，迁移在 [alembic/versions](alembic/versions)：
`0001` 基线，`0002` 身份与会话六张表。「至多一个所有者」用部分唯一索引在库层强制，
不只靠应用代码。时间一律按 UTC 存不带时区的值。

迁移用 Alembic，`render_as_batch=True`（SQLite 改表必须批处理）。新增迁移：

```bash
uv run alembic revision -m "说明"
```

迁移只向前，空库可从头执行。

## 日志

JSON 行输出到 stdout，由 systemd 收进 journald。`log.py` 会丢弃命中
`password`、`secret`、`token`、`cookie`、`authorization`、`csrf`、`body` 等关键字的字段，
异常只保留类型与消息。日志里不写文件正文。

审计写在 `audit_logs`：登录成功与失败、退出、建账号、权限变更、停用、改密码、建站点、签发与撤销令牌。
登录失败的记录里只有用户名和 IP，不记密码。
