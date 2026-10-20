# CtrlStore

一台 Linux 服务器上的内容库：接收、存放、编目、发布图片、视频、Markdown、PDF、Word、Excel 和纯文本，
并向同一台机器上的网站提供稳定的读取方式。文件字节落在磁盘，SQLite 只保存索引、权限、发布状态与检索文本。

* 范围与决议：[plan/项目企划书.md](plan/项目企划书.md)
* 实施顺序：[plan/分步实现规划.md](plan/分步实现规划.md)

当前进度：**第 5 部分（发布与公开文件）已完成**。公开读取 API（集合、文章）、缩略图、管理页面尚未实现。

## 目录

```text
backend_ctrl/
  plan/                     企划书与分步实现规划
  src/ctrlstore/
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
      permissions.py        八项能力与语义
      throttle.py           登录失败限速
      service.py            接口与命令行共用的业务规则
      deps.py               身份解析与权限检查的唯一入口
    storage/                存储层（对象键 + 本地文件系统）
      keys.py               storage_key 生成与校验
      local.py              分片写入、合并、落盘、清理
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
      download.py           私有下载（X-Forwarded 或直接发送）
      router.py schemas.py  管理接口 /assets
    public_api/             公开接口 /api/public
      files.py              签名下载
    fsck.py                 一致性检查
    content/ imaging/       后续部分按此分层填充
  alembic/                  迁移脚本（0001 基线 … 0004 内容）
  deploy/
    nginx/ctrlstore.conf.example
    systemd/                第 9 部分
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

ctrlstore import <本地路径> [--asset-id <ID>]   # 从服务器路径导入；带 ID 则作为新版本
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
| POST | `/api/admin/v1/assets/{id}/publish` | `asset.publish`；发布当前版本 |
| POST | `/api/admin/v1/assets/{id}/unpublish` | `asset.publish`；撤回，公开地址立即 404 |
| POST | `/api/admin/v1/assets/{id}/archive` | `asset.publish`；归档 |
| POST | `/api/admin/v1/assets/{id}/trash` | `asset.trash`；进回收站 |
| POST | `/api/admin/v1/assets/{id}/restore` | `asset.trash`；恢复，不自动公开 |
| DELETE | `/api/admin/v1/assets/{id}` | 所有者；彻底删除（须已在回收站） |
| GET | `/api/admin/v1/assets/{id}/file` | `asset.read`；任意状态下载原件（支持 Range） |
| GET | `/api/admin/v1/assets/{id}/signed-url` | `asset.read`；给已发布内容签短时地址 |
| GET | `/api/public/v1/files/{id}` | 匿名；凭签名读取，撤回后 404 |

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
```

扩展名随该版本的**真实类型**变化：换过文件类型之后，旧扩展名的地址必须消失，
所以每次发布都会先清空该内容在 `public/` 下的目录，再重建两个链接。
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
| 彻底删除 | 仅所有者 | 须已在回收站；有内容把它当封面时拒绝并列出引用 |

失败一律落在「更不可访问」这一侧：发布时先提交状态再建链接（链接失败 = 公开地址 404，
不会泄露未发布内容）；撤回时先删链接再提交状态（提交失败也只是 404）。残留的链接由
`fsck` 收掉。

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

## 数据库

SQLite，单文件，`journal_mode=WAL`、`busy_timeout=5000`、`foreign_keys=ON`、`synchronous=NORMAL`、
`cache_size=-8000`、`mmap_size=0`、`temp_store=FILE`。这些 PRAGMA 写在
[`db.py`](src/ctrlstore/db.py) 的 `PRAGMAS` 里，每次新建连接时统一重放。

每个进程只保留一个连接（`pool_size=1, max_overflow=0`）：API 两个进程、worker 一个进程，
共三条连接，写锁竞争可预测。只有 API 和 worker 打开库文件，网站只走 HTTP。

表结构见 [models.py](src/ctrlstore/models.py)，迁移在 [alembic/versions](alembic/versions)：
`0001` 基线，`0002` 身份与会话，`0003` 上传与幂等键，`0004` 目录、标签、内容与版本。
「至多一个所有者」「同一父目录下不重名」「同一内容版本号唯一」都由库层约束兜底，
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

审计写在 `audit_logs`：登录成功与失败、退出、建账号、权限变更、停用、改密码、建站点、签发与撤销令牌，
上传的开始、完成、取消与失败，以及建目录、改名、移动、建标签、内容登记、元数据修改、版本追加与淘汰。
登录失败的记录里只有用户名和 IP，不记密码；上传与内容记录里只有类型、字节数与哈希，不记文件正文。
