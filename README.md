# Zhixu Deployment

This source package includes the Vue frontend, Flask backend, Pi agent service, and Windows notification collector. Runtime data, credentials, dependencies, and build outputs are not included.

## Requirements

- Windows with PowerShell 7 for the desktop launcher.
- Python 3.12–3.14 and uv.
- Node.js 22.19 or later, with npm.

## Local setup

Run the following commands from the project root:

```powershell
./scripts/install.ps1
npm run build
```

Double-click `启动.cmd`, choose a new data directory, enable **比赛演示模式** (competition demo mode), and start the service. The launcher initializes the database and local encryption keys, starts the API, Pi agent, and background worker, and opens a local demo session. SMTP and a production test account are not required for this mode. Use the launcher's stop button to shut down these services.

Configure your own model endpoint and API key in the application's model settings to enable AI features. Without a model connection, the interface and manual task management remain available.

The installer uses uv's copy mode to support OneDrive folders. The application stores generated local settings and user data in the data directory selected in the launcher.

## Optional OCR

Large third-party OCR resources are downloaded separately:

```powershell
./backend/.venv/Scripts/python.exe scripts/download_ocr.py
./backend/.venv/Scripts/python.exe -m playwright install chromium
npm run build
```

The downloader checks every file against the bundled size and SHA-256 manifest. It downloads the tiny model by default; use `--model all` for both model sizes. A mirror can be supplied with `--base-url https://your-host/ocr-runtime/`. The default download endpoint is hosted by the project and must remain available. Text-based workflows do not require OCR resources.

Third-party versions and license notices are in `packages/ocr-runtime/THIRD_PARTY_NOTICES.md`.

## Optional Windows collection

The collector source is in `desktop-client/notification_client/`; its entry point is `采集客户端.cmd`. Live WeChat collection additionally requires the adapted `wechat-cli 0.2.4` component and a compatible local WeChat installation. That component and private WeChat configuration are not bundled. `scripts/wechat_component.py` provides the integration boundary. Devices collect nothing until the user selects groups.

For a basic demonstration, enter notifications manually or use the Webhook integration instead of configuring WeChat collection.

## Docker deployment

The included `Dockerfile` and `docker-compose.yml` run the API, migration job, Pi agent, and worker with persistent data volumes.

1. Copy `deploy/.env.example` to `.env.production` and replace the placeholder values. Set your HTTPS origin, model credentials, and optional mail settings. Disable SMTP and inbound IMAP if they are not configured.
2. Generate a random `AUTH_HMAC_KEY` of at least 32 bytes and a separate Fernet-compatible `LLM_ENCRYPTION_KEY`. Preserve both when upgrading an existing database.
3. Create `.env.agent` with `PI_BRIDGE_TOKEN`, derived as HMAC-SHA256 of the UTF-8 string `notification-pi-bridge-v1` using the same `AUTH_HMAC_KEY`. Use the lowercase hexadecimal digest.
4. If OCR is needed, download its resources before building the image.
5. Run `docker compose up -d --build`. The API is published only on `127.0.0.1:8080`; configure an HTTPS reverse proxy for public access and set `WAITRESS_TRUSTED_PROXY` to the actual trusted proxy address.

The application does not load `.env` files automatically outside Docker or the desktop launcher. Production email login requires a working mail transport. The local demo login is available only in development mode on loopback addresses. Never commit environment files or user data, and do not use `docker compose down -v` when preserving an existing installation.

## Basic checks

```powershell
npm run typecheck
Push-Location backend
./.venv/Scripts/python.exe -m unittest discover -s tests -v
Pop-Location
```

This compact package retains seven application and migration checks. Full regression suites, private deployment tools, and development history are omitted.
