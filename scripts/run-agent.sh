#!/usr/bin/env bash
set -euo pipefail

# Pi 仅监听回环地址；令牌只在当前进程环境中存在。
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$repo_root/backend/.venv/bin/python"
[[ -x "$python_bin" ]] || python_bin="$repo_root/runtime/python/bin/python3"
[[ -x "$python_bin" ]] || { echo "找不到 Python 运行时" >&2; exit 2; }
[[ -n "${AUTH_HMAC_KEY:-}" ]] || { echo "缺少 AUTH_HMAC_KEY" >&2; exit 2; }
export PI_BRIDGE_TOKEN="$("$python_bin" -c "import hashlib,hmac,os; print(hmac.new(os.environ['AUTH_HMAC_KEY'].encode(),b'notification-pi-bridge-v1',hashlib.sha256).hexdigest())")"
export PI_CALLBACK_URL="${PI_CALLBACK_URL:-http://127.0.0.1:8080}"
export PI_AGENT_PORT="${PI_AGENT_PORT:-5011}"
node_bin="$repo_root/runtime/node/node"
[[ -x "$node_bin" ]] || node_bin="$(command -v node)"
cd "$repo_root"
exec "$node_bin" agent-service/src/server.mjs
