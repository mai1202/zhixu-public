"""为网页登录增加长期会话和可撤销的设备信息，历史过期会话不复活。"""
from alembic import op
import sqlalchemy as sa

revision = '0032_web_session_devices'
down_revision = '0028_notification_groups'
branch_labels = depends_on = None


def upgrade():
    """新增字段使用安全默认值，历史有效会话在认证后自动升级。"""
    op.add_column('web_sessions', sa.Column('persistent', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('web_sessions', sa.Column('user_agent', sa.String(512), nullable=True))
    op.add_column('web_sessions', sa.Column('last_seen_at_epoch', sa.Integer(), nullable=True))


def downgrade():
    """回退设备信息，不修改已有会话令牌及撤销记录。"""
    with op.batch_alter_table('web_sessions') as batch:
        batch.drop_column('last_seen_at_epoch')
        batch.drop_column('user_agent')
        batch.drop_column('persistent')
