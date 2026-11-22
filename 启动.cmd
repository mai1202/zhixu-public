@echo off
chcp 65001 >nul
rem 使用现有 Python 图形运行时，不修改系统执行策略或创建自启项。
rem 虚拟环境解释器被安全软件拦截时，启动器会自动改用其基础解释器。
call "%~dp0scripts\launch_gui.cmd" "%~dp0scripts\local_launcher.py"
if errorlevel 1 (
  echo 请先按 README 安装 Python 和 uv，再重新打开启动器。
  pause
)
