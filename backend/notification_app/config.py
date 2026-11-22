import os
from pathlib import Path

from sqlalchemy.engine import URL


def _nonnegative_int(name: str, default: int, maximum: int) -> int:
    raw_value = os.environ.get(name)
    value = default if raw_value in (None, "") else int(raw_value)
    if value < 0 or value > maximum:
        raise ValueError(f"{name} 必须在 0 到 {maximum} 之间")
    return value


def _positive_int(name: str, default: int, maximum: int) -> int:
    raw_value = os.environ.get(name)
    value = default if raw_value is None else int(raw_value)
    if value < 1 or value > maximum:
        raise ValueError(f"{name} 必须在 1 到 {maximum} 之间")
    return value


def _login_allowlist() -> tuple[str, ...]:
    """读取登录邮箱白名单；未设置时不额外限制邮箱名单，学校邮箱规则仍生效。

    留空或 * 表示关闭名单限制，仅建议开发/测试环境使用。
    """
    raw_value = os.environ.get("AUTH_ALLOWED_EMAILS")
    if raw_value is None:
        raw_value = ""
    if raw_value.strip() in {"", "*"}:
        return ()
    return tuple(
        item.strip().lower()
        for item in raw_value.split(",")
        if item.strip()
    )


def load_config() -> dict[str, object]:
    """读取部署配置；没有环境变量时仅使用本机开发数据目录。"""
    project_root = Path(__file__).resolve().parents[2]
    data_dir = Path(os.environ.get("DATA_DIR", project_root / "data")).expanduser().resolve()
    app_env = os.environ.get("APP_ENV", "development")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        database_url = URL.create(
            "sqlite",
            database=str(data_dir / "notification_management.sqlite3"),
        ).render_as_string(hide_password=False)

    origin_value = os.environ.get("TRUSTED_ORIGINS")
    if origin_value is None and app_env != "production":
        trusted_origins = (
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:5000",
            "http://localhost:5000",
        )
    else:
        trusted_origins = tuple(
            origin.strip().rstrip("/")
            for origin in (origin_value or "").split(",")
            if origin.strip()
        )

    return {
        # 必须先在目标数据库完成并发验收；用户授权与部署验收是两个独立条件。
        "PLANNING_AUTOMATIC_VERIFIED": os.environ.get("PLANNING_AUTOMATIC_VERIFIED", "false").lower() in {"true", "1"},
        # 通知要求直接校验生效，后台托管仍受账户自动维护开关约束。
        "APP_ENV": app_env,
        # 仅管理员预置的测试账户可用密码登录；默认关闭，不影响学校邮箱验证码。
        "TEST_PASSWORD_LOGIN_ENABLED": os.environ.get("TEST_PASSWORD_LOGIN_ENABLED", "false").lower() in {"true", "1"},
        "OPERATIONS_ALERT_EMAIL": os.environ.get("OPERATIONS_ALERT_EMAIL", ""),
        "OPERATIONS_TEST_EMAIL": os.environ.get("OPERATIONS_TEST_EMAIL", ""),
        "AGENT_TOOL_REVIEW_ENABLED": os.environ.get("AGENT_TOOL_REVIEW_ENABLED", "true").lower() in {"true", "1"},
        "FILE_SANDBOX_ENABLED": os.environ.get("FILE_SANDBOX_ENABLED", "false").lower() in {"true", "1"},
        "FILE_SANDBOX_IMAGE": os.environ.get("FILE_SANDBOX_IMAGE", "python:3.13-alpine"),
        "PRIVACY_CONSENT_REQUIRED": os.environ.get("PRIVACY_CONSENT_REQUIRED", "true").lower() in {"true", "1"},
        "PRIVACY_CONTACT": os.environ.get("PRIVACY_CONTACT", ""),
        "DATA_DIR": str(data_dir),
        "DATABASE_URL": database_url,
        "FRONTEND_DIST": str(project_root / "frontend" / "dist"),
        "AUTH_HMAC_KEY": os.environ.get("AUTH_HMAC_KEY"),
        "AUTH_ALLOWED_EMAILS": _login_allowlist(),
        "MAIL_TRANSPORT": os.environ.get("MAIL_TRANSPORT", "disabled"),
        "ONBOARDING_REMINDER_DELAY_SECONDS": _positive_int('ONBOARDING_REMINDER_DELAY_SECONDS', 86400, 30 * 86400),
        # 收信与联网由部署方单独配置，不把运营邮箱或搜索服务凭证交给模型。
        "INBOUND_MAIL_ENABLED": os.environ.get("INBOUND_MAIL_ENABLED", "false").lower() in {"1", "true", "yes"},
        "INBOUND_IMAP_HOST": os.environ.get("INBOUND_IMAP_HOST", ""),
        "INBOUND_IMAP_PORT": _positive_int("INBOUND_IMAP_PORT", 993, 65535),
        "INBOUND_IMAP_USER": os.environ.get("INBOUND_IMAP_USER", ""),
        "INBOUND_IMAP_SECRET": os.environ.get("INBOUND_IMAP_SECRET", ""),
        "INBOUND_IMAP_FOLDER": os.environ.get("INBOUND_IMAP_FOLDER", "INBOX"),
        "WEB_SEARCH_API_KEY": os.environ.get("WEB_SEARCH_API_KEY", ""),
        # DuckDuckGo 是默认联网入口；Brave 在配置密钥后参与回退或联合检索。
        "DUCKDUCKGO_ENABLED": os.environ.get("DUCKDUCKGO_ENABLED", "true").lower()
        in {"1", "true", "yes"},
        "DUCKDUCKGO_REGION": os.environ.get("DUCKDUCKGO_REGION", "cn-zh"),
        "DUCKDUCKGO_TIMEOUT_SECONDS": _positive_int("DUCKDUCKGO_TIMEOUT_SECONDS", 8, 30),
        "SEARCH_CACHE_SECONDS": _positive_int("SEARCH_CACHE_SECONDS", 120, 3600),
        "SEARCH_PROVIDER_FAILURE_THRESHOLD": _positive_int("SEARCH_PROVIDER_FAILURE_THRESHOLD", 3, 20),
        "SEARCH_PROVIDER_COOLDOWN_SECONDS": _positive_int("SEARCH_PROVIDER_COOLDOWN_SECONDS", 120, 3600),
        # 向量索引是可选增强；未配置时继续使用关键词检索并在诊断中说明。
        "EMBEDDING_ENABLED": os.environ.get("EMBEDDING_ENABLED", "false").lower() in {"1", "true", "yes"},
        "EMBEDDING_ENDPOINT": os.environ.get("EMBEDDING_ENDPOINT", ""),
        "EMBEDDING_MODEL": os.environ.get("EMBEDDING_MODEL", ""),
        "EMBEDDING_API_KEY": os.environ.get("EMBEDDING_API_KEY", ""),
        "EMBEDDING_DIMENSIONS": _nonnegative_int("EMBEDDING_DIMENSIONS", 0, 8192),
        "EMBEDDING_TIMEOUT_SECONDS": _positive_int("EMBEDDING_TIMEOUT_SECONDS", 20, 120),
        "EMBEDDING_ALLOW_CLOUD": os.environ.get("EMBEDDING_ALLOW_CLOUD", "false").lower() in {"1", "true", "yes"},
        "RETRIEVAL_CHUNK_CHARS": _positive_int("RETRIEVAL_CHUNK_CHARS", 600, 4000),
        "RETRIEVAL_CHUNK_OVERLAP": _positive_int("RETRIEVAL_CHUNK_OVERLAP", 80, 1000),
        "RETRIEVAL_TOP_K": _positive_int("RETRIEVAL_TOP_K", 8, 50),
        # 单轮预算：普通任务与复杂请求均允许 1000 次工具；结果体积和耗时仍受限。
        "AGENT_TOOL_BUDGET": _positive_int("AGENT_TOOL_BUDGET", 1000, 1000),
        "AGENT_COMPLEX_TOOL_BUDGET": _positive_int("AGENT_COMPLEX_TOOL_BUDGET", 1000, 1000),
        "AGENT_RUN_BUDGET_SECONDS": _positive_int("AGENT_RUN_BUDGET_SECONDS", 300, 3000),
        "AGENT_TOOL_RESULT_MAX_CHARS": _positive_int("AGENT_TOOL_RESULT_MAX_CHARS", 60000, 1000000),
        # SMTP 由部署方提供真实 host、发件地址和客户端授权码，不把网易参数写死在代码里。
        "SMTP_HOST": os.environ.get("SMTP_HOST", ""),
        "SMTP_PORT": _positive_int("SMTP_PORT", 587, 65535),
        "SMTP_AUTH_USER": os.environ.get("SMTP_AUTH_USER", ""),
        "SMTP_AUTH_SECRET": os.environ.get("SMTP_AUTH_SECRET", ""),
        "SMTP_FROM": os.environ.get("SMTP_FROM", ""),
        "SMTP_STARTTLS": os.environ.get("SMTP_STARTTLS", "true").lower() in {"1", "true", "yes"},
        "SMTP_USE_SSL": os.environ.get("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"},
        "SMTP_TIMEOUT_SECONDS": _positive_int("SMTP_TIMEOUT_SECONDS", 20, 120),
        "GRAPH_CLIENT_ID": os.environ.get("GRAPH_CLIENT_ID", ""),
        "GRAPH_CLIENT_SECRET": os.environ.get("GRAPH_CLIENT_SECRET", ""),
        "GRAPH_TENANT_ID": os.environ.get("GRAPH_TENANT_ID", "consumers"),
        "GRAPH_REFRESH_TOKEN": os.environ.get("GRAPH_REFRESH_TOKEN", ""),
        "GRAPH_TIMEOUT_SECONDS": _positive_int("GRAPH_TIMEOUT_SECONDS", 20, 120),
        "LLM_ENCRYPTION_KEY": os.environ.get("LLM_ENCRYPTION_KEY"),
        # 管理员可在服务端统一配置一个默认模型；登录用户无需自行填写密钥。
        "DEFAULT_LLM_PROVIDER": os.environ.get("DEFAULT_LLM_PROVIDER", "deepseek"),
        "DEFAULT_LLM_MODEL": os.environ.get("DEFAULT_LLM_MODEL", "deepseek-flash"),
        "DEFAULT_LLM_API_KEY": os.environ.get("DEFAULT_LLM_API_KEY", ""),
        "DEFAULT_LLM_TIMEOUT_SECONDS": _positive_int("DEFAULT_LLM_TIMEOUT_SECONDS", 60, 120),
        # 仅限制平台付费密钥；每天北京时间零点重置，两个明确指定的账户豁免。
        # 兼容旧部署环境键；实际限制已由逐调用人民币账本固定为每日3元，此值不再参与拦截。
        "PLATFORM_DAILY_TOKEN_LIMIT": _positive_int("PLATFORM_DAILY_TOKEN_LIMIT", 2_000_000, 2_000_000),
        # Windows 采集客户端下载包可放在数据目录，也可由部署方提供外部下载地址。
        "CLIENT_DOWNLOAD_DIR": str(
            Path(os.environ.get("CLIENT_DOWNLOAD_DIR", data_dir / "downloads")).expanduser().resolve()
        ),
        "CLIENT_DOWNLOAD_FILENAME": os.environ.get("CLIENT_DOWNLOAD_FILENAME", ""),
        "CLIENT_DOWNLOAD_URL": os.environ.get("CLIENT_DOWNLOAD_URL", ""),
        "LLM_ALLOWED_CUSTOM_HOSTS": tuple(
            host.strip().lower()
            for host in os.environ.get("LLM_ALLOWED_CUSTOM_HOSTS", "").split(",")
            if host.strip()
        ),
        "OCR_STORAGE_DIR": str(data_dir / "private" / "ocr"),
        "OCR_MAX_UPLOAD_BYTES": 20 * 1024 * 1024,
        "OCR_MAX_IMAGE_PIXELS": 25_000_000,
        "OCR_RUNNER_TIMEOUT_SECONDS": _positive_int("OCR_RUNNER_TIMEOUT_SECONDS", 180, 900),
        "WORKER_LEASE_SECONDS": _positive_int("WORKER_LEASE_SECONDS", 600, 3600),
        "SERVER_LISTEN": os.environ.get("SERVER_LISTEN", "127.0.0.1:8080"),
        "SERVER_THREADS": _positive_int("SERVER_THREADS", 8, 256),
        "WAITRESS_TRUSTED_PROXY": os.environ.get("WAITRESS_TRUSTED_PROXY", ""),
        "DEVICE_CONTRACT_VERSION": "1.0",
        # Pi 仅在服务端回环监听；浏览器不能直接访问内部执行接口。
        "PI_AGENT_URL": os.environ.get("PI_AGENT_URL", "http://127.0.0.1:5011"),
        "PI_RUN_TIMEOUT_SECONDS": _positive_int("PI_RUN_TIMEOUT_SECONDS", 540, 3000),
        "OCR_CHROMIUM_EXECUTABLE": os.environ.get("OCR_CHROMIUM_EXECUTABLE"),
        "TRUSTED_ORIGINS": trusted_origins,
        "SESSION_COOKIE_NAME": "nm_session",
        # 兼容原环境变量：现在表示长期网页登录的续期周期，不再是强制注销期限。
        "SESSION_COOKIE_TTL_SECONDS": _positive_int("SESSION_COOKIE_TTL_SECONDS", 8 * 60 * 60, 30 * 24 * 60 * 60),
        "EMAIL_CODE_TTL_SECONDS": _positive_int("EMAIL_CODE_TTL_SECONDS", 10 * 60, 60 * 60),
        "EMAIL_CODE_RESEND_SECONDS": _positive_int("EMAIL_CODE_RESEND_SECONDS", 60, 60 * 60),
        "EMAIL_CODE_MAX_ATTEMPTS": _positive_int("EMAIL_CODE_MAX_ATTEMPTS", 5, 20),
        # 同一邮箱三个独立窗口共同生效，避免短时连发，也允许正常重试。
        "AUTH_RATE_EMAIL_5MIN": _positive_int("AUTH_RATE_EMAIL_5MIN", 3, 1000),
        "AUTH_RATE_EMAIL_HOURLY": _positive_int("AUTH_RATE_EMAIL_HOURLY", 20, 1000),
        "AUTH_RATE_EMAIL_DAILY": _positive_int("AUTH_RATE_EMAIL_DAILY", 50, 100000),
        # 反代后的多个账户可能共用来源 IP，粗粒度保护不能盖过邮箱自身配额。
        "AUTH_RATE_IP_HOURLY": _positive_int("AUTH_RATE_IP_HOURLY", 100, 10000),
        "AUTH_RATE_GLOBAL_HOURLY": _positive_int("AUTH_RATE_GLOBAL_HOURLY", 100, 100000),
        "MAX_CONTENT_LENGTH": 20 * 1024 * 1024,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": os.environ.get("SESSION_COOKIE_SECURE", "").lower()
        in {"1", "true", "yes"},
    }
