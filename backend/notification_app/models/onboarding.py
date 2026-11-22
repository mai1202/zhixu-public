"""首次使用引导的可恢复进度与一次性设置提醒。"""
from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class UserOnboarding(Base):
    __tablename__ = 'user_onboarding'
    # Worker 每分钟查找未排队且到期的提醒，避免账户增多后反复扫描全部历史进度。
    __table_args__ = (Index('ix_user_onboarding_reminder_due', 'reminder_enabled', 'reminder_outbox_id', 'reminder_due_at_epoch'),)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='active')
    skipped_sections_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reminder_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reminder_due_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    reminder_outbox_id: Mapped[str | None] = mapped_column(ForeignKey('mail_outbox.id', ondelete='SET NULL'))
    snoozed_until_epoch: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
