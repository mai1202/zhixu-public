@echo off
rem Shared launcher for GUI entry scripts.
rem Prefer the bundled runtime, then the venv base interpreter, then the venv itself.
setlocal
set "ROOT=%~dp0.."
set "ENTRY=%~1"
if not defined ENTRY (
    echo Missing GUI entry script.
    exit /b 2
)

if exist "%ROOT%\runtime\python\pythonw.exe" (
    start "" "%ROOT%\runtime\python\pythonw.exe" "%ENTRY%"
    exit /b 0
)

set "VENV=%ROOT%\backend\.venv"
set "BASE_HOME="
set "BASE_PYTHONW="
if not exist "%VENV%\pyvenv.cfg" goto :skip_cfg
for /f "tokens=2 delims==" %%A in ('findstr /b /i "home" "%VENV%\pyvenv.cfg"') do set "BASE_HOME=%%A"
if defined BASE_HOME if "%BASE_HOME:~0,1%"==" " set "BASE_HOME=%BASE_HOME:~1%"
if exist "%BASE_HOME%\pythonw.exe" set "BASE_PYTHONW=%BASE_HOME%\pythonw.exe"
:skip_cfg

rem Some security products block the venv interpreter but trust the base interpreter.
if defined BASE_PYTHONW (
    set "PYTHONPATH=%VENV%\Lib\site-packages"
    start "" /b "%BASE_PYTHONW%" "%ENTRY%"
    exit /b 0
)

if exist "%VENV%\Scripts\pythonw.exe" (
    start "" /b "%VENV%\Scripts\pythonw.exe" "%ENTRY%"
    exit /b 0
)

pyw -3 "%ENTRY%"
exit /b %errorlevel%
