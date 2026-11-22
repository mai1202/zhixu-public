"""增加工具执行前审查回执。"""
from alembic import op
import sqlalchemy as sa
revision = "0041_agent_tool_review"
down_revision = "0040_notification_integrations"
branch_labels = depends_on = None


def upgrade():
    """审查日志随账户和任务生命周期删除。"""
    op.create_table("agent_tool_reviews", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_call_id", sa.String(200), nullable=False), sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False), sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False), sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_agent_tool_review_call"))


def downgrade():
    """只移除新增的审查记录。"""
    op.drop_table("agent_tool_reviews")
