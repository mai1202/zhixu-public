"""增加服务端统一默认模型标记，便于登录后无需配置即可使用。"""
from alembic import op
import sqlalchemy as sa

revision = '0023_default_provider'
down_revision = '0022_watch_pending_delivery'
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        'provider_profiles',
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        'ix_provider_profiles_default',
        'provider_profiles',
        ['user_id', 'is_default'],
    )


def downgrade():
    op.drop_index('ix_provider_profiles_default', table_name='provider_profiles')
    op.drop_column('provider_profiles', 'is_default')
