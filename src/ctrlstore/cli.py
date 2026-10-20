"""``ctrlstore`` 命令行。

第一部分只提供三件事：

* ``ctrlstore init``：创建数据目录并执行迁移（账号创建在第二部分加入）。
* ``ctrlstore serve``：启动 API。
* ``ctrlstore check-config``：只校验配置，用于部署前自检与排错。

参数解析用标准库 ``argparse``，不额外引入 CLI 框架。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import IS_POSIX, ConfigError, Settings, load_settings
from .db import create_engine, run_migrations
from .log import configure_logging

#: 目录权限：属主与同组可进入，其他用户无权限（企划第 11.1 节）。
DIRECTORY_MODE = 0o750


def main(argv: Sequence[str] | None = None) -> int:
    """命令入口。返回进程退出码。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctrlstore", description="CtrlStore 内容库")
    parser.add_argument("--version", action="version", version=f"ctrlstore {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="创建数据目录并执行数据库迁移")
    init.set_defaults(handler=_cmd_init)

    serve = subcommands.add_parser("serve", help="启动 API 服务")
    serve.add_argument("--host", default=None, help="监听地址，默认取配置")
    serve.add_argument("--port", type=int, default=None, help="监听端口，默认取配置")
    serve.set_defaults(handler=_cmd_serve)

    check = subcommands.add_parser("check-config", help="只校验配置，不启动服务")
    check.set_defaults(handler=_cmd_check_config)

    return parser


def _load(args: argparse.Namespace) -> Settings:
    return load_settings(args.config)


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
    print(f"数据目录：{settings.data_dir}")
    print(f"元数据库：{settings.db_path}")
    print("数据库已迁移到最新版本")
    return 0


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


def _ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if IS_POSIX:
        os.chmod(path, DIRECTORY_MODE)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
