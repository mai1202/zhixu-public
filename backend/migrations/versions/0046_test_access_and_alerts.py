"""受限密码测试账号与去重运维告警；不在迁移中创建默认密码。"""
from alembic import op
import sqlalchemy as sa
revision = '0046_test_access_and_alerts'
down_revision = '0045_notification_suggestions'
branch_labels = depends_on = None


def upgrade():
    op.create_table('test_account_access',
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('password_hash', sa.String(512), nullable=False), sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('daily_budget_micros', sa.BigInteger(), nullable=False), sa.Column('created_at_epoch', sa.Integer(), nullable=False))
    op.create_table('operations_alerts', sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('incident_key', sa.String(150), nullable=False), sa.Column('status', sa.String(20), nullable=False),
        sa.Column('created_at_epoch', sa.Integer(), nullable=False), sa.Column('sent_at_epoch', sa.Integer()),
        sa.Column('error_code', sa.String(80)), sa.UniqueConstraint('incident_key', name='uq_operations_alert_incident'))


def downgrade():
    op.drop_table('operations_alerts')
    op.drop_table('test_account_access')
