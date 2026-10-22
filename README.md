# CtrlStore

一台 Linux 服务器上的内容库：接收、存放、编目、发布图片、视频、Markdown、PDF、Word、Excel 和纯文本，
并向同一台机器上的网站提供稳定的读取方式。文件字节落在磁盘，SQLite 只保存索引、权限、发布状态与检索文本。

* 范围与决议：[plan/项目企划书.md](plan/项目企划书.md)
* 实施顺序：[plan/分步实现规划.md](plan/分步实现规划.md)

当前进度：**第 1–11 部分已实现**；管理端位于 `/admin/`，包含独立的文案字典页。

## 目录

```text
backend_ctrl/
  plan/                     企划书与分步实现规划
  src/ctrlstore/
    admin_ui/               管理端单页应用（HTML + CSS + JS，无需 Node 构建）
    config.py               配置分层与校验（含各类型大小上限）
    db.py                   SQLite 连接、PRAGMA、迁移执行
    models.py               数据模型（表结构）
    errors.py               统一错误码与响应
    audit.py                审计记录
    idempotency.py          幂等键
    health.py               健康判据（内存、磁盘）
    log.py                  JSON 日志
    main.py                 API 入口与路由挂载
    cli.py                  ctrlstore 命令行
    auth/                   密码、会话、令牌、权限、限速、业务操作、依赖
      passwords.py          Argon2id
      sessions.py           Cookie 会话（库里存哈希）
      tokens.py             访问令牌
      permissions.py        八项内容能力、两项文案能力与预设
      throttle.py           登录失败限速
      service.py            接口与命令行共用的业务规则
      deps.py               身份解析与权限检查的唯一入口
    storage/                存储层（对象键 + 本地文件系统）
      keys.py               storage_key 生成与校验
      local.py              分片写入、合并、落盘、公开链接、清理
    uploads/                上传
      inspection.py         文件头识别、类型一致性、像素与大小上限
      service.py            会话、分片、合并、直传、过期清理
      router.py             管理接口 /uploads
    folders/                目录树（物化路径、防成环）
      service.py router.py schemas.py
    tags/                   标签（名称唯一、slug 派生）
      service.py router.py
    assets/                 内容与版本
      markdown.py           front matter 与正文引用
      service.py            登记、元数据、版本替换与保留、引用维护
      publish.py            发布/撤回/归档/回收站/恢复 + 公开链接
      signing.py            短时签名地址
      download.py           私有下载（X-Accel-Redirect 或直接发送）
      router.py schemas.py  管理接口 /assets
    collections/            集合与站点授权
      service.py            集合 CRUD、排序、前缀匹配
      router.py schemas.py  管理接口 /collections、/sites/{id}/grants
    entries/                文案字典（独立于文件资产）
      validation.py         HTTP / CLI 共用校验
      service.py            草稿、发布快照、修订号、条数限制与审计
      router.py schemas.py  管理接口 /entries
    content/                内容处理
      markdown_render.py    Markdown 渲染与 HTML 消毒
    public_api/             公开接口 /api/public
      deps.py               站点身份与可见性判定（单一入口）
      payloads.py           公开响应形状
      collections.py assets.py files.py entries.py
    fsck.py                 一致性检查
    imaging/                缩略图与展示图
    jobs/                   任务队列与处理器（缩略图、抽取、视频、定时发布、清理）
    stats/                  访问统计与主面板数据
    search/                 FTS5 索引与检索
    content/                文本抽取、表格预览、视频探测
    worker.py               后台 worker 入口
    quota.py                字节与条数配额
    backup.py               备份清单读取（健康接口）
  alembic/                  迁移脚本（0001 基线 … 0011 文案字典）
  deploy/
    nginx/nginx.conf.example           worker_processes 2 的主配置
    nginx/ctrlstore.conf.example       站点片段
    systemd/                           API 模板、slice、worker
    Dockerfile / docker-compose.example.yml   可选容器跑法
  scripts/                  停写备份与恢复
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

## 管理端

管理端是 API 进程直接提供的单页应用，不需要 Node 构建，也没有独立开发服务器：

```text
http://127.0.0.1:8000/admin/
```

- 使用所有者或编辑账号登录；会话走 HttpOnly Cookie，所有写请求自动带 `X-CSRF-Token`。
- 已接入总览、内容库、内容详情、Markdown 编辑、分片上传、集合、站点与令牌、账号与权限、回收站、系统状态。
- 内容预览走 `/api/admin/v1/assets/{id}/preview`，卡片缩略图走 `/thumb`；都要求 `asset.read`，不把 `originals/` 暴露成静态目录。
- Markdown / 纯文本正文通过 `/assets/{id}/body` 读回当前版本；保存正文仍走 `PATCH /assets/{id}`，生成新版本。
- 编辑器将未保存的正文提交到 `POST /assets/{id}/render`，与公开 HTML 共用 Markdown 与 nh3 消毒；公开图片使用绝对原图地址，仅站点图片使用短时签名地址，不可用图片显示缺失标记。
- 大文件上传按 4 MiB 分片；临时失败自动重试两次，队列中的「重试失败分片」会保留会话并跳过已成功上传的分片。编辑保存遇到 409 会提示并重新加载最新正文与修订号。
- 系统状态里的后台任务与审计日志是只读视图；`fsck`、停写备份与恢复仍在服务器维护窗口用命令行执行。
- 首次使用先执行 `ctrlstore init` 创建所有者。管理端登录失败同样受账号 + IP 限速，失败会写入审计日志。

生产若在 Nginx 后面，`deploy/nginx/ctrlstore.conf.example` 已将 `/admin/` 反代到 API 进程。

界面总览：[docs/admin-ui/overview.png](docs/admin-ui/overview.png)。完整页面截图在 [`docs/admin-ui/`](docs/admin-ui/)。

## 文案字典

在管理端「文案」中按键维护短句，例如 `nav.about`。保存只改草稿，点击“发布已保存草稿”才更新网站读取的快照。所有者拥有全部能力；`entry.update` 可新建、修改并查看草稿，`entry.publish` 可审核、发布和撤回。预设 `editor` 仍是原有八项内容权限，`copywriter` 只含两项文案权限。

```bash
ctrlstore entry set nav.about --value '关于我们'
ctrlstore entry publish nav.about
curl 'http://127.0.0.1:8000/api/public/v1/entries/nav.about?language=zh-CN'
ctrlstore entry unpublish nav.about
```

值是纯文本，网站使用文本节点插入。站点令牌读取同键时，本站专属优先于库级；不会回退到其他语言。默认全库最多 2000 条，使用 `entry_limit` 配置。API、分页、缓存、站点隔离与开发说明见 [docs/entries.md](docs/entries.md)。

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
ctrlstore token create (--site 标识 | --user 用户名) [--name 备注] [--expires-days N] [--allowed-ip IP]
ctrlstore token list
ctrlstore token revoke <令牌 ID>

ctrlstore import <本地路径> [--asset-id <ID>]   # 从服务器路径导入；带 ID 则作为新版本
ctrlstore publish <内容 ID>                 # 命令行发布（所有者）
ctrlstore unpublish <内容 ID>               # 命令行撤回（所有者）
ctrlstore worker [--once]                   # 后台任务：缩略图、抽取、视频、定时发布、清理
ctrlstore jobs list [--status failed]       # 任务与最近一次错误
ctrlstore fsck [--fix-public-links] [--skip-hashes]   # 一致性检查
```

`init` 会提示输入两次密码；非交互环境加 `--password-stdin` 从标准输入读一行。
密码一律走标准输入，不进 shell 历史，也不出现在进程列表里。

`serve` 在前台跑一个 Uvicorn 进程。部署基线是 systemd 起两个 `ctrlstore serve` 进程 + 一个 worker，
不靠 Uvicorn 自带的 `--workers`，这样每个进程的配置、日志和资源上限都由 systemd 统一约束（第 9 部分给单元文件）。

`import` 与 HTTP 上传共用同一套校验和落盘逻辑：类型按文件头判定，超限直接拒绝，
导入后打印类型、大小、SHA-256、内容 ID 与存储键。加 `--asset-id` 可作为该内容的新版本导入。

```bash
ctrlstore import ./photo.jpg --asset-id <内容 ID>   # 替换成新版本
```

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
| POST | `/api/admin/v1/uploads` | 新建内容要 `asset.upload`；带 `asset_id` 替换要 `asset.update` |
| GET | `/api/admin/v1/uploads/{id}` | `asset.upload`；查会话状态与已收分片 |
| PUT | `/api/admin/v1/uploads/{id}/parts/{n}` | 随会话：新建用 `asset.upload`，替换用 `asset.update` |
| POST | `/api/admin/v1/uploads/{id}/complete` | 同上，完成时再查一次；支持 `Idempotency-Key` |
| DELETE | `/api/admin/v1/uploads/{id}` | 同上；放弃并删临时文件 |
| POST | `/api/admin/v1/uploads/direct` | 新建要 `asset.upload`；带 `asset_id` 要 `asset.update` |
| GET | `/api/admin/v1/folders` | `asset.read`；按物化路径排序 |
| POST | `/api/admin/v1/folders` | `folder.manage`；建目录 |
| PATCH | `/api/admin/v1/folders/{id}` | `folder.manage`；改名（子孙路径同步） |
| POST | `/api/admin/v1/folders/{id}/move` | `folder.manage`；移动（子孙跟着走） |
| GET | `/api/admin/v1/tags` | `asset.read`；`q` 按名称筛选 |
| POST | `/api/admin/v1/tags` | `tag.manage`；按名称取或建 |
| GET | `/api/admin/v1/assets` | `asset.read`；游标分页与筛选 |
| POST | `/api/admin/v1/assets` | `asset.upload`；直接创建 Markdown / 纯文本 |
| GET | `/api/admin/v1/assets/{id}` | `asset.read`；含全部保留版本 |
| PATCH | `/api/admin/v1/assets/{id}` | `asset.update`；改元数据、标签或正文，必须带 `revision` |
| POST | `/api/admin/v1/assets/{id}/publish` | `asset.publish`；立即发布，或带 `publish_at` 定时 |
| POST | `/api/admin/v1/assets/{id}/unpublish` | `asset.publish`；撤回，公开地址立即 404 |
| POST | `/api/admin/v1/assets/{id}/archive` | `asset.publish`；归档 |
| POST | `/api/admin/v1/assets/{id}/trash` | `asset.trash`；进回收站 |
| POST | `/api/admin/v1/assets/{id}/restore` | `asset.trash`；恢复，不自动公开 |
| DELETE | `/api/admin/v1/assets/{id}` | 所有者；彻底删除（须已在回收站） |
| GET | `/api/admin/v1/assets/{id}/file` | `asset.read`；任意状态下载原件（支持 Range） |
| GET | `/api/admin/v1/assets/{id}/preview` | `asset.read`；详情预览，优先派生图，图片可回退原图 |
| GET | `/api/admin/v1/assets/{id}/thumb` | `asset.read`；只读已生成的缩略图/封面，不拿原图代替 |
| GET | `/api/admin/v1/assets/{id}/body` | `asset.read`；读回 Markdown / 纯文本当前正文 |
| GET | `/api/admin/v1/assets/{id}/signed-url` | `asset.read`；给已发布内容签短时地址 |
| GET | `/api/admin/v1/collections` | `asset.read`；按别名排序 |
| POST | `/api/admin/v1/collections` | `collection.manage`；建集合（别名如 `home.hero`） |
| PATCH | `/api/admin/v1/collections/{id}` | `collection.manage`；改标题或类型限制 |
| GET/POST | `/api/admin/v1/collections/{id}/items` | `asset.read` / `collection.manage`；条目 |
| DELETE | `/api/admin/v1/collections/{id}/items/{asset_id}` | `collection.manage`；移除条目 |
| PATCH | `/api/admin/v1/collections/{id}/items/{asset_id}` | `collection.manage`；改这一处的展示标题 |
| PUT | `/api/admin/v1/collections/{id}/order` | `collection.manage`；整体重排，一次事务 |
| GET/POST | `/api/admin/v1/sites/{id}/grants` | 所有者；站点可读的集合前缀 |
| DELETE | `/api/admin/v1/sites/{id}/grants/{grant_id}` | 所有者 |
| GET | `/api/public/v1/collections/{slug}` | 公开；按可见性过滤条目 |
| GET | `/api/public/v1/assets/{id}` | 公开；`?format=markdown` 仅站点令牌 |
| GET | `/api/public/v1/by-slug/{slug}` | 公开；按别名取文章或文本 |
| GET | `/api/public/v1/assets/{id}/table` | 公开；xlsx 表名、表头与前 100 行 |
| GET | `/api/public/v1/files/{id}` | 匿名；凭签名读取，撤回后 404 |
| POST | `/api/public/v1/pageviews` | 站点令牌；页面访问上报，重复事件不重复计数 |
| GET | `/api/admin/v1/dashboard` | 所有者；今日、月份、近 12 个月与库存 |
| GET | `/api/admin/v1/dashboard/day` | 所有者；某日的次数与最多 10 条路径 |
| GET | `/api/admin/v1/search` | `asset.read`；三字以上全文，一两个字查标题/文件名/标签 |
| GET | `/api/admin/v1/jobs` | 所有者；最近后台任务，供系统状态页只读展示 |
| GET | `/api/admin/v1/audit` | 所有者；最近审计记录，供系统状态页只读展示 |
| GET | `/api/admin/v1/health` | 所有者；磁盘、队列、迁移版本与上次成功备份 |
| GET | `/api/public/v1/search` | 站点令牌且配置开启；只搜已发布且该站点能读的内容 |

替换文件没有单独接口：`POST /uploads` 或 `/uploads/direct` 带 `asset_id`，完成后追加为新版本。
新建与替换共用一条上传路径，避免两套登记规则。

错误格式固定为 `{"error":{"code":"...","message":"..."}}`，`code` 稳定（`unauthorized`、`forbidden`、
`not_found`、`conflict`、`invalid`、`too_large`、`rate_limited`、`insufficient_storage` ……），`message` 是中文。

### 认证与 CSRF

两条通道二选一：

* **Cookie 会话**（浏览器）：`cs_session`，`HttpOnly`、`SameSite=Lax`，HTTPS 时再加 `Secure`。
  绝对有效期 12 小时，空闲 2 小时；改密码、停用、退出都会让旧会话失效。
  库里只存令牌的 SHA-256。
* **访问令牌**（命令行 / 站点）：请求头 `Authorization: Bearer <id>.<secret>`，不走 Cookie，也不需要 CSRF。

Cookie 会话的所有写操作必须带 `X-CSRF-Token` 头，值取自 `POST /session` 或 `GET /me` 的 `csrf_token`。
校验放在依赖 `writer` 里，新增写接口自动受保护，不会漏。令牌通道跳过 CSRF 校验（没有浏览器环境，也就没有 CSRF 面）。

令牌可以绑定允许的来源 IP（`allowed_ip`，逗号分隔多个；空表示不限制）。解析令牌时先比对
`X-Forwarded-For` 的第一段，不匹配按无效令牌处理。命令行与站点令牌同一规则。

登录失败按「账号 + IP」限速：窗口 5 分钟内 5 次。计数放在进程内存，两个 API 进程各自计数；
密码用 Argon2id，单次尝试本身就有成本，这个额度够用。

### 权限

八项可授予能力：`asset.read`、`asset.upload`、`asset.update`、`asset.publish`、`asset.trash`、
`folder.manage`、`collection.manage`、`tag.manage`。所有者不读权限表，代码里视为拥有全部；
新编辑默认零权限。预设 `editor` 一次授予八项。检查入口只有一个：`deps.require_permission(name)`。

## 上传与存储

### 两条上传路径

| 场景 | 做法 |
| --- | --- |
| 小文件（100 MB 以内够用） | `POST /uploads/direct`，一次 multipart |
| 大文件、需要断点续传 | `POST /uploads` 建会话 → 逐片 `PUT .../parts/{n}` → `POST .../complete` |

直传不是另一套实现：它内部就是「建会话 + 写第 1 片 + 完成」，所以类型判断、上限、
哈希与落盘只有一份代码（`uploads/service.py`）。命令行 `ctrlstore import` 同样走这条路。

分片序号从 1 开始，完成时要求连续；同一序号重复提交就覆盖，客户端因此不需要额外协议。
`GET /uploads/{id}` 返回已收到的分片，供续传前确认进度。

未完成的会话 24 小时后作废。**到期在使用时结算**：读状态、写分片、合并、取消都会先比
`expires_at`，过期就当场清掉临时目录并标成 `expired`，再返回 409——不依赖「下一次有人建会话」
才会清理。建会话时另有一次批量清理，把其他用户遗留的过期会话一并收掉。

每名用户同时最多 2 个未完成会话，全库 4 个；已作废的会话不占额度。

`POST .../complete` 接受 `Idempotency-Key`：24 小时内同一键 + 同一请求返回第一次的结果，
换请求体则 409。直传没有稳定请求体，需要幂等时请用分片流程。

### 类型识别

只看文件头，不信浏览器申报（`uploads/inspection.py`）：

* 扩展名必须在允许名单里（`.jpg .jpeg .png .gif .webp .mp4 .webm .md .markdown .pdf .docx .xlsx .txt`）；
  HTML、SVG、zip、exe、旧版 `.doc/.xls` 都在这道关被挡下；
* 文件头判定出的类型必须与扩展名一致，否则 422；
* 客户端声明的 `Content-Type` 若存在且不是 `application/octet-stream`，也要一致；
  纯文本类只要求 `text/*`（`.md` 报 `text/plain` 很常见）；
* docx/xlsx 只读 ZIP 条目名判断，不解压内容，避免解压炸弹；
* 图片只读尺寸头，超过 4000 万像素拒绝，不会把整图解码进内存。

### 落盘与上限

```text
data/originals/aa/<asset_id>/v1/original.jpg   校验通过后的原件（同盘 os.replace，原子）
data/tmp/uploads/<session>/parts/000001.part   未完成的分片
```

`storage_key` 只由服务端生成：`aa` 取内容 ID 前两位，后接版本目录与固定文件名。
读取前再校验一次形状，`..`、反斜杠、绝对路径一律拒绝（`storage/keys.py`）。

单文件上限按类型配置（`[limits]`，默认图片 30 MB、视频 2 GB、文档 50/100 MB、文本 2 MB）。
磁盘剩余低于 10 GB 或低于总容量 5% 时，建会话与合并前都会拒绝（507 `insufficient_storage`），
健康接口同时标为 `degraded`。

### 完成上传之后

字节在校验后落到 `originals/aa/<asset_id>/v<n>/original.ext`，同时登记成内容：
`assets` 一行、`asset_versions` 一行，`current_version_id` 指向这一版。
新建内容此时是 `draft` + `private`，还没有公开地址——发布是第 5 部分的事。

## 内容与版本

### 目录

目录是给人浏览的树，与磁盘路径无关。内部一律用 `id`，`path` 是物化出来的展示路径
（`/资料/2026`，根是 `/`）。改名与移动在同一事务里把子孙的 `path` 前缀一起换掉，
所以列表排序与祖先判断都不需要递归查询。移动到自己或自己的子孙下面会被拒绝（422）。

同一父目录下不能重名：非根目录用组合唯一约束，根目录用部分唯一索引
（SQLite 的普通 UNIQUE 会把多个 NULL 当成不同值）。冲突返回 409。

### 标签

名称唯一，`slug` 由名称派生（空白与标点折叠成 `-`，中文原样保留；折叠后撞车自动加序号）。
内容打标签只给名字：不存在的标签当场创建，调用方不需要先建后关联。

两个不同写法指同一件事时，所有者用 `POST /api/admin/v1/tags/{id}/merge`（`{"into": "目标标签 ID"}`）
把内容整体改挂过去并删除来源标签；受影响内容的检索索引同步重建。不能合并到自己。

### 内容

七类内容都落在同一张 `assets` 表里，`kind` 区分。上传类内容由上传完成自动登记；
Markdown 与纯文本可以不经上传直接创建，也可以直接改正文——正文的每一次修改都写成新版本，
与上传共用同一套版本结构、保留规则与发布路径（企划第 8.4 节 T1）。

`PATCH /assets/{id}` 的 `body` 字段只对 Markdown 与纯文本有效（其他类型返回 422，请替换文件）：

* 新正文写入 `v(n+1)`，`current_version_id` 指过去，旧版本按保留规则淘汰；
* Markdown 的 front matter 重新映射到 `title`、`slug`、`summary`、`language`、`cover`、`tags`；
  同一请求里显式给出的字段优先，不会被正文里的 front matter 覆盖；
* 正文里引用的图片重新解析，`asset_references` 随之更新。

编辑中的字段（`title`、`summary`、`description`、`alt`、`language`、`slug`、`folder_id`、
`cover_asset_id`、`visibility`）与发布快照（`published_*`）分列。第 4 部分只写前者，
`published_version_id` 恒为空；PATCH 也不能写 `published_*`（未知字段直接 422，
不会被静默忽略）。

**Markdown 的 front matter** 会映射到内容字段：`title`、`slug`、`excerpt`/`summary`、
`tags`、`cover`（可写内容 ID、`/p/<id>/...` 或 `asset:<id>`）。正文里引用的本库图片
解析成内容 ID 记进 `asset_references`；外站地址与不存在的 ID 都不落库，等第 6 部分渲染时再处理。

**封面** 只允许指向图片，否则 422。

### 版本与权限

替换文件 = 上传一个新文件并把它挂到已有内容上（`POST /uploads` 带 `asset_id`），
完成后追加 `v(n+1)` 并把 `current_version_id` 指过去。

**替换属于修改，不是上传**：建会话、写分片、完成、取消这几步都按会话是否带 `asset_id`
判定所需能力——带 `asset_id` 要 `asset.update`，不带才要 `asset.upload`。
只有上传权限的编辑可以创建新内容，但改不了已有文件的版本；完成时再查一次，
所以会话建好之后被收回权限也挡住。

`published_version_id` 不动，所以**没有发布权限的人替换文件，改不了访客看到的内容**
（第 5 部分的发布才切换线上版本）。

保留规则：图片与文档留最近 5 版，视频留 2 版；`current_version_id` 与
`published_version_id` 指向的版本永远保留，即使它的版本号更旧。

`revision` 是乐观锁：修改元数据必须带 `revision`，不一致返回 409。一次请求（无论改几个字段、
是否同时改标签）只加 1；替换版本也加 1。

## 发布与公开区

### 可见性矩阵

发布不等于公开：内容默认 `private`。只有「已发布 + `public` 或 `unlisted`」才会在
`public/` 下出现文件（企划第 12.7 节）。

| 状态 + 可见性 | `/p/` 当前版 | 管理下载 |
| --- | --- | --- |
| 草稿 / 任意 | 无 | 有 |
| 已发布 + private | 无 | 有 |
| 已发布 + site | 无（只能签名地址） | 有 |
| 已发布 + unlisted | 有 | 有 |
| 已发布 + public | 有 | 有 |
| 回收站 | 无 | 有 |

### 公开区布局

```text
public/<asset_id>/original.png          当前发布版（硬链接，地址 /p/<id>/original.png）
public/<asset_id>/v/<n>/original.png    同一版本（地址 /p/<id>/v/<n>/original.png，可长期缓存）
public/<asset_id>/thumb.webp            已就绪的缩略图（地址 /p/<id>/thumb.webp）
```

缩略图只有在该版本已就绪时才出现；没有的文件不会在公开 JSON 里给地址。
派生文件的挂载规则见「后台任务与派生」一节。

扩展名随该版本的**真实类型**变化：换过文件类型之后，旧扩展名的地址必须消失，
所以每次发布都会先清空该内容在 `public/` 下的目录，再重建应有的链接（原图 + 已就绪的派生）。
同盘用硬链接；原件区与公开区不在同一块盘时退化为复制，健康检查会标 `degraded`
（`public_not_same_filesystem`）。

**公开区永远指向「已发布版本」，不是「当前版本」。** 发布之后又替换了文件时，两条指针
指向不同的行：线上仍是旧版，公开区、签名地址、`fsck`、改可见性都必须按 `published_version_id`
判断。这个选择集中在 [`publish.desired_links`](src/ctrlstore/assets/publish.py) 与
`publish.published_version`，调用方不传版本号——传错一次就会把仍在线上的一版判成"该删"。

### 状态变化与失败落点

| 动作 | 权限 | 做的事 |
| --- | --- | --- |
| 发布 | `asset.publish` | 校验版本与引用 → 提交快照与状态 → 建链接 |
| 撤回 / 归档 | `asset.publish` | 删链接 → 状态回 `draft` / `archived`，清空快照 |
| 转私有 / 转公开 | `asset.publish`（已发布内容改可见性） | 按新可见性增删链接 |
| 进回收站 | `asset.trash` | 删链接 → 标 `trashed` |
| 恢复 | `asset.trash` | 只改状态，**不自动重新公开** |
| 彻底删除 | 仅所有者 | 须已在回收站；被别的内容当封面（编辑中或已发布）、或被任何集合包含时拒绝并列出引用 |

失败一律落在「更不可访问」这一侧：发布时先提交状态再建链接（链接失败 = 公开地址 404，
不会泄露未发布内容）；撤回时先删链接再提交状态（提交失败也只是 404）。残留的链接由
`fsck` 收掉。

再次发布不再先清空目录：新文件逐个以临时名 + `os.replace` 原子替换，旧的扩展名/版本号
最后才删除，读者不会在重建期间看到 404。撤回、归档、进回收站仍整目录清掉。

`POST …/publish` 与 `POST …/unpublish` 接受 `Idempotency-Key`：同一密钥、同一请求体
24 小时内返回第一次的结果，不再增加修订号，也不重复改公开区；同一密钥换了请求体返回 409。
命令行 `ctrlstore publish` / `unpublish` 直接改状态，不需要幂等键。

`published_version_id` 非空当且仅当当前处于已发布状态：撤回、归档、进回收站都会清空
发布快照，公开查询因此只需要判断一个条件。

### 引用检查

发布文章时，正文引用的图片必须**已发布**，否则 409 并列出缺失的内容 ID。
图片之后被撤回时，文章仍保持发布——第 6 部分渲染时会去掉那张图。

### 私有下载与短时签名

`GET /assets/{id}/file` 鉴权后由 Nginx 用 `X-Accel-Redirect` 发送（支持 Range）；
`originals/` 在 Nginx 里只配 `internal` location，外部 URL 进不去。
开发环境没有 Nginx 时把 `x_accel_redirect` 设为 `false`，由进程直接发送。

「仅站点」内容不进入 `/p/`，网站后端用签名地址取字节：

```bash
# 站点后端（或所有者）先换一个短时地址
GET /api/admin/v1/assets/{id}/signed-url?ttl=300
# => {"url": "http://…/api/public/v1/files/<id>?version=<vid>&expires=<ts>&sig=<hmac>", ...}

# 再按这个地址取字节；签名过期、内容撤回或版本被替换之后一律 404
curl -o poster.jpg "http://…/api/public/v1/files/<id>?version=…&expires=…&sig=…"
```

签名绑定「内容 ID + 已发布版本 ID + 过期时间」，默认 300 秒、最长 1 小时。
撤回或替换之后旧签名自然失效，不需要吊销表。签名密钥由 `pageview_hmac_secret`
派生（`hmac(secret, "ctrlstore/file-signing/v1")`），不额外增加配置项。

### fsck

```bash
ctrlstore fsck                      # 只报告
ctrlstore fsck --fix-public-links   # 按已提交状态增删公开链接
ctrlstore fsck --skip-hashes        # 文件多时跳过哈希比对
```

检查六项：记录有而文件缺失、文件有而记录缺失、应公开却缺链接、公开区多余链接、
哈希或大小不一致、残留的 `.tmp` 链接。`--fix-public-links` **只**动公开区，
不删任何原件，也不会把已撤回或回收站里的内容重新公开。发现问题时退出码为 1。

## 集合与网站读取

### 集合

```bash
POST /api/admin/v1/collections              {"slug": "home.hero", "title": "首页主视觉"}
POST /api/admin/v1/collections/{id}/items   {"asset_id": "…", "caption": "秋季主图"}
PUT  /api/admin/v1/collections/{id}/order   {"asset_ids": ["…", "…"]}
```

别名全局唯一、只能用小写字母/数字/点/连字符；同一集合里同一内容只出现一次；
位置在集合内唯一，所以重排先把旧值挪到偏移区、再写 0..n-1，整次事务提交——
顺序要么全对，要么完全不变。`allowed_kinds` 可以限制集合只收某几类内容，
收紧限制时会先确认现有条目都符合，否则 409。

### 站点授权与前缀

```bash
POST /api/admin/v1/sites/{id}/grants  {"prefix": "home", "can_read_unlisted": false}
```

前缀按**点号分段**匹配：`home` 覆盖 `home.hero`，不覆盖 `homepage`。
`can_read_unlisted` 决定该站点的集合列表里要不要出现「未列出」内容。

### 公开读取与可见性

| 可见性 | 匿名 | 站点令牌 |
| --- | --- | --- |
| `public` | 可读，给 `/p/` 原图地址 | 可读，给 `/p/` 原图地址 |
| `unlisted` | 知道完整地址可读，集合不列出 | 可读；集合里列出需 `can_read_unlisted` |
| `site` | 不可读 | 可读（须在已授权集合里），只给短时签名地址，不给 `/p/` |
| `private` / 草稿 / 归档 / 回收站 | 不可读 | 不可读 |

判定只有两个函数：[`deps.can_read_asset`](src/ctrlstore/public_api/deps.py)（单条）与
`deps.visible_in_collection`（列表）。新增读取入口必须经过它们，否则迟早漏一格。

集合本身能不能读由**归属**决定：库级集合（无 `site_id`）对所有调用方开放，
属于某站点的集合只对那个站点开放。站点授权只决定内容可见性（`site` 与未列出），
不参与集合可读性——两件事分开，语义才不打架。

带令牌但令牌无效返回 401，不降级成匿名——否则「令牌用错」会显示成「内容不存在」。

### 响应与缓存

```json
{
  "slug": "home.hero",
  "title": "首页主视觉",
  "updated_at": "2026-10-20T08:00:00Z",
  "items": [
    {"position": 0, "caption": "秋季主图",
     "asset": {"id": "…", "kind": "image", "title": "主视觉", "mime": "image/png",
               "width": 2400, "height": 1350,
               "urls": {"original": "http://…/p/…/original.png"}}}
  ]
}
```

* 只读发布快照：标题、摘要、标签、封面、别名、正文、正文图片都只在发布时更新。
  编辑中的 `slug`、`asset_references` 与标题改了不影响线上。
* `urls.original` 只在内容真的进了 `/p/` 时出现；「仅站点」内容给 `urls.signed`
  （绑定内容 + 已发布版本 + 过期时间，撤回后自动失效）。私有内容两者都没有。
* 缩略图/展示图要等对应派生文件 `ready` 才出现（第 7 部分）。
* 地址用 `public_base_url` 拼绝对地址，换域名只改配置。
* 匿名响应 `Cache-Control: public, max-age=60` 并带弱 `ETag`，`If-None-Match` 命中返回 304；
  带站点令牌的响应 `private, no-store`。
* 集合的 `updated_at` 与 `ETag` 只由本次**可见条目**的发布时间决定。

### Markdown 渲染

`content/markdown_render.py`：Markdown → HTML → `nh3` 消毒 → 只留能解析成功的图片。

* 脚本、内联事件、`javascript:` 一律去掉；允许的标签与属性是白名单。
* 文内图片只能来自本库：外站图片整张去掉；`asset:<id>`、`/p/<id>/…`、带域名的
  `/p/<id>/…` 三种写法都收敛到同一个绝对地址。
* 图片被撤回或未发布时，这张图从 HTML 里去掉，文章照常可读。
  能被调用方读取的「仅站点」图片则换成短时签名地址——授权站点看到图，匿名看不到。
* **只有图片受上面两条约束**。`[手册](/p/<id>/original.pdf)` 是下载链接，不要求目标
  是已发布图片（否则链接一份手册都会被判成"引用了未发布图片"）；链接里的 `/p/`
  地址同样补成绝对地址。
* 代码块与行内代码里的 `![…](asset:…)` 是示例文字，不算引用。
* front matter 不进正文——它已经映射到 JSON 的 `title` / `slug` / `summary` / `tags`。
* `?format=markdown` 只对站点令牌开放，匿名只拿 HTML。

引用规则分两层：「是不是图片」由 `assets/markdown.image_ids` 在解析阶段判定；
「这张图此刻能不能读」由 `public_api.deps.can_read_asset` 在渲染时判定。

### 纯文本的 HTML

纯文本**不经过 Markdown**：先做 HTML 转义，再包进 `<pre>`，最后照常过 `nh3` 消毒。
所以正文里的 ```、`#`、`[x](y)` 都是字面量，不会被当成语法；`<script>` 仍然被转义成文本。

它和文章用同一套字段（`body.format` + `body.html`），网站不需要为公告写第二种渲染分支；
需要原文时用 `?format=markdown` 取纯文本（字段名沿用既有约定，内容就是原文）。

## 后台任务与派生

一个 worker 进程从 `jobs` 表领取任务，同时只做一件事。它与 API 分进程运行，
缩略图卡住时不会挡住登录与发布；启动方式是独立的 `ctrlstore worker`（systemd 单元在第 9 部分给出）。

### 队列

* 领取用 `BEGIN IMMEDIATE`：取出 `pending` 并改成 `running` 在同一事务里。即使误起了两个
  worker，也只有一个能领到同一行。
* 租约 15 分钟、最多 3 次。worker 中途被杀时，租约过期后任务可以再领；领满 3 次仍失败就停在
  `failed`，不无限重启。
* 失败只写 `jobs.last_error`，不改已提交的业务结果。`ctrlstore jobs list --status failed` 是所有者
  查看它们的地方。
* 周期任务也是普通任务：`publish.due` 每分钟、`maintenance.cleanup` 每小时各补一条；同类已有
  待处理或处理中的就不重复补，空转不会把队列堆满。
* 低内存时，领取查询跳过四类解码任务，按 `created_at, id` 领取其他任务，定时发布和清理继续执行。
  被跳过的任务保持 `pending`，不改创建时间、不占租约、不消耗尝试次数；只剩解码任务时休眠
  2 秒再检查内存，不反复领取再退回。内存恢复后按原顺序继续。
* 领取后、执行前会再检查内存。此时才发现不足则退回任务并还原本次尝试计数，同轮再尝试
  一条非解码任务。任务分类统一放在 `jobs.service.MEMORY_REQUIRED_KINDS`，新增解码类型时
  同步登记，领取筛选与执行前检查共用这份分类。

| 类型 | 作用 |
| --- | --- |
| `image.thumb` | 缩略图：最长边 480 的 WebP |
| `image.display` | 展示图：最长边 1600 的 WebP，随缩略图一起入队 |
| `content.extract` | 文档/文本抽取、xlsx 预览、PDF 首页图 |
| `video.inspect` | ffprobe 判断能否播放，ffmpeg 截封面 |
| `publish.due` | 把到点的定时发布送出去 |
| `maintenance.cleanup` | 过期上传、回收站到期、访问明细、旧任务记录 |

### 缩略图

图片入库（含替换）后立刻登记一条 `pending` 的派生记录并排任务；发布**不等**它完成。
派生文件写在 `derivatives/aa/<id>/vN/thumb.webp`，公开地址固定为 `/p/{id}/thumb.webp`。
生成失败只把这条记录标成 `failed`：原图照常发布，响应里没有 `thumb` 字段。

公开区只补**已经应该在 `/p/` 的那一版**：已发布、可见性为公开或未列出、且版本就是
`published_version_id`。仅站点、私有、草稿与待发布的替换都不挂缩略图。

* 发布时缩略图已就绪：随原图一起挂出；
* 还没就绪：公开响应先只给 `urls.original`，worker 完成后再补 `urls.thumb`；
* 撤回、归档、改为私有或进回收站：整个目录（含缩略图）一起摘掉。

派生文件不 `ready` 就不给 URL；响应里每一条公开地址在返回前都会确认文件确实存在，
不会给出一个必然 404 的地址。

### 定时发布

`POST /api/admin/v1/assets/{id}/publish` 带未来的 `publish_at` 时只登记计划：不写快照、
不进公开区。到点后由 `publish.due` 按第 5 部分的同一顺序执行——先提交快照，再按可见性挂链接。

已发布内容也可以定时更新：到点用**当前版本**重新发布，线上在那之前保持旧版本。手动发布、
撤回、归档、进回收站都会取消定时。

发不出去（例如正文引用了未发布图片）会取消定时并写 `asset_publish_failed` 审计，
不会每分钟重试一次。

### 保留与清理

* 图片与文档留最近 5 个版本，视频留待发布版与已发布版（`version_retention` /
  `video_version_retention`）。两个指针占用的版本不计入上限。淘汰版本时它的派生目录一起删。
* 回收站默认保留 30 天（`trash_retention_days`），到期由 `maintenance.cleanup` 彻底删除；
  仍被封面或集合引用时跳过，等所有者先解除引用。
* 超过 24 小时未完成的上传会话、过旧的已完成任务记录、400 天前的访问明细也在同一个
  任务里清理（日汇总不动）。

### 验收

```bash
ctrlstore import ./hero.png          # 图片入库：自动排缩略图，内容仍是草稿
ctrlstore worker --once              # 生成 480 像素 WebP，派生状态变 ready
ctrlstore jobs list                  # 应看到 image.thumb 为 done

# 在管理接口把可见性设为 public 再发布（见「接口」一节），然后读公开 JSON
curl -s http://127.0.0.1:8000/api/public/v1/assets/<内容 ID>
# urls 同时有 original 与 thumb；浏览器打开 <public_base_url>/p/<内容 ID>/thumb.webp
# 得到最长边 480 的 WebP，原图地址仍是上传时的原清晰度文件

# 如果先发布、后跑 worker，链接会在 worker 完成时补上：
curl -s http://127.0.0.1:8000/api/public/v1/assets/<内容 ID>   # 这时只有 original
ctrlstore worker --once
curl -s http://127.0.0.1:8000/api/public/v1/assets/<内容 ID>   # 多了 thumb
```

## 访问统计与主面板

### 口径

* 日历按 `Asia/Shanghai`，一天从当地 00:00 到次日 00:00；库内时间仍是 UTC。
* **独立访客**：同一个访客标识在一个自然日内计 1 人。标识是网站发给浏览器的随机号，
  不是账号，也不是 IP。
* **浏览次数**：一次真实的页面打开计 1 次。同一人当天看三页，人数是 1、次数是 3。
* **近 12 个月**是「每日独立访客按月相加」，不是整月去重后的人数。
* **未上报与上报后当天为 0 分开显示**：前者 `reported=false`，给出「尚未收到网站上报」，
  数字留空；后者是真实的 0。

### 上报

`POST /api/public/v1/pageviews`，**只接受站点令牌**。访客浏览器不持有令牌：事件由
网站服务器转发，正文是 `event_id`、`visitor_id`、`path`，可选 `user_agent`。

| 情况 | 结果 |
| --- | --- |
| 正常页面打开 | `counted: true` |
| 同一 `event_id` 在 24 小时内重试 | `counted: false`，`reason: duplicate` |
| 预取 / 预渲染（`Sec-Purpose`、`Purpose`、`X-Purpose`、`X-Moz`） | `prefetch` |
| 爬虫、社交预览、合成监控的 UA | `bot` |
| `/api`、`/p`、`/admin`、`/healthz`、`/_internal`、带查询串或片段的路径 | `ignored_path` |
| 路径不以 `/` 开头、含 `..`、绝对外链或超过 512 字符 | 422 `invalid` |

响应固定为 `{"counted": true|false, "reason": …}`，带 `Cache-Control: no-store`。网站仍按
「统计失败不影响页面」处理：超时后最多重试两次，不阻塞渲染。

没有补报入口：日期只取服务器**收到请求**的时间，请求里带 `date` 之类的字段会被忽略。

限速按站点、每分钟 `pageview_rate_per_minute`（默认 600）计；与登录限速一样计数放在
进程内存，两个 API 进程各自计数——**实际全局额度约为配置值的两倍**。额度远高于正常
转发量，只用来挡刷量。

### 存了什么

* `pageview_daily`：站点 + 上海日期的人数与次数，长期保留，是日历的历史。
* `pageview_events`：24 小时内的 `event_id`，用来去重。
* `pageview_visitors`：当日已计过人数的 HMAC；**原始标识、IP、查询串都不入库**。
* `pageview_paths`：按路径的次数；一天超过 500 条不同路径时，其余并入「其他」。
  被并入的路径次数另记在 `pageview_merged_paths`；它再次出现时可以恢复成独立一行，
  不会永远沉在「其他」里。

三张明细表保留 400 天（含今天），由 worker 的 `maintenance.cleanup` 删除；删明细不改
已经写好的日汇总。访客 HMAC 用 `pageview_hmac_secret`，**不要在某一天中途更换密钥**，
否则同一天的人会被算成两批。访问上报量大且含访客行为，因此不进审计日志。

统计口径需要时区数据：仓库的依赖里有 `tzdata`（Debian 13 自带系统时区，
Windows 开发与测试靠这个包）。

### 主面板

`GET /api/admin/v1/dashboard?month=YYYY-MM`，**仅所有者**：

* `today`：今日人数、次数，以及与昨日的差（`uv_delta`、`pv_delta`）；
* `month.days`：整月每日人数与次数，没有记录的日子为 0；
* `months`：近 12 个月（含本月）的「每日独立访客按月相加」；
* `inventory`：七类内容的条数、已发布条数、当前版本字节、含历史版本字节；页脚是
  全库条数、总占用、回收站条数与磁盘剩余。

没有上报记录时返回 `reported: false` 与 `message`，`today` 为 `null`、`month.days` 与
`months` 为空；库存不依赖上报，照常计算。

`GET /api/admin/v1/dashboard/day?date=YYYY-MM-DD` 返回当天的人数、次数与最多 10 条路径，
供日历点开某一天时用。编辑访问这两个接口返回 403。

### 网站侧接入

浏览器在首次打开和客户端路由切换时把事件发给 Next.js 自己的接口，再由网站服务器带
站点令牌转发；站点令牌只放在网站服务器的环境变量里。预取、`/api` 与 `/p` 请求不上报。
网站侧的访客标识可以放在自己的 Cookie 里（例如 `cs_vid`），CtrlStore 只收到它的 HMAC。

## 文档抽取与表格预览

文档与文本在入库时排入 `content.extract`；抽取失败不影响原件，发布也不等它完成。

| 类型 | 抽取结果 |
| --- | --- |
| `docx` | 段落与表格文本（`python-docx`，只读） |
| `xlsx` | 表名、表头、每表前 100 行（`openpyxl` 只读模式）+ 预览 JSON |
| `pdf` | 逐页文本，最多 500 页（`pypdf`）；有 `pdftoppm` 时另存首页图 |
| `markdown` / `text` | 文件本身就是正文 |

固定上限：解压后 ≤ 200 MB、压缩包条目 ≤ 2000、单文件处理 ≤ 60 秒、PDF ≤ 500 页、
Excel ≤ 20 表 × 100 行 × 50 列、抽取文本 ≤ 20000 字符（超出截断并标记）。
超限、加密、损坏、没有文本都属于确定性结果，记 `failed` 或 `skipped` 后结束任务，
**不重试**——重试解决不了这些问题，只会堵住后面的任务。60 秒上限除循环检查外，
Linux 上还用 `SIGALRM` 给单次库调用一个硬超时；ZIP 另查压缩比，防中央目录谎报大小。

`GET /api/public/v1/assets/{id}/table?sheet=0&limit=100` 读已发布版本的预览：

```json
{"status": "ready", "sheets": ["预算"], "name": "预算",
 "header": ["项目", "金额"], "rows": [["服务器", "1200"]], "truncated": true}
```

预览还没生成时同样返回 200，`status` 为 `pending` 或 `failed`，不返回 404。
预览 JSON 存在 `derivatives/<id>/vN/preview.json`，**不进入 `/p/`**。

公开 JSON 另外补了已发布版本的 `filename`（下载按钮用），以及 `playback`、
`duration_ms`、`page_count`、`extract_status`。文件名只出现在响应里，不写进 `/p/` 路径。

## 检索

管理端与站点各用一份索引行，存于 SQLite FTS5（trigram 分词，不装中文分词插件）：

* `admin`：编辑中的标题、文件名、别名、标签与**当前版本**正文；
* `published`：发布快照与**已发布版本**正文。

`GET /api/admin/v1/search?q=…`（`asset.read`）三字及以上查全文，一两个字只查
标题/文件名/标签，最多 50 条；回收站在取前 50 条之前就排除。

`GET /api/public/v1/search?q=…` 默认关闭（`site_search_enabled = false`），打开后
必须带站点令牌，并且：

1. 只查 `published` 索引行——草稿、待发布替换、未授权正文都不在其中；
2. 在 SQL 里先按可见性收窄（公开可读；仅站点必须在已授权集合里；未列出不出现），
   再取前 50 条；结果最后仍逐个过 `searchable` 复核。这样私有的已发布内容不会
   占满名额，把后面的公开文章挤掉。

索引在内容变更时整体重建（标题、标签、正文、版本、发布状态、抽取完成），
所以不存在「改了标题还能搜到旧值」的中间状态。升级时迁移会按现有内容回填一次；
但在第 9 部分之前入库、从未抽取过的文档没有正文可回填，需要重新导入一版
（`ctrlstore import --asset-id`）才会进入全文索引。

## 视频

不转码、不改写原件。有 `ffprobe` 时判断能否直接播：

* MP4：H.264（音频 AAC 或没有音频）→ `playable`；
* WebM：VP8 / VP9 / AV1（音频 Opus、Vorbis 或没有音频）→ `playable`；
* 其余 → `download_only`，仍保存原件，只承诺可下载。

有 `ffmpeg` 时截一张封面（`-threads 1`，宽度 1280），写成 `poster` 派生文件；
没有就跳过。封面的公开链接规则与缩略图相同：只有该版本已经在 `/p/` 时才挂出。
公开地址的扩展名与容器一致（`.mp4` / `.webm`）；仅站点视频仍走签名地址，
不会因为探测完成而变成 `/p/*.mp4`。

## 配额

用户与全库各有两条配额，默认 0 表示不限：

* 字节：`user_quota_bytes` / `library_quota_bytes`；
* 条数：`user_quota_items` / `library_quota_items`。

字节计入未彻底删除的所有版本（含回收站）与所有派生文件（缩略图、展示图、封面、
预览 JSON）；条数只按内容计，一条内容不论多少版本都算 1。公开区硬链接不是新字节，
不重复计；临时分片不在库里，只受磁盘阈值约束。

**计账主体是内容所有者**：编辑替换他人内容时，新版本仍记在原所有者名下。
上传在开会话时按类型上限预估一次、合并时按真实字节再检查一次；直接改 Markdown /
纯文本正文走同一条检查，且在落盘之前。超限返回 507 `insufficient_storage`。

## 运维：备份、恢复与 systemd

### 备份

`scripts/backup.sh` 是停写备份，必须在维护窗口以 root 运行：

```bash
sudo CTRLSTORE_DATA_DIR=/var/lib/ctrlstore \
     CTRLSTORE_BACKUP_DIR=/var/backups/ctrlstore \
     scripts/backup.sh
```

它会停掉两个 API 实例与 worker，做 WAL checkpoint、SQLite `.backup`，在同一窗口复制
`originals/`、`derivatives/`、配置与 `secrets.env`，最后写 `manifest.json`（应用版本、
迁移版本、起止时间、文件数）。密钥文件保持 0600，且不进入版本库。
只要开始停服务，退出钩子就会尝试把服务拉起来——`systemctl stop` 失败时也一样；失败
目录会被改名成 `*.failed`，不能当恢复源。`public/` 与 `tmp/` 不备份。备份目录应在另一块盘上。

### 恢复

```bash
sudo scripts/restore.sh /var/backups/ctrlstore/20261021-030000
```

只接受 `status=complete` 的清单。恢复数据库、文件、配置与密钥，修正属主，按已提交的
发布快照执行 `ctrlstore fsck --fix-public-links`，再启动服务。恢复与升级分开：先回到清单
上的应用版本，核对集合 API、原图、视频、权限与版本，确认后再单独升级。
`published_slug` 与 `published_references` 在数据库里，随 SQLite 备份走，恢复脚本
不会用编辑中的 `slug` 或 `asset_references` 重写它们。

### systemd

`deploy/systemd/` 下三个文件：

```bash
sudo install -m 0644 deploy/systemd/ctrlstore-api.slice \
    deploy/systemd/ctrlstore-api@.service deploy/systemd/ctrlstore-worker.service \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ctrlstore-api.slice ctrlstore-api@0 ctrlstore-api@1 ctrlstore-worker
```

`ctrlstore-api@0` 监听 8000，`@1` 监听 8001，两者同属 `ctrlstore-api.slice`，
合计内存上限 512M；worker 单独一个进程，`MemoryMax=768M`。密钥放
`/etc/ctrlstore/secrets.env`（`VAR=value`，0600），systemd 用 `EnvironmentFile` 读。

Nginx 全局的 `worker_processes 2` 写在主配置里（`deploy/nginx/nginx.conf.example`），
不要放进 `sites-available` 片段；站点片段的上游已指向 8000/8001。

仓库同时提供可选容器跑法：`deploy/Dockerfile` 与 `deploy/docker-compose.example.yml`
（一个 API 容器 + 一个 worker 容器）。数据目录用 volume 共享，`public/` 仍由宿主机
Nginx 直接 alias；容器不是默认部署方式，systemd 仍是主路径。

### 健康

`GET /healthz` 仍然只返回 `{"status":"ok"|"degraded"}`。所有者的
`GET /api/admin/v1/health` 另给磁盘剩余/总量、队列状态计数、应用与迁移版本、
上次成功备份（读 `backup_dir` 下最新完整清单）。

可用内存低于 `min_available_memory_bytes`（默认 1 GB）时：新上传、导入与合并返回 507；
队列里的解码类任务（缩略图、展示图、抽取、视频探测）保持 `pending`，不开始执行、
不消耗重试次数，内存恢复后继续。定时发布与清理照常执行；读取、撤回与已发布文件的
公开访问不受影响。`ctrlstore worker --once` 做完当前可执行的任务即退出，等待内存的任务留在队列。

## 数据库

SQLite，单文件，`journal_mode=WAL`、`busy_timeout=5000`、`foreign_keys=ON`、`synchronous=NORMAL`、
`cache_size=-8000`、`mmap_size=0`、`temp_store=FILE`。这些 PRAGMA 写在
[`db.py`](src/ctrlstore/db.py) 的 `PRAGMAS` 里，每次新建连接时统一重放。

每个进程只保留一个连接（`pool_size=1, max_overflow=0`）：API 两个进程、worker 一个进程，
共三条连接，写锁竞争可预测。只有 API 和 worker 打开库文件，网站只走 HTTP。

表结构见 [models.py](src/ctrlstore/models.py)，迁移在 [alembic/versions](alembic/versions)：
`0001` 基线，`0002` 身份与会话，`0003` 上传与幂等键，`0004` 目录、标签、内容与版本，
`0005` 集合与站点授权，`0006` 发布快照补别名与图片引用，`0007` 后台任务、派生文件与定时发布，
`0008` 访问统计（日汇总、事件、访客与路径），`0009` 全文检索（FTS5）与派生文件字节，
`0010` 令牌来源 IP 与路径合并账本。
「至多一个所有者」「同一父目录下不重名」「同一内容版本号唯一」「同一集合内同一内容只出现一次、
位置唯一」「已发布别名唯一」都由库层约束兜底，不只靠应用代码。
时间一律按 UTC 存不带时区的值。

迁移用 Alembic，`render_as_batch=True`（SQLite 改表必须批处理）。新增迁移：

```bash
uv run alembic revision -m "说明"
```

迁移只向前，空库可从头执行。

## 日志

JSON 行输出到 stdout，由 systemd 收进 journald。`log.py` 会丢弃命中
`password`、`secret`、`token`、`cookie`、`authorization`、`csrf`、`body` 等关键字的字段，
异常只保留类型与消息。日志里不写文件正文。

审计写在 `audit_logs`：登录成功与失败、退出、建账号、权限变更、停用、改密码、建站点、签发与撤销令牌，
上传的开始、完成、取消与失败，建目录、改名、移动、建标签、内容登记、元数据修改、版本追加与淘汰，
以及发布、撤回、归档、回收站、定时发布登记与定时发布失败。
登录失败的记录里只有用户名和 IP，不记密码；上传与内容记录里只有类型、字节数与哈希，不记文件正文。
