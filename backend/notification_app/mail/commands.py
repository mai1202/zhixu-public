from time import time

import click
from cryptography.fernet import Fernet
from flask import current_app
from sqlalchemy.orm import Session as DbSession

from ..models import MailCredential
from .oauth import GraphOAuthError, authorize_with_device_code


def _store_refresh_token(token: str) -> None:
    """使用应用密钥加密保存 OAuth refresh token。"""
    raw_key = current_app.config.get("LLM_ENCRYPTION_KEY")
    if not isinstance(raw_key, str):
        raise click.ClickException("请配置 LLM_ENCRYPTION_KEY 后再授权 Graph。")
    try:
        encrypted = Fernet(raw_key.encode("ascii")).encrypt(token.encode("utf-8")).decode("ascii")
    except (ValueError, UnicodeError):
        raise click.ClickException("LLM_ENCRYPTION_KEY 格式无效。") from None
    engine = current_app.extensions["notification_db_engine"]
    with DbSession(engine) as db, db.begin():
        credential = db.get(MailCredential, "graph")
        if credential is None:
            db.add(MailCredential(provider="graph", secret_ciphertext=encrypted, updated_at_epoch=int(time())))
        else:
            credential.secret_ciphertext = encrypted
            credential.updated_at_epoch = int(time())


def register_mail_commands(app) -> None:
    """注册只供服务器运营者运行的 Graph delegated 授权和本地撤销命令。"""

    @app.cli.command("mail-graph-authorize")
    def graph_authorize() -> None:
        client_id = str(current_app.config.get("GRAPH_CLIENT_ID", ""))
        tenant = str(current_app.config.get("GRAPH_TENANT_ID", "consumers"))
        client_secret = str(current_app.config.get("GRAPH_CLIENT_SECRET", ""))
        try:
            token, scope = authorize_with_device_code(
                client_id=client_id,
                tenant=tenant,
                client_secret=client_secret,
                timeout=int(current_app.config.get("GRAPH_TIMEOUT_SECONDS", 20)),
            )
            _store_refresh_token(token)
        except GraphOAuthError as error:
            raise click.ClickException(f"Graph 授权失败（{error.code}）。") from None
        click.echo(f"Graph refresh token 已加密保存；授权范围：{scope or '由 Microsoft 返回'}。")

    @app.cli.command("mail-graph-revoke-local")
    def graph_revoke_local() -> None:
        """删除本地 refresh token；重启 worker 后停止使用缓存 access token。"""
        engine = current_app.extensions["notification_db_engine"]
        with DbSession(engine) as db, db.begin():
            credential = db.get(MailCredential, "graph")
            if credential is not None:
                db.delete(credential)
        click.echo("本地 Graph refresh token 已删除。请重启 worker，并在 Microsoft 账户/租户中撤销应用授权。")
