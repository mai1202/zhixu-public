import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from flask import Flask
from sqlalchemy.engine import URL

from notification_app import create_app
from notification_app.mail.transports import FakeMailTransport
from cryptography.fernet import Fernet


class AppHarness:
    def __init__(self, app: Flask, temp_dir: tempfile.TemporaryDirectory) -> None:
        self.app = app
        self.client = app.test_client()
        self.temp_dir = temp_dir
        self.mail: FakeMailTransport = app.extensions["notification_mail_transport"]

    def close(self) -> None:
        self.app.extensions["notification_db_engine"].dispose()
        self.temp_dir.cleanup()


def make_harness(**config_overrides) -> AppHarness:
    repo_root = Path(__file__).resolve().parents[2]
    # 测试数据库和私有附件写入系统临时目录，避免污染仓库、同步盘或受限工作区。
    temp_dir = tempfile.TemporaryDirectory(
        prefix="通知管理测试_",
        ignore_cleanup_errors=True,
    )
    database_path = Path(temp_dir.name) / "app.sqlite3"
    database_url = URL.create(
        "sqlite",
        database=str(database_path),
    ).render_as_string(hide_password=False)
    app = create_app(
        {
            "TESTING": True,
            # 业务回归使用合成工具，独立审查链路由专门用例启用并验证。
            "AGENT_TOOL_REVIEW_ENABLED": False,
            "PRIVACY_CONSENT_REQUIRED": False,
            "APP_ENV": "testing",
            "DATA_DIR": temp_dir.name,
            "CLIENT_DOWNLOAD_DIR": str(Path(temp_dir.name) / "downloads"),
            "DATABASE_URL": database_url,
            "AUTH_HMAC_KEY": "unit-test-auth-key-with-at-least-32-bytes",
            "AUTH_ALLOWED_EMAILS": (),
            "LLM_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
            "DEFAULT_LLM_API_KEY": "",
            "MAIL_TRANSPORT": "fake",
            "TRUSTED_ORIGINS": ("http://127.0.0.1:5173",),
            **config_overrides,
        }
    )

    backend_dir = repo_root / "backend"
    alembic_config = Config(str(backend_dir / "alembic.ini"))
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    return AppHarness(app, temp_dir)


def login(client, mail: FakeMailTransport, email: str) -> tuple[dict, str]:
    requested = client.post("/api/v1/auth/request-code", json={"email": email})
    if requested.status_code != 202:
        raise AssertionError(f"request-code failed: {requested.status_code}")
    challenge_id = requested.json["challenge_id"]
    code = next(
        item.code for item in reversed(mail.sent_codes)
        if item.challenge_id == challenge_id
    )
    verified = client.post(
        "/api/v1/auth/verify-code",
        json={"challenge_id": challenge_id, "code": code},
    )
    if verified.status_code != 200:
        raise AssertionError(f"verify-code failed: {verified.status_code}")
    return verified.json["user"], verified.json["csrf_token"]
