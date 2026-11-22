from collections.abc import Mapping
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session as DbSession

from ..models import MailCredential
from .transports import (
    DisabledMailTransport,
    FakeMailTransport,
    GraphMailTransport,
    MailNotConfigured,
    MailTransport,
    SmtpMailTransport,
)


def create_mail_transport(config: Mapping[str, object], *, engine=None) -> MailTransport:
    """生产环境不允许误用测试邮箱传输器。"""
    injected = config.get("MAIL_TRANSPORT_INSTANCE")
    if injected is not None:
        return injected  # type: ignore[return-value]

    transport_name = str(config.get("MAIL_TRANSPORT", "disabled")).lower()
    if transport_name == "fake":
        if not config.get("TESTING") and config.get("APP_ENV") != "development":
            raise RuntimeError("fake MailTransport 只允许显式开发或测试环境使用。")
        return FakeMailTransport()
    if transport_name == "disabled":
        return DisabledMailTransport()
    if transport_name in {"smtp", "netease_smtp"}:
        return SmtpMailTransport(config)
    if transport_name == "graph":
        if engine is None:
            raise RuntimeError("Graph MailTransport 需要应用数据库以安全保存轮换令牌。")

        def cipher() -> Fernet:
            key = config.get("LLM_ENCRYPTION_KEY")
            if not isinstance(key, str):
                raise MailNotConfigured("请配置 LLM_ENCRYPTION_KEY 以加密 Graph refresh token。")
            try:
                return Fernet(key.encode("ascii"))
            except (ValueError, UnicodeError):
                raise MailNotConfigured("Graph token 加密密钥配置无效。") from None

        def load_refresh_token() -> str | None:
            with DbSession(engine) as db:
                stored = db.get(MailCredential, "graph")
                if stored is None:
                    return None
                try:
                    return cipher().decrypt(stored.secret_ciphertext.encode("ascii")).decode("utf-8")
                except (InvalidToken, UnicodeError):
                    raise MailNotConfigured("保存的 Graph refresh token 无法解密。") from None

        def save_refresh_token(token: str) -> None:
            encrypted = cipher().encrypt(token.encode("utf-8")).decode("ascii")
            import time

            with DbSession(engine) as db, db.begin():
                stored = db.get(MailCredential, "graph")
                if stored is None:
                    db.add(MailCredential(provider="graph", secret_ciphertext=encrypted, updated_at_epoch=int(time.time())))
                else:
                    stored.secret_ciphertext = encrypted
                    stored.updated_at_epoch = int(time.time())

        return GraphMailTransport(
            config,
            load_refresh_token=load_refresh_token,
            save_refresh_token=save_refresh_token,
        )
    raise RuntimeError("MAIL_TRANSPORT 必须是 disabled、fake、smtp 或 graph。")
