#!/usr/bin/env bash
set -euo pipefail

# 安装锁定依赖；Playwright 浏览器由单独选项显式下载。
# 使用复制模式，兼容同步盘及不支持硬链接的目录。
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
uv --directory "$repo_root/backend" sync --locked --link-mode copy
uv --directory "$repo_root/desktop-client" sync --extra wechat --locked --link-mode copy
npm --prefix "$repo_root" ci --ignore-scripts
if [[ "${INSTALL_OCR_BROWSER:-0}" == "1" ]]; then
  uv --directory "$repo_root/backend" run --locked python -m playwright install chromium
fi
