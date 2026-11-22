"""增加来源、通知、事件、待办、修订和变更集。"""

from alembic import op
import sqlalchemy as sa

revision = "0003_business_inbox"
down_revision = "0002_accounts_and_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 所有业务表均带 user_id，列表与详情查询都必须再按 owner 过滤。
    op.create_table(
        "source_documents",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("archived_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_source_documents_owner_created", "source_documents", ["user_id", "created_at_epoch"])
    op.create_table(
        "source_segments",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("source_document_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_document_id", "ordinal", name="uq_source_segments_document_ordinal"),
    )
    op.create_index("ix_source_segments_owner_document", "source_segments", ["user_id", "source_document_id"])
    op.create_table(
        "notices",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("source_document_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("archived_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notices_owner_status_updated", "notices", ["user_id", "status", "updated_at_epoch"])
    op.create_table(
        "events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("notice_id", sa.String(36), nullable=True),
        sa.Column("source_document_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("actor", sa.String(500), nullable=True),
        sa.Column("stage", sa.String(500), nullable=True),
        sa.Column("scope", sa.String(1000), nullable=True),
        sa.Column("deadline_json", sa.JSON(), nullable=True),
        sa.Column("time_range_json", sa.JSON(), nullable=True),
        sa.Column("materials_json", sa.JSON(), nullable=False),
        sa.Column("submission", sa.String(2000), nullable=True),
        sa.Column("eligibility", sa.String(24), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("unknowns_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("archived_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["notice_id"], ["notices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_owner_status_updated", "events", ["user_id", "status", "updated_at_epoch"])
    op.create_index("ix_events_owner_notice", "events", ["user_id", "notice_id"])
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("due_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("completed_at_epoch", sa.Integer(), nullable=True),
        sa.Column("archived_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_owner_status_due", "tasks", ["user_id", "status"])
    op.create_table(
        "revisions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("object_type", sa.String(32), nullable=False),
        sa.Column("object_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("source_document_id", sa.String(36), nullable=True),
        sa.Column("actor_type", sa.String(24), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "object_type", "object_id", "version", name="uq_revisions_object_version"),
    )
    op.create_index("ix_revisions_owner_object", "revisions", ["user_id", "object_type", "object_id"])
    op.create_table(
        "change_sets",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("source_document_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("summary", sa.String(1000), nullable=False),
        sa.Column("bundle_json", sa.JSON(), nullable=True),
        sa.Column("operations_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("confirmed_at_epoch", sa.Integer(), nullable=True),
        sa.Column("undone_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_change_sets_owner_status_created", "change_sets", ["user_id", "status", "created_at_epoch"])


def downgrade() -> None:
    op.drop_index("ix_change_sets_owner_status_created", table_name="change_sets")
    op.drop_table("change_sets")
    op.drop_index("ix_revisions_owner_object", table_name="revisions")
    op.drop_table("revisions")
    op.drop_index("ix_tasks_owner_status_due", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_events_owner_notice", table_name="events")
    op.drop_index("ix_events_owner_status_updated", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_notices_owner_status_updated", table_name="notices")
    op.drop_table("notices")
    op.drop_index("ix_source_segments_owner_document", table_name="source_segments")
    op.drop_table("source_segments")
    op.drop_index("ix_source_documents_owner_created", table_name="source_documents")
    op.drop_table("source_documents")
