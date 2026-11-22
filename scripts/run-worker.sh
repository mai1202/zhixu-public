#!/usr/bin/env bash
set -euo pipefail

# Worker 与 API 分开运行，便于各自重启和收集状态。
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root/backend"
exec uv run --locked python -m notification_app.jobs.worker
