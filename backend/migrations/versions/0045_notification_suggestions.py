"""新增首页低置信度建议，不恢复已退役的旧核对请求表。"""
from alembic import op
import sqlalchemy as sa
revision = "0045_notification_suggestions"
down_revision = "0044_mail_commands"
branch_labels = depends_on = None


def upgrade():
    """建议与原始来源、操作版本绑定，避免确认过期解析结果。"""
    op.create_table("notification_suggestions", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("change_set_id", sa.String(36), sa.ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("change_version", sa.Integer(), nullable=False), sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("bundle_json", sa.JSON(), nullable=False), sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False), sa.UniqueConstraint("user_id", "source_id", name="uq_notification_suggestion_source"))
    op.create_index("ix_notification_suggestions_user_id", "notification_suggestions", ["user_id"])


def downgrade():
    """不触及已确认生成的通知、事务和日程。"""
    op.drop_table("notification_suggestions")
