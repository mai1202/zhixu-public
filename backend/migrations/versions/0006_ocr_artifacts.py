"""保存私有 OCR 图片、执行状态、原始结果和校正版本。"""

from alembic import op
import sqlalchemy as sa

revision = "0006_ocr_artifacts"
down_revision = "0005_llm_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ocr_artifacts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=True),
        sa.Column("source_document_id", sa.String(36), nullable=True),
        sa.Column("storage_key", sa.String(100), nullable=False),
        sa.Column("media_type", sa.String(32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("image_hash", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("orientation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("corrected_items_json", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(80), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_ocr_artifacts_storage_key"),
    )
    op.create_index("ix_ocr_artifacts_owner_created", "ocr_artifacts", ["user_id", "created_at_epoch"])
    op.create_index("ix_ocr_artifacts_owner_source", "ocr_artifacts", ["user_id", "source_document_id"])


def downgrade() -> None:
    op.drop_index("ix_ocr_artifacts_owner_source", table_name="ocr_artifacts")
    op.drop_index("ix_ocr_artifacts_owner_created", table_name="ocr_artifacts")
    op.drop_table("ocr_artifacts")
