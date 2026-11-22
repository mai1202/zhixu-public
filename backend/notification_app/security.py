from functools import wraps
from time import time
from typing import Any, Callable, TypeVar
import hmac

import click
from flask import Flask, current_app, g, request
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from .errors import ApiProblem
from .models import Device, DeviceCredential, User, VerifiedAddress, WebSession
from .services.auth_service import csrf_digest, normalize_gdufs_email, session_digest

F = TypeVar("F", bound=Callable[..., Any])


def parse_json_body(schema: type[BaseModel]) -> BaseModel:
    """用 Pydantic 拒绝额外字段，且不把原输入回显到错误响应。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiProblem("validation_error", "请求内容必须是 JSON 对象。", 422)
    try:
        return schema.model_validate(body)
    except ValidationError:
        raise ApiProblem("validation_error", "请求内容不符合接口格式。", 422) from None


def validate_request_origin() -> None:
    """若浏览器提供 Origin，则只接受部署配置中的可信来源。"""
    origin = request.headers.get("Origin")
    if origin is None:
        return
    allowed = current_app.config.get("TRUSTED_ORIGINS", ())
    if origin.rstrip("/") not in allowed:
        raise ApiProblem("origin_not_allowed", "请求来源未获允许。", 403)


def validate_device_contract() -> None:
    """拒绝协议版本不匹配的桌面端，并提供明确的升级错误代码。"""
    required = str(current_app.config.get("DEVICE_CONTRACT_VERSION", "1.0"))
    supplied = request.headers.get("X-Notification-Contract-Version", "")
    if supplied != required:
        raise ApiProblem(
            "client_upgrade_required",
            "本机客户端协议版本已过期，请升级客户端后重试。",
            426,
            {"required_contract_version": required},
        )


def _load_identity() -> dict[str, Any] | None:
    cookie_name = str(current_app.config["SESSION_COOKIE_NAME"])
    token = request.cookies.get(cookie_name)
    if not token or len(token) > 128:
        return None

    token_hash = session_digest(token)
    engine = current_app.extensions["notification_db_engine"]
    now = int(time())
    query = (
        select(
            WebSession.id.label("session_id"),
            WebSession.user_id.label("user_id"),
            WebSession.expires_at_epoch.label("expires_at_epoch"),
            WebSession.revoked_at_epoch.label("revoked_at_epoch"),
            WebSession.persistent.label("persistent"),
            WebSession.last_seen_at_epoch.label("last_seen_at_epoch"),
            User.primary_email.label("email"),
            User.is_admin.label("is_admin"),
            User.is_active.label("is_active"),
        )
        .join(User, User.id == WebSession.user_id)
        .where(WebSession.token_digest == token_hash)
    )
    with DbSession(engine) as db:
        row = db.execute(query).mappings().one_or_none()
    if (
        row is None
        or (not row["persistent"] and row["expires_at_epoch"] <= now)
        or row["revoked_at_epoch"] is not None
        or not row["is_active"]
    ):
        return None

    # 仅升级尚未过期的旧会话；长期会话的续期点不等于强制退出期限。
    ttl = int(current_app.config['SESSION_COOKIE_TTL_SECONDS'])
    renew = not row['persistent'] or row['expires_at_epoch'] <= now + max(1, ttl // 4)

    key = str(current_app.config["AUTH_HMAC_KEY"]).encode("utf-8")
    return {
        "session_id": row["session_id"],
        "user_id": row["user_id"],
        "email": row["email"],
        "is_admin": bool(row["is_admin"]),
        "renew_cookie": renew,
        "update_activity": renew or (row['last_seen_at_epoch'] or 0) <= now - 300,
        "session_expires_at_epoch": now + ttl if renew else row['expires_at_epoch'],
        "csrf_token": hmac.new(
            key,
            f"csrf:{token}".encode("utf-8"),
            "sha256",
        ).hexdigest(),
    }


def _maintain_identity(identity: dict[str, Any]) -> bool:
    """来源与 CSRF 通过后才持久续期，拒绝的写请求不能吞掉旧 Cookie 的续期机会。"""
    if not identity['update_activity']:
        return True
    values = {'last_seen_at_epoch': int(time())}
    if identity['renew_cookie']:
        values.update(persistent=True, expires_at_epoch=identity['session_expires_at_epoch'])
    with DbSession(current_app.extensions['notification_db_engine']) as db:
        changed = db.execute(update(WebSession).where(
            WebSession.id == identity['session_id'],
            WebSession.revoked_at_epoch.is_(None),
        ).values(**values))
        db.commit()
        # 并发撤销已发生时不得刷新 Cookie 或恢复已撤销会话。
        return changed.rowcount == 1


def authenticated(*, csrf: bool = False, admin: bool = False):
    """将 Web 会话、管理员角色和写请求 CSRF 检查集中在路由边界。"""
    def decorator(function: F) -> F:
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any):
            identity = _load_identity()
            if identity is None:
                raise ApiProblem("authentication_required", "请先验证邮箱登录。", 401)
            if admin and not identity["is_admin"]:
                raise ApiProblem("admin_required", "当前账户无管理员权限。", 403)

            if csrf:
                validate_request_origin()
                supplied = request.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(identity["csrf_token"], supplied):
                    raise ApiProblem("csrf_failed", "请求安全校验失败，请刷新后重试。", 403)

            if not _maintain_identity(identity):
                raise ApiProblem('authentication_required', '请先验证邮箱登录。', 401)
            g.auth_identity = identity
            return function(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    return decorator


def device_authenticated(*, scope: str):
    """校验短时设备令牌、撤销状态与最小权限范围。"""
    def decorator(function: F) -> F:
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any):
            validate_device_contract()
            authorization = request.headers.get("Authorization", "")
            scheme, separator, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not separator or not token or len(token) > 256:
                raise ApiProblem("device_token_invalid", "需要有效的设备访问凭证。", 401)

            key = current_app.config.get("AUTH_HMAC_KEY")
            if not isinstance(key, str) or len(key.encode("utf-8")) < 32:
                raise ApiProblem("auth_not_configured", "服务尚未配置设备认证密钥。", 503)
            digest = hmac.new(
                key.encode("utf-8"),
                f"device-access:{token}".encode("utf-8"),
                "sha256",
            ).hexdigest()
            engine = current_app.extensions["notification_db_engine"]
            now = int(time())
            with DbSession(engine) as db:
                row = db.execute(
                    select(DeviceCredential, Device)
                    .join(Device, Device.id == DeviceCredential.device_id)
                    .where(
                        DeviceCredential.access_digest == digest,
                        DeviceCredential.access_expires_at_epoch > now,
                        DeviceCredential.revoked_at_epoch.is_(None),
                        Device.status == "active",
                    )
                ).one_or_none()
            if row is None:
                raise ApiProblem("device_token_invalid", "设备访问凭证无效、过期或已撤销。", 401)
            credential, device = row
            if scope not in credential.scopes_json:
                raise ApiProblem("device_scope_denied", "设备凭证没有执行此操作的权限。", 403)
            g.device_identity = {
                "device_id": device.id,
                "user_id": device.user_id,
                "scopes": list(credential.scopes_json),
            }
            return function(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    return decorator


def register_account_commands(app: Flask) -> None:
    """首次管理员只能从命令行授予给已验证邮箱，禁止首个注册者自动升级。"""
    @app.cli.group("account")
    def account_group() -> None:
        """管理已验证账户的部署级权限。"""

    @account_group.command("grant-admin")
    @click.argument("email")
    def grant_admin(email: str) -> None:
        try:
            _normalized, email_key = normalize_gdufs_email(email)
        except ApiProblem as error:
            raise click.ClickException(error.message) from None

        engine = app.extensions["notification_db_engine"]
        with DbSession(engine) as db:
            user = db.scalar(
                select(User)
                .join(VerifiedAddress, VerifiedAddress.user_id == User.id)
                .where(
                    VerifiedAddress.email_key == email_key,
                    User.is_active.is_(True),
                )
            )
            if user is None:
                raise click.ClickException("未找到该地址对应的已验证账户。")
            user.is_admin = True
            db.commit()
        click.echo("已授予已验证账户管理员权限。")
