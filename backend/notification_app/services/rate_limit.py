from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..models import RateLimitBucket


def _increment_bucket(
    db: DbSession,
    bucket_key: str,
    now_epoch: int,
    window_seconds: int,
) -> tuple[int, int]:
    """用数据库原子 upsert 记录窗口，供多个 Web 进程共享同一配额。"""
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        insert = sqlite_insert
    elif dialect == "postgresql":
        insert = postgresql_insert
    else:
        raise RuntimeError("验证码限流仅支持已规划的 SQLite 与 PostgreSQL。")

    expired = RateLimitBucket.window_started_epoch <= now_epoch - window_seconds
    statement = insert(RateLimitBucket).values(
        bucket_key=bucket_key,
        window_started_epoch=now_epoch,
        hits=1,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[RateLimitBucket.bucket_key],
        set_={
            "window_started_epoch": case(
                (expired, now_epoch),
                else_=RateLimitBucket.window_started_epoch,
            ),
            "hits": case(
                (expired, 1),
                else_=RateLimitBucket.hits + 1,
            ),
        },
    ).returning(
        RateLimitBucket.hits,
        RateLimitBucket.window_started_epoch,
    )
    hits, started = db.execute(statement).one()
    return int(hits), int(started)


def consume_email_request_limits(
    db: DbSession,
    *,
    key: bytes,
    email_key: str,
    client_ip: str,
    now_epoch: int,
    limits: dict[str, int],
) -> None:
    """对邮箱的三个窗口及来源 IP、全局发送量同时限流，共享数据库配额。"""
    import hashlib
    import hmac

    checks = (
        ("email", email_key, limits["email_5min"], 300),
        ("email", email_key, limits["email_hourly"], 3600),
        ("email", email_key, limits["email_daily"], 86400),
        ("ip", client_ip or "unknown", limits["ip"], 3600),
        ("global", "all", limits["global"], 3600),
    )
    retry_after = 0
    for scope, identity, maximum, window_seconds in checks:
        digest = hmac.new(
            key,
            # 版本和窗口长度共同区分配额；旧的每小时三次记录不影响新规则。
            f"rate-limit:v2:{scope}:{window_seconds}:{identity}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        hits, started = _increment_bucket(db, digest, now_epoch, window_seconds)
        if hits > maximum:
            retry_after = max(retry_after, max(1, started + window_seconds - now_epoch))

    # 多窗口作为同一事务：被拒绝时回滚本次增量，避免重复点击耗尽更长窗口。
    if retry_after:
        db.rollback()
        raise ApiProblem(
            "rate_limited",
            "验证码发送次数已达限制，请稍后重试。",
            429,
            {"retry_after_seconds": retry_after},
            retry_after=retry_after,
        )
    db.commit()
