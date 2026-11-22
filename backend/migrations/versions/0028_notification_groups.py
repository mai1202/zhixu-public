"""通知跨消息归组；旧原文、摘要及邮箱状态保持可恢复。"""
from alembic import op
import sqlalchemy as sa

revision = '0028_notification_groups'
down_revision = '0027_student_journeys'
branch_labels = depends_on = None


def upgrade():
    """SQLite 用批量迁移建立外键，旧记录默认未归组、未锁定。"""
    with op.batch_alter_table('notification_organizations') as batch:
        batch.add_column(sa.Column('group_source_id', sa.String(36), nullable=True))
        batch.add_column(sa.Column('group_reason', sa.Text(), nullable=True))
        batch.add_column(sa.Column('grouping_locked', sa.Integer(), server_default='0', nullable=False))
        batch.create_foreign_key('fk_notification_group_source', 'source_documents', ['group_source_id'], ['id'], ondelete='SET NULL')
        batch.create_index('ix_notification_organizations_group_source_id', ['group_source_id'])


def downgrade():
    """只移除阅读归组，不移除原始消息与业务对象。"""
    with op.batch_alter_table('notification_organizations') as batch:
        batch.drop_index('ix_notification_organizations_group_source_id')
        batch.drop_constraint('fk_notification_group_source', type_='foreignkey')
        batch.drop_column('grouping_locked')
        batch.drop_column('group_reason')
        batch.drop_column('group_source_id')
