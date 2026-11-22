"""独立的字段主张，不改变 NormalizedBundle 1.0。"""
from alembic import op
import sqlalchemy as sa

revision = "0029_requirement_claims"
down_revision = "0028_notification_groups"
branch_labels = depends_on = None


def upgrade():
    """增量列保留旧业务投影，历史记录不自动生成未经核验的证据。"""
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("identity_json", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("field_policies_json", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table("requirement_claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("change_set_id", sa.String(36), sa.ForeignKey("change_sets.id", ondelete="SET NULL")),
        sa.Column("field_key", sa.String(120), nullable=False), sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False), sa.Column("relation", sa.String(24), nullable=False),
        sa.Column("supersedes_id", sa.String(36)), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False), sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.UniqueConstraint("user_id", "fingerprint", name="uq_claims_owner_fingerprint"))
    op.create_index("ix_claims_owner_event", "requirement_claims", ["user_id", "event_id"])


def downgrade():
    """仅在维护者明确回滚迁移时移除新历史表。"""
    op.drop_table("requirement_claims")
    with op.batch_alter_table("events") as batch:
        batch.drop_column("field_policies_json")
        batch.drop_column("identity_json")
