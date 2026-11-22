import hashlib
import hmac
import re
import secrets
import time
from uuid import UUID, uuid4

from flask import current_app, request
from sqlalchemy import case, select, update
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..mail.transports import DisabledMailTransport, MailNotConfigured
from ..models import EmailChallenge, User, VerifiedAddress, WebSession, UserPrivacyConsent
from .rate_limit import consume_email_request_limits

LOCAL_PART_PATTERN = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_\x60{|}~.-]+")


def normalize_gdufs_email(email: str) -> tuple[str, str]:
    """验证单一邮箱地址，并只规范化 domain，不改写 local part。"""
    invalid = ApiProblem(
        "invalid_email",
        "请输入格式正确的广外邮箱地址。",
        422,
    )
    if not isinstance(email, str) or email != email.strip() or any(ch in email for ch in "\r\n\t "):
        raise invalid
    if email.count("@") != 1:
        raise invalid

    local_part, domain = email.split("@", 1)
    if (
        not local_part
        or len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or not LOCAL_PART_PATTERN.fullmatch(local_part)
        or not domain
        or domain.endswith(".")
    ):
        raise invalid

    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise invalid from None

    labels = ascii_domain.split(".")
    if (
        len(ascii_domain) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not re.fullmatch(r"[a-z0-9-]+", label)
            for label in labels
        )
    ):
        raise invalid

    if ascii_domain != "gdufs.edu.cn" and not ascii_domain.endswith(".gdufs.edu.cn"):
        raise ApiProblem(
            "email_domain_not_allowed",
            "仅支持 gdufs.edu.cn 及其子域邮箱。",
            422,
        )

    normalized_email = f"{local_part}@{ascii_domain}"
    return normalized_email, normalized_email


def _allowed_login_emails() -> tuple[str, ...]:
    """返回部署方配置的登录白名单；空元组表示不限制。"""
    raw_value = current_app.config.get("AUTH_ALLOWED_EMAILS") or ()
    if isinstance(raw_value, str):
        raw_value = raw_value.split(",")
    return tuple(
        str(item).strip().lower()
        for item in raw_value
        if str(item).strip()
    )


def _raise_login_email_not_allowed(allowed: tuple[str, ...]) -> None:
    # 公开登录接口不返回真实白名单地址，仅提示使用测试账号。
    raise ApiProblem(
        "login_email_not_allowed",
        "请登录测试账号。",
        403,
    )


def ensure_login_email_allowed(email: str) -> None:
    """白名单在发送验证码和消费验证码两处校验，避免配置变更后旧挑战绕过。"""
    allowed = _allowed_login_emails()
    if allowed and email.strip().lower() not in allowed:
        _raise_login_email_not_allowed(allowed)


def _provision_default_provider(user_id: str) -> None:
    """登录后静默准备服务器默认模型；失败不回滚已验证的登录会话。"""
    try:
        from .provider_service import ensure_default_profiles

        ensure_default_profiles(user_id)
    except Exception:
        current_app.logger.warning("默认模型配置初始化失败 user_id=%s", user_id)


def _auth_key() -> bytes:
    raw_key = current_app.config.get("AUTH_HMAC_KEY")
    if not isinstance(raw_key, str) or len(raw_key.encode("utf-8")) < 32:
        raise ApiProblem(
            "auth_not_configured",
            "服务尚未配置邮箱验证密钥。",
            503,
        )
    return raw_key.encode("utf-8")


def _keyed_digest(purpose: str, value: str, key: bytes | None = None) -> str:
    secret = key or _auth_key()
    return hmac.new(
        secret,
        f"{purpose}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _challenge_digest(challenge_id: str, email_key: str, code: str) -> str:
    value = f"{challenge_id}:{email_key}:{code}"
    return _keyed_digest("email-code", value)


def issue_login_code(email: str, client_ip: str, *, policy_version=None) -> str:
    """创建限时验证码；响应与传输日志都不包含验证码本身。"""
    key = _auth_key()
    # 先校验地址与白名单，再检查发信通道；非白名单地址始终得到明确提示。
    allowed = _allowed_login_emails()
    try:
        email, email_key = normalize_gdufs_email(email)
    except ApiProblem as error:
        # 配置了精确白名单时，其他学校域名也统一按“不允许登录”提示。
        if allowed and error.code == "email_domain_not_allowed":
            _raise_login_email_not_allowed(allowed)
        raise
    ensure_login_email_allowed(email)
    mail_transport = current_app.extensions["notification_mail_transport"]
    if isinstance(mail_transport, DisabledMailTransport):
        raise ApiProblem(
            "mail_not_configured",
            "尚未配置运营发信通道，当前不能发送登录验证码。",
            503,
        )

    engine = current_app.extensions["notification_db_engine"]
    now = int(time.time())
    ttl = int(current_app.config["EMAIL_CODE_TTL_SECONDS"])
    resend_interval = int(current_app.config["EMAIL_CODE_RESEND_SECONDS"])
    max_attempts = int(current_app.config["EMAIL_CODE_MAX_ATTEMPTS"])
    challenge_id = str(uuid4())
    code = f"{secrets.randbelow(100_000_000):08d}"

    with DbSession(engine) as db:
        latest = db.scalar(
            select(EmailChallenge)
            .where(
                EmailChallenge.email_key == email_key,
                EmailChallenge.purpose == "login",
            )
            .order_by(EmailChallenge.issued_at_epoch.desc())
            .limit(1)
        )
        if latest is not None:
            remaining = resend_interval - (now - latest.issued_at_epoch)
            if remaining > 0:
                db.commit()
                raise ApiProblem(
                    "resend_too_soon",
                    "请等待后再重新发送验证码。",
                    429,
                    {"retry_after_seconds": remaining},
                    retry_after=remaining,
                )

        # 先检查一分钟重发间隔，过早重复点击不会消耗任何发送配额。
        consume_email_request_limits(
            db,
            key=key,
            email_key=email_key,
            client_ip=client_ip,
            now_epoch=now,
            limits={
                "email_5min": int(current_app.config["AUTH_RATE_EMAIL_5MIN"]),
                "email_hourly": int(current_app.config["AUTH_RATE_EMAIL_HOURLY"]),
                "email_daily": int(current_app.config["AUTH_RATE_EMAIL_DAILY"]),
                "ip": int(current_app.config["AUTH_RATE_IP_HOURLY"]),
                "global": int(current_app.config["AUTH_RATE_GLOBAL_HOURLY"]),
            },
        )

        # 同一邮箱的新验证码使之前未消费的验证码失效。
        db.execute(
            update(EmailChallenge)
            .where(
                EmailChallenge.email_key == email_key,
                EmailChallenge.purpose == "login",
                EmailChallenge.consumed_at_epoch.is_(None),
            )
            .values(consumed_at_epoch=now)
        )
        db.add(
            EmailChallenge(
                id=challenge_id,
                email=email,
                email_key=email_key,
                purpose="login",
                code_digest=_challenge_digest(challenge_id, email_key, code),
                issued_at_epoch=now,
                expires_at_epoch=now + ttl,
                consumed_at_epoch=None,
                attempts=0,
                policy_version=policy_version,
            )
        )
        db.commit()

    try:
        mail_transport.send_verification_code(email, challenge_id, code, ttl)
    except MailNotConfigured:
        _consume_unsent_challenge(challenge_id, now)
        raise ApiProblem("mail_not_configured", "尚未配置运营发信通道。", 503) from None
    except Exception:
        # 外部发送失败时立即作废挑战；不记录异常文本，避免泄漏地址或验证码。
        _consume_unsent_challenge(challenge_id, now)
        raise ApiProblem("mail_send_failed", "验证码邮件暂时发送失败，请稍后重试。", 502) from None

    return challenge_id


def _consume_unsent_challenge(challenge_id: str, now_epoch: int) -> None:
    engine = current_app.extensions["notification_db_engine"]
    with DbSession(engine) as db:
        db.execute(
            update(EmailChallenge)
            .where(
                EmailChallenge.id == challenge_id,
                EmailChallenge.consumed_at_epoch.is_(None),
            )
            .values(consumed_at_epoch=now_epoch)
        )
        db.commit()


def consume_login_code(challenge_id: UUID, code: str) -> dict[str, object]:
    """原子消费一次验证码并建立普通账户及服务端会话。"""
    key = _auth_key()
    engine = current_app.extensions["notification_db_engine"]
    now = int(time.time())
    challenge_key = str(challenge_id)
    max_attempts = int(current_app.config["EMAIL_CODE_MAX_ATTEMPTS"])
    invalid_code = ApiProblem(
        "invalid_or_expired_code",
        "验证码无效、已过期或已使用。",
        400,
    )

    with DbSession(engine) as db:
        challenge = db.get(EmailChallenge, challenge_key)
        if (
            challenge is None
            or challenge.purpose != "login"
            or challenge.consumed_at_epoch is not None
            or challenge.expires_at_epoch <= now
            or challenge.attempts >= max_attempts
        ):
            raise invalid_code
        ensure_login_email_allowed(challenge.email)

        expected_digest = _challenge_digest(
            challenge_key,
            challenge.email_key,
            code,
        )
        if not hmac.compare_digest(challenge.code_digest, expected_digest):
            attempt_update = db.execute(
                update(EmailChallenge)
                .where(
                    EmailChallenge.id == challenge_key,
                    EmailChallenge.consumed_at_epoch.is_(None),
                    EmailChallenge.expires_at_epoch > now,
                    EmailChallenge.attempts < max_attempts,
                )
                .values(
                    attempts=EmailChallenge.attempts + 1,
                    consumed_at_epoch=case(
                        (EmailChallenge.attempts + 1 >= max_attempts, now),
                        else_=None,
                    ),
                )
            )
            db.commit()
            if attempt_update.rowcount != 1:
                raise invalid_code
            raise invalid_code

        claimed = db.execute(
            update(EmailChallenge)
            .where(
                EmailChallenge.id == challenge_key,
                EmailChallenge.consumed_at_epoch.is_(None),
                EmailChallenge.expires_at_epoch > now,
                EmailChallenge.attempts < max_attempts,
            )
            .values(
                consumed_at_epoch=now,
                attempts=EmailChallenge.attempts + 1,
            )
            .returning(EmailChallenge.email, EmailChallenge.email_key)
        ).one_or_none()
        if claimed is None:
            db.rollback()
            raise invalid_code

        email, email_key = claimed
        return _create_web_session(db, email, email_key, key, now, policy_version=challenge.policy_version)


def _create_web_session(db: DbSession, email: str, email_key: str, key: bytes, now: int, *, policy_version=None) -> dict:
    """复用正式登录的账户创建、会话、CSRF 与默认模型初始化逻辑。"""
    user = db.scalar(select(User).where(User.email_key == email_key))
    if user is None:
        user = User(
            id=str(uuid4()),
            primary_email=email,
            email_key=email_key,
            is_active=True,
            is_admin=False,
            verified_at_epoch=now,
            created_at_epoch=now,
        )
        db.add(user)
        db.flush()
        db.add(
            VerifiedAddress(
                id=str(uuid4()),
                user_id=user.id,
                email=email,
                email_key=email_key,
                is_primary=True,
                verified_at_epoch=now,
            )
        )
    elif not user.is_active:
        db.commit()
        raise ApiProblem(
            "invalid_or_expired_code",
            "验证码无效、已过期或已使用。",
            400,
        )

    if policy_version:
        consent = db.get(UserPrivacyConsent, user.id)
        if consent is None:
            db.add(UserPrivacyConsent(user_id=user.id, policy_version=policy_version, accepted_at_epoch=now))
        else:
            consent.policy_version = policy_version; consent.accepted_at_epoch = now
    token = secrets.token_urlsafe(32)
    session_id = str(uuid4())
    ttl = int(current_app.config["SESSION_COOKIE_TTL_SECONDS"])
    db.add(
        WebSession(
            id=session_id,
            user_id=user.id,
            token_digest=_keyed_digest("web-session", token, key),
            created_at_epoch=now,
            expires_at_epoch=now + ttl,
            revoked_at_epoch=None,
            # 每次登录创建独立会话，不撤销其他浏览器或设备的登录。
            persistent=True,
            user_agent=request.headers.get('User-Agent', '')[:512],
            last_seen_at_epoch=now,
        )
    )
    public_user = {
        "id": user.id,
        "email": user.primary_email,
        "is_admin": bool(user.is_admin),
    }
    db.commit()
    _provision_default_provider(user.id)

    return {
        "user": public_user,
        "session_token": token,
        "csrf_token": _keyed_digest("csrf", token, key),
        "session_id": session_id,
        "expires_in": ttl,
    }


def login_local_test_account(email: str, *, policy_version=None) -> dict | None:
    """开发环境的回环请求可用 @local.test 直接登录，其他邮箱仍走验证码。"""
    if (
        current_app.config.get("APP_ENV") != "development"
        or request.remote_addr not in {"127.0.0.1", "::1"}
        or request.host.split(":", 1)[0].lower() not in {"127.0.0.1", "localhost"}
        or not email.lower().endswith("@local.test")
    ):
        return None
    # 复用原有邮箱格式校验，只替换校验用域名，不冒用真实学校邮箱。
    local_part = email.rsplit("@", 1)[0]
    normalize_gdufs_email(f"{local_part}@gdufs.edu.cn")
    normalized = f"{local_part}@local.test"
    with DbSession(current_app.extensions["notification_db_engine"]) as db:
        return _create_web_session(db, normalized, normalized, _auth_key(), int(time.time()), policy_version=policy_version)


def session_digest(token: str) -> str:
    return _keyed_digest("web-session", token)


def csrf_digest(token: str) -> str:
    return _keyed_digest("csrf", token)


def set_web_session_cookie(response, token: str) -> None:
    """浏览器最多保留 400 天；活跃使用及后台心跳会提前重置期限。"""
    response.set_cookie(
        str(current_app.config['SESSION_COOKIE_NAME']), token,
        max_age=400 * 24 * 60 * 60,
        httponly=True, secure=bool(current_app.config['SESSION_COOKIE_SECURE']),
        samesite='Lax', path='/',
    )
