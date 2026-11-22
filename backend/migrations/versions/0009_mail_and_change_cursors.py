"""增加用户变更序号、日报偏好和持久邮件 outbox。"""

from alembic import op
import sqlalchemy as sa

revision = "0009_mail_and_change_cursors"
down_revision = "0008_agent_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("change_sequence", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("change_sets", sa.Column("change_seq", sa.Integer(), nullable=True))
    op.create_index(
        "ix_change_sets_owner_change_seq",
        "change_sets",
        ["user_id", "change_seq"],
    )
    op.add_column("jobs", sa.Column("priority", sa.Integer(), server_default="50", nullable=False))

    op.create_table(
        "digest_preferences",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("send_time", sa.String(5), nullable=False),
        sa.Column("recipient_address_id", sa.String(36), nullable=True),
        sa.Column("include_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("next_send_epoch", sa.Integer(), nullable=True),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["recipient_address_id"], ["verified_addresses.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_digest_preferences_user"),
    )
    op.create_table(
        "digest_cursors",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("last_change_seq", sa.Integer(), nullable=False),
        sa.Column("last_sent_local_date", sa.String(10), nullable=True),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "mail_outbox",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("template_name", sa.String(80), nullable=False),
        sa.Column("recipient", sa.String(254), nullable=False),
        sa.Column("recipient_address_id", sa.String(36), nullable=False),
        sa.Column("recipient_verified_at_epoch", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("dedupe_key", sa.String(250), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=True),
        sa.Column("scheduled_at_epoch", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("accepted_at_epoch", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recipient_address_id"], ["verified_addresses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_mail_outbox_dedupe_key"),
    )
    op.create_index("ix_mail_outbox_owner_created", "mail_outbox", ["user_id", "created_at_epoch"])
    op.create_index("ix_mail_outbox_status_priority", "mail_outbox", ["status", "priority", "scheduled_at_epoch"])
    op.create_table(
        "mail_credentials",
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("provider"),
    )


def downgrade() -> None:
    op.drop_table("mail_credentials")
    op.drop_index("ix_mail_outbox_status_priority", table_name="mail_outbox")
    op.drop_index("ix_mail_outbox_owner_created", table_name="mail_outbox")
    op.drop_table("mail_outbox")
    op.drop_table("digest_cursors")
    op.drop_table("digest_preferences")
    op.drop_column("jobs", "priority")
    op.drop_index("ix_change_sets_owner_change_seq", table_name="change_sets")
    op.drop_column("change_sets", "change_seq")
    op.drop_column("users", "change_sequence")
