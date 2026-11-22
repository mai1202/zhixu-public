"""持久计划保存跨运行写工具回执，重试时幂等返回原结果。"""
from alembic import op
import sqlalchemy as sa

revision = '0024_plan_tool_receipts'
down_revision = '0023_default_provider'
branch_labels = depends_on = None


def upgrade():
    op.add_column('agent_execution_plans', sa.Column('tool_receipts_json', sa.JSON()))


def downgrade():
    op.drop_column('agent_execution_plans', 'tool_receipts_json')
