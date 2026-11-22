"""独立接入凭证和订阅源；原始通知仍复用现有个人来源模型。"""
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class WebhookCredential(Base):
    __tablename__ = "webhook_credentials"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at_epoch: Mapped[int] = mapped_column(Integer)


class NotificationFeed(Base):
    __tablename__ = "notification_feeds"
    __table_args__ = (UniqueConstraint("user_id", "url", name="uq_notification_feed_url"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(2000))
    created_at_epoch: Mapped[int] = mapped_column(Integer)
    next_poll_epoch: Mapped[int] = mapped_column(Integer, index=True)
    last_success_epoch: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
