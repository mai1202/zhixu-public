"""Pi 流式事件、取消标记与业务工具幂等收据。"""

from alembic import op
import sqlalchemy as sa

revision = "0012_pi_events"
down_revision = "0011_agent_memory"
branch_labels = depends_on = None


def upgrade():
    # 会话原始轨迹由 Pi JSONL 保存；数据库保留网页恢复所需的流与执行收据。
    op.create_table("agent_run_events",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True), sa.Column("payload_json", sa.JSON(), nullable=False))
    op.create_table("agent_run_controls",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("cancelled", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("agent_tool_receipts",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tool_call_id", sa.String(200), primary_key=True),
        sa.Column("input_hash", sa.String(64), nullable=False), sa.Column("result_json", sa.JSON(), nullable=False))


def downgrade():
    # 不触碰用户原始会话和附件。
    op.drop_table("agent_tool_receipts")
    op.drop_table("agent_run_controls")
    op.drop_table("agent_run_events")
