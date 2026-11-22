"""通知邮箱整理和固定主助手的定时唤醒计划。"""

from alembic import op
import sqlalchemy as sa

revision = '0016_assistant_workflow'
down_revision = '0015_agent_insights'
branch_labels = depends_on = None


def upgrade():
    """只增表，原通知、候选、消息与附件原样保留。"""
    op.create_table('notification_organizations',
        sa.Column('source_id', sa.String(36), sa.ForeignKey('source_documents.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(100)), sa.Column('summary', sa.Text()), sa.Column('advice', sa.Text()),
        sa.Column('mailbox', sa.String(16), nullable=False), sa.Column('read_at_epoch', sa.Integer()),
        sa.Column('version', sa.Integer(), nullable=False), sa.Column('updated_at_epoch', sa.Integer(), nullable=False))
    op.create_index('ix_notification_organizations_user_id', 'notification_organizations', ['user_id'])
    op.create_table('agent_scheduled_tasks',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(150), nullable=False), sa.Column('message', sa.Text(), nullable=False),
        sa.Column('mode', sa.String(16), nullable=False), sa.Column('next_run_at_epoch', sa.Integer(), nullable=False),
        sa.Column('interval_seconds', sa.Integer()), sa.Column('attachment_ids', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False), sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('last_run_id', sa.String(36)), sa.Column('created_at_epoch', sa.Integer(), nullable=False))
    op.create_index('ix_agent_scheduled_tasks_user_id', 'agent_scheduled_tasks', ['user_id'])
    op.create_index('ix_agent_schedules_due', 'agent_scheduled_tasks', ['status', 'next_run_at_epoch'])


def downgrade():
    """仅回退本次新增的数据结构。"""
    op.drop_index('ix_agent_schedules_due', table_name='agent_scheduled_tasks')
    op.drop_index('ix_agent_scheduled_tasks_user_id', table_name='agent_scheduled_tasks')
    op.drop_table('agent_scheduled_tasks')
    op.drop_index('ix_notification_organizations_user_id', table_name='notification_organizations')
    op.drop_table('notification_organizations')
