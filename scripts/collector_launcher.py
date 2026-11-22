"""双击入口使用源码中的客户端，无需用户安装 notification-client 命令。"""

import json
import os
from pathlib import Path
import subprocess
import sys


def active_release(program):
    """稳定入口只读取本安装目录内的版本指针，损坏时回到原始版本。"""
    program = Path(program).resolve()
    base = program.parents[2] if program.parent.name == 'releases' and program.parent.parent.name == '.collector-updates' else program
    try:
        relative = json.loads((base / 'collector-active.json').read_text(encoding='utf-8'))['path']
        target = (base / relative).resolve()
        if target.parent != (base / '.collector-updates/releases').resolve():
            return base
        if not all((target / name).is_file() for name in ('runtime/python/pythonw.exe',
                'scripts/collector_launcher.py', 'desktop-client/notification_client/gui.py')):
            return base
        return target
    except (OSError, ValueError, TypeError, KeyError):
        return base


def main():
    """新版使用自身Python运行时，原始桌面入口和登录自启路径保持稳定。"""
    program = Path(__file__).resolve().parents[1]
    selected = active_release(program)
    if selected != program:
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        subprocess.Popen([str(selected / 'runtime/python/pythonw.exe'),
            str(selected / 'scripts/collector_launcher.py'), *sys.argv[1:]], creationflags=flags)
        return
    sys.path.insert(0, str(program / 'desktop-client'))
    from notification_client.gui import main as gui_main
    gui_main()


if __name__ == '__main__':
    main()
