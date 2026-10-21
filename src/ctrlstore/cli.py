"""``ctrlstore`` 命令行。

第一期没有图形界面时，部署、建账号、签发令牌都要靠这里完成，
所以这些命令直接调用 ``auth.service``——和 HTTP 接口是同一套规则，
命令行不是后门，只是另一种调用方式。

密码一律从标准输入读：既不进 shell 历史，也不出现在进程列表里。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from . import __version__, fsck
from .assets import publish as publish_service
from .assets import service as asset_service
from .auth import permissions, service
from .config import IS_POSIX, ConfigError, Settings, load_settings
from .db import create_engine, run_migrations
from .errors import ApiError
from .jobs import service as jobs_service
from .log import configure_logging, ensure_utf8_stdout
from .models import JOB_DONE, JOB_FAILED, JOB_PENDING, JOB_RUNNING, Asset, User, utcnow
from .storage import LocalStorage
from .uploads import service as upload_service

#: 目录权限：属主与同组可进入，其他用户无权限（企划第 11.1 节）。
DIRECTORY_MODE = 0o750

#: fsck 问题代号的中文说明。
_ISSUE_LABELS = {
    "missing_file": "数据库有记录但文件不存在",
    "orphan_file": "文件存在但没有记录",
    "missing_link": "应公开但缺少链接",
    "stale_link": "公开区存在多余链接",
    "hash_mismatch": "哈希或大小不一致",
    "temp_link": "残留的 .tmp 链接",
}


def main(argv: Sequence[str] | None = None) -> int:
    """命令入口。返回进程退出码。"""
    # 先统一输出编码：下面的中文提示在 Windows 控制台上也要能看清。
    ensure_utf8_stdout()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctrlstore", description="CtrlStore 内容库")
    parser.add_argument("--version", action="version", version=f"ctrlstore {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="建数据目录、执行迁移、创建唯一所有者")
    init.add_argument("--username", default="owner", help="所有者用户名，默认 owner")
    _add_password_flag(init)
    init.set_defaults(handler=_cmd_init)

    serve = subcommands.add_parser("serve", help="启动 API 服务")
    serve.add_argument("--host", default=None, help="监听地址，默认取配置")
    serve.add_argument("--port", type=int, default=None, help="监听端口，默认取配置")
    serve.set_defaults(handler=_cmd_serve)

    check = subcommands.add_parser("check-config", help="只校验配置，不启动服务")
    check.set_defaults(handler=_cmd_check_config)

    worker = subcommands.add_parser(
        "worker", help="运行后台 worker：缩略图、定时发布、到期清理"
    )
    worker.add_argument("--once", action="store_true", help="处理完当前排队的任务就退出")
    worker.set_defaults(handler=_cmd_worker)

    jobs = subcommands.add_parser("jobs", help="后台任务")
    job_actions = jobs.add_subparsers(dest="action", required=True)
    job_list = job_actions.add_parser("list", help="列出任务与最近一次错误")
    job_list.add_argument(
        "--status",
        default=None,
        choices=[JOB_PENDING, JOB_RUNNING, JOB_DONE, JOB_FAILED],
        help="只看某个状态",
    )
    job_list.add_argument("--limit", type=int, default=50, help="最多列出多少条，默认 50")
    job_list.set_defaults(handler=_cmd_jobs_list)

    upload = subcommands.add_parser("import", help="从服务器本地路径导入文件")
    upload.add_argument("path", type=Path, help="要导入的文件路径")
    upload.add_argument(
        "--asset-id", default=None, help="要替换的内容 ID；给了就作为该内容的新版本"
    )
    upload.set_defaults(handler=_cmd_import)

    publish = subcommands.add_parser("publish", help="发布内容（所有者）")
    publish.add_argument("asset_id")
    publish.set_defaults(handler=_cmd_publish)

    unpublish = subcommands.add_parser("unpublish", help="撤回内容（所有者）")
    unpublish.add_argument("asset_id")
    unpublish.set_defaults(handler=_cmd_unpublish)

    fsck = subcommands.add_parser("fsck", help="检查库记录与磁盘是否一致")
    fsck.add_argument(
        "--fix-public-links",
        action="store_true",
        help="按已提交的发布状态增删公开链接；不删原件，也不重新公开已撤回内容",
    )
    fsck.add_argument("--skip-hashes", action="store_true", help="跳过哈希比对（文件多时更快）")
    fsck.set_defaults(handler=_cmd_fsck)

    _build_user_commands(subcommands)
    _build_site_commands(subcommands)
    _build_token_commands(subcommands)
    return parser


def _build_user_commands(subcommands) -> None:
    user = subcommands.add_parser("user", help="账号管理")
    actions = user.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", help="创建编辑")
    create.add_argument("username")
    _add_password_flag(create)
    _add_permission_flags(create)
    create.set_defaults(handler=_cmd_user_create)

    listing = actions.add_parser("list", help="列出账号及其权限")
    listing.set_defaults(handler=_cmd_user_list)

    grant = actions.add_parser("grant", help="授予权限")
    grant.add_argument("username")
    _add_permission_flags(grant, required=True)
    grant.set_defaults(handler=_cmd_user_grant)

    revoke = actions.add_parser("revoke", help="收回权限")
    revoke.add_argument("username")
    _add_permission_flags(revoke, required=True)
    revoke.set_defaults(handler=_cmd_user_revoke)

    disable = actions.add_parser("disable", help="停用编辑，立即失效其会话与令牌")
    disable.add_argument("username")
    disable.set_defaults(handler=_cmd_user_disable)

    reset = actions.add_parser("reset-password", help="重置某个账号的密码")
    reset.add_argument("username")
    _add_password_flag(reset)
    reset.set_defaults(handler=_cmd_user_reset_password)

    owner = subcommands.add_parser("owner", help="所有者专用操作")
    owner_actions = owner.add_subparsers(dest="action", required=True)
    owner_reset = owner_actions.add_parser(
        "reset-password", help="在服务器本地重置所有者的密码（只重置，不新建）"
    )
    _add_password_flag(owner_reset)
    owner_reset.set_defaults(handler=_cmd_owner_reset_password)


def _build_site_commands(subcommands) -> None:
    site = subcommands.add_parser("site", help="网站管理")
    actions = site.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", help="创建网站")
    create.add_argument("key", help="站点标识，例如 main-site")
    create.add_argument("--name", default="", help="显示名，默认与标识相同")
    create.set_defaults(handler=_cmd_site_create)

    listing = actions.add_parser("list", help="列出网站")
    listing.set_defaults(handler=_cmd_site_list)


def _build_token_commands(subcommands) -> None:
    token = subcommands.add_parser("token", help="访问令牌管理")
    actions = token.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", help="签发令牌，明文只显示一次")
    subject = create.add_mutually_exclusive_group(required=True)
    subject.add_argument("--site", help="站点标识")
    subject.add_argument("--user", help="用户名，用于命令行访问")
    create.add_argument("--name", default="", help="令牌备注名")
    create.add_argument(
        "--allowed-ip",
        default=None,
        help="允许的来源 IP，逗号分隔多个；默认不限制",
    )
    create.add_argument("--expires-days", type=int, default=None, help="多少天后过期，默认不过期")
    create.set_defaults(handler=_cmd_token_create)

    listing = actions.add_parser("list", help="列出令牌（不含明文）")
    listing.set_defaults(handler=_cmd_token_list)

    revoke = actions.add_parser("revoke", help="撤销令牌")
    revoke.add_argument("token_id")
    revoke.set_defaults(handler=_cmd_token_revoke)


def _add_password_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="从标准输入读密码（非交互环境用）；默认交互式输入两次",
    )


def _add_permission_flags(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument(
        "--permission",
        action="append",
        dest="permissions",
        default=[],
        choices=list(permissions.PERMISSIONS),
        metavar="NAME",
        help=f"单项权限，可重复。可选：{', '.join(permissions.PERMISSIONS)}",
    )
    parser.add_argument(
        "--preset",
        action="append",
        dest="presets",
        default=[],
        choices=sorted(permissions.PRESETS),
        metavar="NAME",
        help="预设，可重复。editor 一次授予全部八项",
    )
    if required:
        parser.set_defaults(_permission_required=True)


# —— 公共辅助 ——


def _load(args: argparse.Namespace) -> Settings:
    return load_settings(args.config)


@contextmanager
def _session(settings: Settings) -> Iterator[DbSession]:
    """打开数据库会话。命令结束时统一提交，异常时回滚。"""
    engine = create_engine(settings)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
    engine.dispose()


def _read_password(args: argparse.Namespace) -> str:
    """交互式读两次密码；``--password-stdin`` 时读一行。"""
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("密码：")
        if getpass.getpass("再次输入：") != password:
            raise ApiError.invalid("两次输入的密码不一致")
    if not password:
        raise ApiError.invalid("密码不能为空")
    return password


def _selected_permissions(args: argparse.Namespace) -> set[str]:
    """把 ``--permission`` 与 ``--preset`` 合成一个集合。"""
    selected = set(args.permissions)
    for name in args.presets:
        selected |= permissions.from_preset(name)
    return selected


def _actor(db: DbSession) -> User:
    """命令行操作的审计主体：记为所有者本人。"""
    owner = service.get_owner(db)
    if owner is None:
        raise ApiError.invalid("尚未初始化所有者，请先运行 ctrlstore init")
    return owner


def _find_user(db: DbSession, username: str) -> User:
    user = service.find_user(db, username)
    if user is None:
        raise ApiError.invalid(f"账号不存在：{username}")
    return user


# —— 命令实现 ——


def _cmd_init(args: argparse.Namespace) -> int:
    settings = _load(args)
    configure_logging(settings.log_level)
    for directory in settings.managed_directories():
        _ensure_directory(directory)
    engine = create_engine(settings)
    try:
        run_migrations(settings, engine=engine)
    finally:
        engine.dispose()

    with _session(settings) as db:
        if service.get_owner(db) is None:
            service.create_owner(db, username=args.username, password=_read_password(args))
            print(f"已创建所有者：{args.username}")
        else:
            print("所有者已存在，跳过创建")

    print(f"数据目录：{settings.data_dir}")
    print(f"元数据库：{settings.db_path}")
    print("数据库已迁移到最新版本")
    return 0


def _cmd_user_create(args: argparse.Namespace) -> int:
    settings = _load(args)
    with _session(settings) as db:
        user = service.create_editor(
            db,
            username=args.username,
            password=_read_password(args),
            granted=_selected_permissions(args),
            actor=_actor(db),
        )
        print(f"已创建编辑：{user.username}")
        print(f"权限：{_format_permissions(user) or '（无）'}")
    return 0


def _cmd_user_list(args: argparse.Namespace) -> int:
    settings = _load(args)
    with _session(settings) as db:
        for user in service.list_users(db):
            state = "已停用" if not user.is_active else user.role
            print(f"{user.username}\t{state}\t{_format_permissions(user) or '-'}")
    return 0


def _cmd_user_grant(args: argparse.Namespace) -> int:
    return _change_permissions(args, add=True)


def _cmd_user_revoke(args: argparse.Namespace) -> int:
    return _change_permissions(args, add=False)


def _change_permissions(args: argparse.Namespace, *, add: bool) -> int:
    settings = _load(args)
    selected = _selected_permissions(args)
    with _session(settings) as db:
        user = _find_user(db, args.username)
        owner = _actor(db)
        current = {item.permission for item in user.permissions}
        updated = current | selected if add else current - selected
        service.set_permissions(db, user, updated, actor=owner)
        print(f"{user.username} 的权限：{_format_permissions(user) or '（无）'}")
    return 0


def _cmd_user_disable(args: argparse.Namespace) -> int:
    settings = _load(args)
    with _session(settings) as db:
        user = _find_user(db, args.username)
        service.disable_user(db, user, actor=_actor(db))
        print(f"已停用：{user.username}")
    return 0


def _cmd_user_reset_password(args: argparse.Namespace) -> int:
    settings = _load(args)
    with _session(settings) as db:
        user = _find_user(db, args.username)
        service.reset_password(db, user, _read_password(args), actor=_actor(db))
        print(f"已重置密码：{user.username}，旧会话与令牌已失效")
    return 0


def _cmd_owner_reset_password(args: argparse.Namespace) -> int:
    """只重置现有所有者；不存在就报错，绝不顺手创建一个。"""
    settings = _load(args)
    with _session(settings) as db:
        owner = service.get_owner(db)
        if owner is None:
            raise ApiError.invalid("所有者不存在，请先运行 ctrlstore init")
        service.reset_password(db, owner, _read_password(args), actor=owner)
        print(f"已重置所有者密码：{owner.username}，旧会话与令牌已失效")
    return 0


def _cmd_site_create(args: argparse.Namespace) -> int:
    settings = _load(args)
    with _session(settings) as db:
        site = service.create_site(db, key=args.key, name=args.name, actor=_actor(db))
        print(f"已创建站点：{site.key}（{site.id}）")
    return 0


def _cmd_site_list(args: argparse.Namespace) -> int:
    settings = _load(args)
    with _session(settings) as db:
        for site in service.list_sites(db):
            print(f"{site.key}\t{site.name}\t{site.id}")
    return 0


def _cmd_token_create(args: argparse.Namespace) -> int:
    settings = _load(args)
    expires_at = utcnow() + timedelta(days=args.expires_days) if args.expires_days else None
    with _session(settings) as db:
        owner = _actor(db)
        if args.site:
            site = service.find_site(db, args.site)
            if site is None:
                raise ApiError.invalid(f"站点不存在：{args.site}")
            plaintext, token = service.issue_site_token(
                db,
                site=site,
                name=args.name,
                expires_at=expires_at,
                actor=owner,
                allowed_ip=args.allowed_ip,
            )
        else:
            user = _find_user(db, args.user)
            plaintext, token = service.issue_user_token(
                db,
                user=user,
                name=args.name,
                expires_at=expires_at,
                actor=owner,
                allowed_ip=args.allowed_ip,
            )
        print(f"已签发令牌：{token.name}（{token.id}）")
        print("令牌只显示这一次，请立即保存：")
        print(plaintext)
    return 0


def _cmd_token_list(args: argparse.Namespace) -> int:
    settings = _load(args)
    with _session(settings) as db:
        for token in service.list_tokens(db):
            state = "已撤销" if token.revoked_at else "有效"
            expires = token.expires_at.isoformat() if token.expires_at else "-"
            print(f"{token.id}\t{token.subject_type}\t{token.name}\t{state}\t{expires}")
    return 0


def _cmd_token_revoke(args: argparse.Namespace) -> int:
    settings = _load(args)
    with _session(settings) as db:
        token = service.get_token(db, args.token_id)
        service.revoke_token(db, token, actor=_actor(db))
        print(f"已撤销令牌：{token.id}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    """从服务器本地路径导入。走与 HTTP 上传相同的会话、校验与落盘逻辑。"""
    settings = _load(args)
    storage = LocalStorage.from_settings(settings)
    path = args.path.expanduser()
    with _session(settings) as db:
        owner = _actor(db)
        session = upload_service.import_file(
            db,
            settings=settings,
            storage=storage,
            actor=owner,
            path=path,
            target_asset_id=args.asset_id,
        )
        print(f"已导入：{path.name}")
        print(f"类型：{session.kind}（{session.mime}）")
        print(f"大小：{session.byte_size} 字节")
        print(f"SHA-256：{session.sha256}")
        print(f"内容 ID：{session.asset_id}")
        print(f"存储键：{session.storage_key}")
        if args.asset_id:
            asset = db.get(Asset, session.asset_id)
            version = asset_service.current_version(asset)
            print(f"当前版本：v{version.version_no if version else '-'}，revision {asset.revision}")
        duplicate = upload_service.duplicate_of(db, session)
        if duplicate:
            print(f"提示：库中已有相同字节的上传（{duplicate}）")
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    """命令行发布。没管理端时也必须能做（企划第 13.4 节）。"""
    settings = _load(args)
    storage = LocalStorage.from_settings(settings)
    with _session(settings) as db:
        owner = _actor(db)
        asset = asset_service.get_asset(db, args.asset_id)
        publish_service.publish(
            db, storage=storage, asset=asset, revision=asset.revision, actor=owner
        )
        print(f"已发布：{asset.id}")
        print(f"状态：{asset.status}；已发布版本：{asset.published_version_id}")
        print(f"修订号：{asset.revision}")
    return 0


def _cmd_unpublish(args: argparse.Namespace) -> int:
    settings = _load(args)
    storage = LocalStorage.from_settings(settings)
    with _session(settings) as db:
        owner = _actor(db)
        asset = asset_service.get_asset(db, args.asset_id)
        publish_service.unpublish(
            db, storage=storage, asset=asset, revision=asset.revision, actor=owner
        )
        print(f"已撤回：{asset.id}")
        print(f"状态：{asset.status}；修订号：{asset.revision}")
    return 0


def _cmd_fsck(args: argparse.Namespace) -> int:
    """检查库记录与磁盘是否一致。默认只报告，``--fix-public-links`` 才动公开区。"""
    settings = _load(args)
    storage = LocalStorage.from_settings(settings)
    with _session(settings) as db:
        report = fsck.run(
            db,
            storage=storage,
            fix_public_links=args.fix_public_links,
            check_hashes=not args.skip_hashes,
        )

    print(f"检查了 {report.checked_files} 个版本文件")
    if report.ok:
        print("没有发现问题")
        return 0

    grouped: dict[str, list[str]] = {}
    for issue in report.issues:
        grouped.setdefault(issue.code, []).append(issue.detail)
    for code, details in sorted(grouped.items()):
        print(f"{_ISSUE_LABELS.get(code, code)}（{len(details)}）：")
        for detail in details:
            print(f"  - {detail}")

    if report.fixed:
        print(f"已修复 {report.fixed} 处公开链接")
    remaining = [issue for issue in report.issues if not (args.fix_public_links and issue.fixable)]
    if remaining:
        print(f"仍有 {len(remaining)} 处需要人工处理（fsck 不删原件）")
    return 0 if not remaining else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .main import create_app

    settings = _load(args)
    uvicorn.run(
        # 直接传应用实例：配置已经在这里解析过，不能再让 Uvicorn 重新猜一遍。
        create_app(settings),
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        # 日志已由 configure_logging 统一成 JSON，不让 Uvicorn 再装一套。
        log_config=None,
        access_log=False,
    )
    return 0


def _cmd_check_config(args: argparse.Namespace) -> int:
    settings = _load(args)
    print("配置有效")
    print(f"数据目录：{settings.data_dir}")
    print(f"元数据库：{settings.db_path}")
    print(f"公开基址：{settings.public_base_url}")
    print(f"来源文件：{settings.config_path}")
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    """后台 worker。部署时由独立的 systemd 单元启动（第 9 部分给单元文件）。"""
    from .worker import run_forever, run_until_idle

    settings = _load(args)
    configure_logging(settings.log_level)
    if args.once:
        processed = run_until_idle(settings)
        print(f"处理了 {processed} 个任务")
    else:
        run_forever(settings)
    return 0


def _cmd_jobs_list(args: argparse.Namespace) -> int:
    """列出任务。失败的任务留在队列里，这里是所有者查看它们的地方。"""
    settings = _load(args)
    with _session(settings) as db:
        jobs = jobs_service.list_jobs(db, status=args.status, limit=args.limit)
        if not jobs:
            print("没有符合条件的任务")
            return 0
        for job in jobs:
            error = (job.last_error or "").replace("\n", " ")[:120]
            line = (
                f"{job.id}\t{job.kind}\t{job.status}\t尝试 {job.attempts}\t"
                f"{job.created_at.isoformat()}\t{error}"
            )
            print(line.rstrip())
        failed = [job for job in jobs if job.status == JOB_FAILED]
        if failed:
            print(f"其中 {len(failed)} 个任务已停在失败状态，请按 last_error 排查原因。")
    return 0


def _format_permissions(user: User) -> str:
    granted = permissions.effective(user.role, (item.permission for item in user.permissions))
    return ",".join(sorted(granted))


def _ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if IS_POSIX:
        os.chmod(path, DIRECTORY_MODE)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
