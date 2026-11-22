"""复杂请求的持久执行计划与步骤回执。"""
from alembic import op
import sqlalchemy as sa

revision = '0020_agent_execution_plans'
down_revision = '0019_knowledge_metadata'
branch_labels = depends_on = None


def upgrade():
    op.create_table('agent_execution_plans',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('conversation_id', sa.String(36)),
        sa.Column('run_id', sa.String(36)),
        sa.Column('goal', sa.Text(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='active'),
        sa.Column('steps_json', sa.JSON(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at_epoch', sa.Integer(), nullable=False),
        sa.Column('updated_at_epoch', sa.Integer(), nullable=False))
    op.create_index('ix_agent_plans_owner_status', 'agent_execution_plans',
                    ['user_id', 'status', 'updated_at_epoch'])
    op.create_index('ix_agent_execution_plans_run_id', 'agent_execution_plans', ['run_id'])


def downgrade():
    op.drop_index('ix_agent_execution_plans_run_id', table_name='agent_execution_plans')
    op.drop_index('ix_agent_plans_owner_status', table_name='agent_execution_plans')
    op.drop_table('agent_execution_plans')
