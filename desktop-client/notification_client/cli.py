"""客户端 CLI：配对、设置采集器、查看状态、立即同步和五分钟循环。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import sys
import time
from typing import Sequence

from .api_client import ClientApiError
from .client_app import ClientApplication, default_data_dir
from .local_queue import QueueFullError
from .wechat_cli import WechatCliError


def _configure_stdio() -> None:
    """管道或重定向输出时统一使用 UTF-8，避免 Windows 本地代码页造成中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if getattr(stream, "isatty", lambda: False)():
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _print_json(payload: dict) -> None:
    """以 UTF-8 中文输出状态摘要，不包含任何访问令牌或消息正文。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _safe_error_code(error: Exception) -> str:
    """把异常转成不含输入正文、URL 或系统路径的短错误代码。"""
    if isinstance(error, ClientApiError):
        return error.code
    if isinstance(error, WechatCliError):
        return error.code
    if isinstance(error, QueueFullError):
        return "queue_full"
    if isinstance(error, (ValueError, RuntimeError)):
        return "client_configuration_error"
    return "client_operation_failed"


def build_parser() -> argparse.ArgumentParser:
    """定义受限的本地管理命令，不提供 shell 或任意 URL 代理。"""
    parser = argparse.ArgumentParser(prog="notification-client", description="通知管理本机采集客户端")
    parser.add_argument("--data-dir", help="合成测试可指定临时数据目录；日常使用默认 LocalAppData")
    subcommands = parser.add_subparsers(dest="command", required=True)

    pair = subcommands.add_parser("pair", help="使用网页短时配对码绑定本机")
    pair.add_argument("--server", required=True, help="服务端根地址")
    pair.add_argument("--name", default=None, help="设备显示名称；默认使用本机名称")
    pair.add_argument("--cli", default=None, help="本机 wechat-cli.exe 路径")

    configure = subcommands.add_parser("configure-cli", help="配置已有 wechat-cli.exe 路径")
    configure.add_argument("path")

    chat = subcommands.add_parser("add-chat", help="核验一个明确指定的会话并提交候选目录")
    chat.add_argument("chat_name")

    subcommands.add_parser("status", help="查看本机配对、暂停与待上传队列状态")
    subcommands.add_parser("sync", help="立即执行一轮采集与上传")
    subcommands.add_parser("pause", help="暂停后续采集，保留本地队列")
    subcommands.add_parser("resume", help="恢复按网页设置间隔采集（默认5分钟）")
    subcommands.add_parser("loop", help="当前登录会话中按网页设置间隔采集")
    subcommands.add_parser("tray", help="显示系统托盘状态菜单并按网页设置间隔采集")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    app = ClientApplication(args.data_dir or default_data_dir())
    try:
        if args.command == "pair":
            code = getpass.getpass("输入网页生成的 10 位设备配对码：")
            name = args.name or __import__("socket").gethostname()
            result = app.pair(server_url=args.server, code=code, device_name=name, cli_path=args.cli)
            _print_json({**result, "message": "设备已领取，等待网页端核对并确认后才可同步。"})
        elif args.command == "configure-cli":
            app.configure_cli(args.path)
            _print_json({"status": "configured"})
        elif args.command == "add-chat":
            result = app.add_chat(args.chat_name)
            _print_json({**result, "message": "会话已加入网页候选目录；选中前不会上传聊天正文。"})
        elif args.command == "status":
            result = app.status()
            try:
                result["server_pairing_status"] = app.pairing_status()
            except ClientApiError as error:
                result["server_pairing_status"] = error.code
            _print_json(result)
        elif args.command == "sync":
            result = app.sync_once()
            _print_json(result)
        elif args.command == "pause":
            app.pause(True)
            _print_json({"status": "paused"})
        elif args.command == "resume":
            app.pause(False)
            _print_json({"status": "active"})
        elif args.command == "loop":
            app.run_loop()
        elif args.command == "tray":
            from .tray import run_tray

            import threading

            threading.Thread(target=app.run_loop, name="notification-client-sync-loop", daemon=True).start()
            run_tray(app)
    except KeyboardInterrupt:
        return 0
    except (ClientApiError, WechatCliError, QueueFullError, RuntimeError, ValueError, OSError) as error:
        code = _safe_error_code(error)
        log_path = app.data_dir / "client.log"
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{timestamp} {code}\n")
        print(f"操作未完成，错误代码：{code}。可用 notification-client status 查看连接状态。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
