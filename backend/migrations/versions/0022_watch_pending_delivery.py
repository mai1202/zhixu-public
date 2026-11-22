"""关注变化在模型运行成功前的待确认状态。"""
from alembic import op
import sqlalchemy as sa

revision = '0022_watch_pending_delivery'
down_revision = '0021_watch_url'
branch_labels = depends_on = None


def upgrade():
    op.add_column('agent_scheduled_tasks', sa.Column('pending_watch_snapshot', sa.JSON()))
    op.add_column('agent_scheduled_tasks', sa.Column('pending_watch_delta_json', sa.JSON()))
    op.add_column('agent_scheduled_tasks', sa.Column('pending_run_id', sa.String(36)))
    op.add_column('agent_scheduled_tasks', sa.Column('pending_attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('agent_scheduled_tasks', sa.Column('pending_since_epoch', sa.Integer()))


def downgrade():
    for name in ('pending_since_epoch', 'pending_attempts', 'pending_run_id',
                 'pending_watch_delta_json', 'pending_watch_snapshot'):
        op.drop_column('agent_scheduled_tasks', name)
