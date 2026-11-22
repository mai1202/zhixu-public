#!/usr/bin/env bash
set -euo pipefail

# 只迁移 DATABASE_URL 指向的库，不自动回滚或覆盖备份。
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
uv --directory "$repo_root/backend" run --locked alembic -c "$repo_root/backend/alembic.ini" upgrade head
