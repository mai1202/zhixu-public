"""助手思考强度与跨对话长期记忆。"""

from alembic import op
import sqlalchemy as sa

revision = "0013_agent_thinking_memory"
down_revision = "0012_pi_events"
branch_labels = depends_on = None


def upgrade():
    # 旧账户默认使用中等思考；实际支持的档位由 Pi 按模型能力裁剪。
    with op.batch_alter_table("agent_preferences") as batch_op:
        batch_op.add_column(
            sa.Column("thinking_level", sa.String(16), nullable=False, server_default="medium")
        )
    op.create_table(
        "agent_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_conversation_id",
            sa.String(36),
            sa.ForeignKey("agent_conversations.id", ondelete="SET NULL"),
        ),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("content", sa.String(1000), nullable=False),
        sa.Column("normalized_content", sa.String(1000), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "normalized_content",
            name="uq_agent_memories_owner_content",
        ),
    )
    op.create_index(
        "ix_agent_memories_owner_updated",
        "agent_memories",
        ["user_id", "updated_at_epoch"],
    )


def downgrade():
    # 删除新增的长期记忆前保留用户会话原文；这里只回退本迁移新增的结构。
    op.drop_index("ix_agent_memories_owner_updated", table_name="agent_memories")
    op.drop_table("agent_memories")
    with op.batch_alter_table("agent_preferences") as batch_op:
        batch_op.drop_column("thinking_level")
