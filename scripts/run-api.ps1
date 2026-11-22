# 前台运行 Waitress；退出窗口即可停止服务。
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repoRoot "backend")
try {
    uv run --locked python -m notification_app.server
    if ($LASTEXITCODE -ne 0) { throw "API 服务退出，代码 $LASTEXITCODE。" }
}
finally {
    Pop-Location
}
