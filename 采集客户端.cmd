@echo off
chcp 65001 >nul
rem 复用项目运行时，无需用户输入配对或安装自启的命令。
rem 虚拟环境解释器被安全软件拦截时，启动器会自动改用其基础解释器。
call "%~dp0scripts\launch_gui.cmd" "%~dp0scripts\collector_launcher.py"
if errorlevel 1 (
  echo 未找到 Python 图形运行时，请使用包含运行时的发布包，或参见学生启动说明。
  pause
)
