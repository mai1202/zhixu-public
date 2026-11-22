from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DigestPreference(Base):
    """用户明确启用的日报时间、时区、收件地址和内容选项。"""

    __tablename__ = "digest_preferences"
    __table_args__ = (UniqueConstraint("user_id", name="uq_digest_preferences_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    timezone: Mapped[str] = mapped_column(String(100), nullable=False, default="Asia/Shanghai")
    send_time: Mapped[str] = mapped_column(String(5), nullable=False, default="07:30")
    recipient_address_id: Mapped[str | None] = mapped_column(ForeignKey("verified_addresses.id", ondelete="SET NULL"))
    include_json: Mapped[dict[str, bool]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_send_epoch: Mapped[int | None] = mapped_column(Integer)
    # 周报与任务提醒独立开关，避免用户只想收日报却被额外邮件打扰。
    weekly_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    weekly_send_day: Mapped[int] = mapped_column(Integer, nullable=False, default=7, server_default="7")
    weekly_send_time: Mapped[str] = mapped_column(String(5), nullable=False, default="18:00", server_default="18:00")
    next_weekly_send_epoch: Mapped[int | None] = mapped_column(Integer)
    reminder_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    reminder_offsets_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=lambda: [1440, 120])
    notification_flags_json: Mapped[dict[str, bool]] = mapped_column(JSON, nullable=False, default=dict)
    quiet_hours_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    merge_window_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class DigestCursor(Base):
    """最近成功日报已覆盖的用户变更序号。"""

    __tablename__ = "digest_cursors"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_change_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sent_local_date: Mapped[str | None] = mapped_column(String(10))
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class MailOutbox(Base):
    """持久邮件快照及投递状态；正文只在数据库内用于发送和用户回看。"""

    __tablename__ = "mail_outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_mail_outbox_dedupe_key"),
        Index("ix_mail_outbox_owner_created", "user_id", "created_at_epoch"),
        Index("ix_mail_outbox_status_priority", "status", "priority", "scheduled_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    template_name: Mapped[str] = mapped_column(String(80), nullable=False)
    recipient: Mapped[str] = mapped_column(String(254), nullable=False)
    recipient_address_id: Mapped[str] = mapped_column(ForeignKey("verified_addresses.id", ondelete="RESTRICT"), nullable=False)
    recipient_verified_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(250), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    scheduled_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    accepted_at_epoch: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class MailCredential(Base):
    """运营发信账户的 OAuth refresh token 密文，不暴露给普通用户接口。"""

    __tablename__ = "mail_credentials"

    provider: Mapped[str] = mapped_column(String(24), primary_key=True)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
