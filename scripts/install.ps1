param(
    [switch]$InstallOcrBrowser
)

# 在仓库根目录安装锁定的 Python 与 npm 依赖，不启动服务或配置自启。
# OneDrive 目录使用复制模式，避免云文件与 uv 硬链接不兼容。
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $repoRoot "backend")
try {
    uv sync --locked --link-mode copy
    if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败，退出码 $LASTEXITCODE。" }
    if ($InstallOcrBrowser) {
        & .\.venv\Scripts\python.exe -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium 安装失败，退出码 $LASTEXITCODE。" }
    }
    # 采集器包含时区数据和微信读取依赖；维护者一次安装，最终用户使用发布包。
    uv sync --project (Join-Path $repoRoot "desktop-client") --extra wechat --locked --link-mode copy
    if ($LASTEXITCODE -ne 0) { throw "采集器依赖安装失败，退出码 $LASTEXITCODE。" }
}
finally {
    Pop-Location
}

Push-Location $repoRoot
try {
    npm ci --ignore-scripts
    if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败，退出码 $LASTEXITCODE。" }
}
finally {
    Pop-Location
}
