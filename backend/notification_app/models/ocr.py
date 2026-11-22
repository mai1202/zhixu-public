from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OcrArtifact(Base):
    """用户私有截图及其不可覆盖的原始 OCR 结果。"""

    __tablename__ = "ocr_artifacts"
    __table_args__ = (
        Index("ix_ocr_artifacts_owner_created", "user_id", "created_at_epoch"),
        Index("ix_ocr_artifacts_owner_source", "user_id", "source_document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    storage_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    image_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    orientation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    corrected_items_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
