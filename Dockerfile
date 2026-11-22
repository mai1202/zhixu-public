# 仅在构建容器中安装 npm 依赖，使用锁文件保证 Linux 原生包匹配。
FROM node:22-bookworm-slim AS npm-base
WORKDIR /app
COPY package.json package-lock.json ./
COPY frontend/package.json ./frontend/package.json
COPY agent-service/package.json ./agent-service/package.json
COPY packages/contracts/package.json ./packages/contracts/package.json

FROM npm-base AS frontend-build
RUN npm ci --no-audit --no-fund
COPY frontend ./frontend
COPY packages ./packages
ENV NODE_OPTIONS=--max-old-space-size=512
RUN npm run build

FROM npm-base AS agent-deps
RUN npm ci --omit=dev --workspace @notification-management/agent-service --no-audit --no-fund

# 固定 Python 主次版本；uv 只负责按项目锁文件安装容器内依赖。
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv
FROM python:3.14-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    OCR_CHROMIUM_EXECUTABLE=/opt/chromium \
    HOME=/tmp/home \
    PATH="/app/backend/.venv/bin:$PATH"
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=npm-base /usr/local/bin/node /usr/local/bin/node
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-cache
# OCR 浏览器与系统库同样只安装在镜像内；不占用宿主机的运行时配置。
RUN python -m playwright install --with-deps chromium --only-shell
# 固定入口指向本次 Playwright 安装的 headless shell，避免误判完整浏览器缺失。
RUN python -c "from pathlib import Path; paths=list(Path('/opt/playwright').glob('chromium_headless_shell-*/chrome-linux/headless_shell')); assert len(paths)==1; Path('/opt/chromium').symlink_to(paths[0])"
COPY backend/notification_app ./notification_app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini
COPY --from=agent-deps /app/node_modules /app/node_modules
COPY agent-service/package.json /app/agent-service/package.json
COPY agent-service/src /app/agent-service/src
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist
COPY --from=frontend-build /app/packages/ocr-runtime/public /app/packages/ocr-runtime/public
# 服务统一以无特权账号运行；命名卷首次创建时继承目录所有者。
RUN groupadd --gid 10001 notification \
    && useradd --uid 10001 --gid 10001 --no-create-home notification \
    && mkdir -p /app/data/private/pi \
    && chown -R 10001:10001 /app/data
USER 10001:10001
CMD ["python", "-m", "notification_app.server"]
