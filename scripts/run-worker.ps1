# Worker 单独运行，便于系统服务分别启停并监控。
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repoRoot "backend")
try {
    uv run --locked python -m notification_app.jobs.worker
    if ($LASTEXITCODE -ne 0) { throw "后台 Worker 退出，代码 $LASTEXITCODE。" }
}
finally {
    Pop-Location
}
