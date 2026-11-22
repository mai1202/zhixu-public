from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class User(Base):
    """已验证邮箱的账户；新账户默认不是管理员。"""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email_key", name="uq_users_email_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    primary_email: Mapped[str] = mapped_column(String(254), nullable=False)
    email_key: Mapped[str] = mapped_column(String(254), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # 单用户递增序号为日报变更快照提供无遗漏游标。
    change_sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    verified_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class VerifiedAddress(Base):
    """账户可以使用的已验证收件地址，主地址始终留在用户记录中。"""

    __tablename__ = "verified_addresses"
    __table_args__ = (
        UniqueConstraint("email_key", name="uq_verified_addresses_email_key"),
        UniqueConstraint("user_id", "email_key", name="uq_verified_addresses_owner_email"),
        Index("ix_verified_addresses_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    email_key: Mapped[str] = mapped_column(String(254), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    verified_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class EmailChallenge(Base):
    """一次性验证码摘要；原始验证码只交给 MailTransport。"""

    __tablename__ = "email_challenges"
    __table_args__ = (
        Index("ix_email_challenges_email_issued", "email_key", "issued_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    email_key: Mapped[str] = mapped_column(String(254), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_at_epoch: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # 验证成功后才将此次主动勾选的版本关联到账户。
    policy_version: Mapped[str | None] = mapped_column(String(40))


class WebSession(Base):
    """服务端会话仅保存带密钥摘要，浏览器只持有 HttpOnly 不透明令牌。"""

    __tablename__ = "web_sessions"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_web_sessions_token_digest"),
        Index("ix_web_sessions_user_expiry", "user_id", "expires_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    revoked_at_epoch: Mapped[int | None] = mapped_column(Integer)

    # 老会话保留原期限；新登录显式启用长期会话，撤销状态始终优先。
    persistent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    last_seen_at_epoch: Mapped[int | None] = mapped_column(Integer)


class RateLimitBucket(Base):
    """持久化的限流窗口，键值经过 HMAC，不保存原始邮箱或 IP。"""

    __tablename__ = "rate_limit_buckets"

    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)
