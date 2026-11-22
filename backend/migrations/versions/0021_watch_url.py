"""官网关注地址。"""
from alembic import op
import sqlalchemy as sa

revision = '0021_watch_url'
down_revision = '0020_agent_execution_plans'
branch_labels = depends_on = None


def upgrade():
    op.add_column('agent_scheduled_tasks', sa.Column('watch_url', sa.String(2000)))


def downgrade():
    op.drop_column('agent_scheduled_tasks', 'watch_url')
