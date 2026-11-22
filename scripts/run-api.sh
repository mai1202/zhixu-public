#!/usr/bin/env bash
set -euo pipefail

# Waitress 前台服务入口，默认仅绑定本机回环地址。
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root/backend"
exec uv run --locked python -m notification_app.server
