"""受限微信组件入口；用户不需要运行或理解 wechat-cli 命令。"""

import json
import os
from pathlib import Path
import subprocess
import sys


def _force_utf8_stdio() -> None:
    """被管道调用时也输出 UTF-8，避免中文 Windows 按本地代码页写出 GBK。"""
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


def _child_environment() -> dict[str, str]:
    """把 UTF-8 输出约定传给嵌套的微信组件进程。"""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def main():
    """发布包使用自带模块，开发版可由维护者指定已安装的组件运行时。"""
    _force_utf8_stdio()
    args = sys.argv[1:]
    if not args or args[0] not in {"contacts", "history", "--version"}:
        return 2
    collect_attachments = '--collect-attachments' in args
    if collect_attachments:
        args.remove('--collect-attachments')
        if args[0] != 'history':
            return 2
    root = Path(__file__).resolve().parents[1]
    config_file = root / '.runtime/wechat-component.json'
    if config_file.is_file():
        # 开发配置只记录已安装 Python 的路径，不包含聊天、数据库密钥或配对令牌。
        config = json.loads(config_file.read_text(encoding='utf-8'))
        command = [config['python'], '-c', 'from wechat_cli.main import cli; cli()', *args]
        if args[0] == 'history':
            # 已在组件 Python 内的子进程直接运行桥接器，避免再次读取开发转发配置。
            command = [config['python'], '-c', f'from wechat_attachment_bridge import run; import sys; run(sys.argv[1:], collect_attachments={collect_attachments!r})', *args]
        result = subprocess.run(
            command,
            cwd=str(Path(__file__).resolve().parent),
            shell=False,
            env=_child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        # 无控制台启动时子进程的标准输出不会自动回到调用方，必须显式转发 JSON。
        if result.stdout and getattr(sys.stdout, "buffer", None) is not None:
            sys.stdout.buffer.write(result.stdout)
        if result.stderr and getattr(sys.stderr, "buffer", None) is not None:
            sys.stderr.buffer.write(result.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        return result.returncode
    try:
        from wechat_cli.main import cli
    except ImportError:
        print('wechat_component_missing', file=sys.stderr)
        return 2
    if args[0] != '--version':
        from wechat_cli.core.config import CONFIG_FILE, KEYS_FILE
        if not Path(CONFIG_FILE).is_file() or not Path(KEYS_FILE).is_file():
            # 初次初始化只在配对确认后发生，不要求用户另开终端。
            result = subprocess.run(
                [sys.executable, '-c', 'from wechat_cli.main import cli; cli()', 'init'],
                env=_child_environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if result.returncode:
                print('wechat_initialization_failed', file=sys.stderr)
                return 3
    # 所有历史页都核对发送者身份；附件索引仍只在用户开启时采集。
    if args[0] == 'history':
        from wechat_attachment_bridge import run
        run(args, collect_attachments=collect_attachments)
    else:
        cli(args=args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
