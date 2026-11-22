@echo off
rem 登录自启：优先使用虚拟环境对应的基础 Python，并复用虚拟环境依赖。
setlocal
set "ROOT=%~dp0.."
set "VENV=%ROOT%\backend\.venv"
set "ENTRY=%ROOT%\scripts\collector_launcher.py"
set "PYTHONPATH=%VENV%\Lib\site-packages"
rem 发布包优先使用内置运行时，更新后的稳定入口会选择已启用版本。
if exist "%ROOT%\runtime\python\pythonw.exe" (
    start "" /b "%ROOT%\runtime\python\pythonw.exe" "%ENTRY%" %*
    exit /b 0
)
set "BASE_HOME="
if not exist "%VENV%\pyvenv.cfg" goto :fallback
for /f "tokens=2 delims==" %%A in ('findstr /b /i "home" "%VENV%\pyvenv.cfg"') do set "BASE_HOME=%%A"
if defined BASE_HOME if "%BASE_HOME:~0,1%"==" " set "BASE_HOME=%BASE_HOME:~1%"
if not exist "%BASE_HOME%\pythonw.exe" goto :fallback
start "" /b "%BASE_HOME%\pythonw.exe" "%ENTRY%" %*
exit /b 0

:fallback
if exist "%VENV%\Scripts\pythonw.exe" (
    start "" /b "%VENV%\Scripts\pythonw.exe" "%ENTRY%" %*
    exit /b 0
)
pyw -3 "%ENTRY%" %*
