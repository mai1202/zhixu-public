"""为用户对话和可恢复的 Agent 运行增加 owner 隔离记录。"""

from alembic import op
import sqlalchemy as sa

revision = "0008_agent_conversations"
down_revision = "0007_devices_and_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("archived_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_conversations_owner_updated",
        "agent_conversations",
        ["user_id", "updated_at_epoch"],
    )
    op.create_table(
        "agent_turns",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_agent_turns_conversation_sequence"),
        sa.UniqueConstraint("conversation_id", "run_id", "role", name="uq_agent_turns_run_role"),
    )
    op.create_index(
        "ix_agent_turns_owner_conversation",
        "agent_turns",
        ["user_id", "conversation_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_turns_owner_conversation", table_name="agent_turns")
    op.drop_table("agent_turns")
    op.drop_index("ix_agent_conversations_owner_updated", table_name="agent_conversations")
    op.drop_table("agent_conversations")
