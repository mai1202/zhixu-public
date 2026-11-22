from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Device(Base):
    """用户明确配对的受限本机客户端。"""

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("user_id", "fingerprint", name="uq_devices_owner_fingerprint"),
        Index("ix_devices_owner_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_heartbeat_epoch: Mapped[int | None] = mapped_column(Integer)
    last_collect_epoch: Mapped[int | None] = mapped_column(Integer)
    last_upload_epoch: Mapped[int | None] = mapped_column(Integer)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_at_epoch: Mapped[int | None] = mapped_column(Integer)
    revoked_at_epoch: Mapped[int | None] = mapped_column(Integer)


class DevicePairing(Base):
    """短时一次性配对码，仅存带密钥摘要。"""

    __tablename__ = "device_pairings"
    __table_args__ = (Index("ix_device_pairings_owner_expiry", "user_id", "expires_at_epoch"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    claimed_device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class DeviceCredential(Base):
    """设备访问与刷新凭证的摘要；刷新时旧摘要立即失效。"""

    __tablename__ = "device_credentials"
    __table_args__ = (
        UniqueConstraint("access_digest", name="uq_device_credentials_access"),
        UniqueConstraint("refresh_digest", name="uq_device_credentials_refresh"),
        Index("ix_device_credentials_device", "device_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    access_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    refresh_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    access_expires_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    refresh_expires_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    refreshed_at_epoch: Mapped[int | None] = mapped_column(Integer)
    revoked_at_epoch: Mapped[int | None] = mapped_column(Integer)


class SyncBatch(Base):
    """一次固定时间窗口的上传记录，重试由 device_id + batch_key 幂等。"""

    __tablename__ = "sync_batches"
    __table_args__ = (
        UniqueConstraint("device_id", "batch_key", name="uq_sync_batches_device_key"),
        Index("ix_sync_batches_owner_created", "user_id", "created_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    batch_key: Mapped[str] = mapped_column(String(36), nullable=False)
    window_start_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    window_end_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    complete: Mapped[bool] = mapped_column(nullable=False)
    failed_pages_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class SyncItem(Base):
    """单条来源的接收确认，不保存重复的聊天正文。"""

    __tablename__ = "sync_items"
    __table_args__ = (
        UniqueConstraint("device_id", "batch_id", "source_key", name="uq_sync_items_device_batch_source"),
        UniqueConstraint("device_id", "batch_id", "ordinal", name="uq_sync_items_device_batch_ordinal"),
        Index("ix_sync_items_owner_batch", "user_id", "batch_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("sync_batches.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attachments_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_document_id: Mapped[str | None] = mapped_column(ForeignKey("source_documents.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class DeviceAttachment(Base):
    """设备附件独立收据，正文 ACK 不会掩盖附件上传或解析失败。"""

    __tablename__ = 'device_attachments'
    __table_args__ = (
        UniqueConstraint('device_id', 'attachment_key', name='uq_device_attachment_key'),
        Index('ix_device_attachments_owner_created', 'user_id', 'created_at_epoch'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    device_id: Mapped[str] = mapped_column(ForeignKey('devices.id', ondelete='CASCADE'), nullable=False)
    parent_source_id: Mapped[str] = mapped_column(ForeignKey('source_documents.id', ondelete='CASCADE'), nullable=False)
    attachment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(240), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    material_id: Mapped[str | None] = mapped_column(ForeignKey('agent_attachments.id', ondelete='SET NULL'))
    job_id: Mapped[str | None] = mapped_column(ForeignKey('jobs.id', ondelete='SET NULL'))
    source_document_id: Mapped[str | None] = mapped_column(ForeignKey('source_documents.id', ondelete='SET NULL'))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    auto_notice: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
