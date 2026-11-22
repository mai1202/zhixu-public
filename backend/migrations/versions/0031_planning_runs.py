"""多事务规划快照与默认关闭的移动许可。"""
from alembic import op
import sqlalchemy as sa

revision = "0031_planning_runs"
down_revision = "0030_clarification_requests"
branch_labels = depends_on = None


def upgrade():
    """历史日程不参与自动重排，用户明确开启后才会移动。"""
    with op.batch_alter_table("schedule_entries") as batch:
        batch.add_column(sa.Column("planning_policy_json", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table("user_planning_states",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("automatic", sa.Boolean(), nullable=False), sa.Column("availability_json", sa.JSON(), nullable=False),
        sa.Column("journey_ids_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("changed_at_epoch", sa.Integer(), nullable=False))
    op.create_table("planning_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False), sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("change_set_id", sa.String(36), sa.ForeignKey("change_sets.id", ondelete="SET NULL")),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False), sa.Column("applied_at_epoch", sa.Integer()))
    op.create_index("ix_planning_runs_owner_created", "planning_runs", ["user_id", "created_at_epoch"])


def downgrade():
    """业务日程投影保留，历史审计不随功能开关删除。"""
    op.drop_table("planning_runs")
    op.drop_table("user_planning_states")
    with op.batch_alter_table("schedule_entries") as batch:
        batch.drop_column("planning_policy_json")
