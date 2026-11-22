"""低置信度建议独立于正式业务，只有本人确认后才登记事务。"""
from sqlalchemy import ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class NotificationSuggestion(Base):
    __tablename__ = "notification_suggestions"
    __table_args__ = (UniqueConstraint("user_id", "source_id", name="uq_notification_suggestion_source"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id", ondelete="CASCADE"))
    change_set_id: Mapped[str] = mapped_column(ForeignKey("change_sets.id", ondelete="CASCADE"))
    change_version: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64))
    bundle_json: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(1000))
    confidence: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)
    created_at_epoch: Mapped[int] = mapped_column(Integer)
    updated_at_epoch: Mapped[int] = mapped_column(Integer)
