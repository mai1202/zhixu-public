# 从仓库锁定环境执行 Alembic 升级。
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repoRoot "backend")
try {
    uv run --locked alembic -c alembic.ini upgrade head
    if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败，退出码 $LASTEXITCODE。" }
}
finally {
    Pop-Location
}
