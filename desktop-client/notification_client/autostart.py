"""仅管理当前 Windows 用户的采集器登录启动项，无需管理员权限。"""

import hashlib
import os
from pathlib import Path
import subprocess
import sys


def startup_name(data_dir: Path) -> str:
    """不同数据目录使用不同键，避免测试或多个安装互相覆盖。"""
    suffix = hashlib.sha256(str(data_dir.resolve()).lower().encode()).hexdigest()[:12]
    return f"NotificationManagementCollector-{suffix}"


def autostart_command(data_dir: Path) -> str:
    """生成当前安装目录对应的登录自启命令，便于识别旧路径。"""
    from .updater import installation_root
    base = installation_root()
    python = Path(sys.executable).with_name("pythonw.exe")
    launcher = base / "scripts" / "collector_launcher.py"
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "autostart_collector.cmd"
    if not python.is_file() or not launcher.is_file() or not wrapper.is_file():
        raise RuntimeError("启动器文件不完整，无法设置登录后自动运行。")
    return subprocess.list2cmdline([
        "cmd.exe", "/d", "/c", "call", str(wrapper),
        "--data-dir", str(data_dir.resolve()), "--background",
    ])


def autostart_enabled(data_dir: Path) -> bool:
    """读取当前用户登录自启状态，只查询本采集器自己的注册表值。"""
    if os.name != "nt":
        return False
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _kind = winreg.QueryValueEx(key, startup_name(data_dir))
        # 旧安装目录或缺失文件不应被当作当前版本已启用，后续轮询会修复路径。
        return isinstance(value, str) and value.strip().casefold() == autostart_command(data_dir).casefold()
    except (OSError, RuntimeError):
        return False


def set_autostart(data_dir: Path, enabled: bool) -> None:
    """使用绑定本次运行的 Python 和绝对路径；解除配对只删除自己的值。"""
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        if not enabled:
            try:
                winreg.DeleteValue(key, startup_name(data_dir))
            except FileNotFoundError:
                pass
            return
        winreg.SetValueEx(key, startup_name(data_dir), 0, winreg.REG_SZ, autostart_command(data_dir))
