"""增加持久上下文摘要、私有对话附件和主助手设置。"""

from alembic import op
import sqlalchemy as sa

revision = "0011_agent_memory"
down_revision = "0010_source_submission_hash"
branch_labels = depends_on = None


def upgrade():
    # 原始历史不修改，摘要仅作为长对话的额外索引。
    op.add_column("agent_conversations", sa.Column("memory_json", sa.JSON(), nullable=True))
    op.create_table("agent_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(240), nullable=False),
        sa.Column("storage_key", sa.String(100), nullable=False, unique=True),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("ocr_artifact_id", sa.String(36), sa.ForeignKey("ocr_artifacts.id"), nullable=True),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False))
    op.create_index("ix_agent_attachments_user_id", "agent_attachments", ["user_id"])
    op.create_table("agent_preferences",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("provider_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("automatic_notifications", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context_budget", sa.Integer(), nullable=False, server_default="64000"),
        sa.Column("main_conversation_id", sa.String(36), sa.ForeignKey("agent_conversations.id", ondelete="SET NULL"), nullable=True))


def downgrade():
    # 降级不触碰原始对话记录，附件文件由数据备份一并保留。
    op.drop_table("agent_preferences")
    op.drop_table("agent_attachments")
    op.drop_column("agent_conversations", "memory_json")
