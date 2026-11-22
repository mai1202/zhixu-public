"""建立受限设备、可靠同步批次，并为外部来源增加幂等键。"""

from alembic import op
import sqlalchemy as sa

revision = "0007_devices_and_sync"
down_revision = "0006_ocr_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_documents", sa.Column("external_key", sa.String(200), nullable=True))
    op.create_index(
        "uq_source_documents_owner_external_key",
        "source_documents",
        ["user_id", "source_type", "external_key"],
        unique=True,
        sqlite_where=sa.text("external_key IS NOT NULL"),
        postgresql_where=sa.text("external_key IS NOT NULL"),
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat_epoch", sa.Integer(), nullable=True),
        sa.Column("last_collect_epoch", sa.Integer(), nullable=True),
        sa.Column("last_upload_epoch", sa.Integer(), nullable=True),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("confirmed_at_epoch", sa.Integer(), nullable=True),
        sa.Column("revoked_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "fingerprint", name="uq_devices_owner_fingerprint"),
    )
    op.create_index("ix_devices_owner_status", "devices", ["user_id", "status"])
    op.create_table(
        "device_pairings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("code_digest", sa.String(64), nullable=False),
        sa.Column("expires_at_epoch", sa.Integer(), nullable=False),
        sa.Column("claimed_device_id", sa.String(36), nullable=True),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["claimed_device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_digest", name="uq_device_pairings_code_digest"),
    )
    op.create_index("ix_device_pairings_owner_expiry", "device_pairings", ["user_id", "expires_at_epoch"])
    op.create_table(
        "device_credentials",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("access_digest", sa.String(64), nullable=False),
        sa.Column("refresh_digest", sa.String(64), nullable=False),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("access_expires_at_epoch", sa.Integer(), nullable=False),
        sa.Column("refresh_expires_at_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("refreshed_at_epoch", sa.Integer(), nullable=True),
        sa.Column("revoked_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("access_digest", name="uq_device_credentials_access"),
        sa.UniqueConstraint("refresh_digest", name="uq_device_credentials_refresh"),
    )
    op.create_index("ix_device_credentials_device", "device_credentials", ["device_id"])
    op.create_table(
        "sync_batches",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("batch_key", sa.String(36), nullable=False),
        sa.Column("window_start_epoch", sa.Integer(), nullable=False),
        sa.Column("window_end_epoch", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("failed_pages_json", sa.JSON(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", "batch_key", name="uq_sync_batches_device_key"),
    )
    op.create_index("ix_sync_batches_owner_created", "sync_batches", ["user_id", "created_at_epoch"])
    op.create_table(
        "sync_items",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(200), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("attachments_json", sa.JSON(), nullable=False),
        sa.Column("source_document_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["sync_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", "batch_id", "source_key", name="uq_sync_items_device_batch_source"),
        sa.UniqueConstraint("device_id", "batch_id", "ordinal", name="uq_sync_items_device_batch_ordinal"),
    )
    op.create_index("ix_sync_items_owner_batch", "sync_items", ["user_id", "batch_id"])


def downgrade() -> None:
    op.drop_index("ix_sync_items_owner_batch", table_name="sync_items")
    op.drop_table("sync_items")
    op.drop_index("ix_sync_batches_owner_created", table_name="sync_batches")
    op.drop_table("sync_batches")
    op.drop_index("ix_device_credentials_device", table_name="device_credentials")
    op.drop_table("device_credentials")
    op.drop_index("ix_device_pairings_owner_expiry", table_name="device_pairings")
    op.drop_table("device_pairings")
    op.drop_index("ix_devices_owner_status", table_name="devices")
    op.drop_table("devices")
    op.drop_index("uq_source_documents_owner_external_key", table_name="source_documents")
    op.drop_column("source_documents", "external_key")
