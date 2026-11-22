"""公共/私人知识库、收信归属与持续关注状态。"""
from alembic import op
import sqlalchemy as sa

revision = '0017_knowledge_and_mail'
down_revision = '0016_assistant_workflow'
branch_labels = depends_on = None


def upgrade():
    """只增加资料及调度字段，原通知、对话、附件不搬迁、不复制。"""
    op.create_table('knowledge_documents',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('user_id',sa.String(36),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False),
        sa.Column('scope',sa.String(16),nullable=False),sa.Column('title',sa.String(500),nullable=False),
        sa.Column('content',sa.Text(),nullable=False),sa.Column('source_url',sa.String(2000)),
        sa.Column('content_hash',sa.String(64),nullable=False),sa.Column('status',sa.String(16),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),sa.Column('created_at_epoch',sa.Integer(),nullable=False),
        sa.Column('updated_at_epoch',sa.Integer(),nullable=False))
    op.create_index('ix_knowledge_scope_owner_updated','knowledge_documents',['scope','user_id','updated_at_epoch'])
    op.create_table('mail_ingress_accounts',
        sa.Column('user_id',sa.String(36),sa.ForeignKey('users.id',ondelete='CASCADE'),primary_key=True),
        sa.Column('token_digest',sa.String(64),nullable=False,unique=True),
        sa.Column('token_ciphertext',sa.Text(),nullable=False),sa.Column('updated_at_epoch',sa.Integer(),nullable=False))
    op.create_table('mail_ingress_receipts',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('user_id',sa.String(36),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False),
        sa.Column('content_hash',sa.String(64),nullable=False),sa.Column('status',sa.String(16),nullable=False),
        sa.Column('result_json',sa.JSON(),nullable=False),sa.Column('lease_until_epoch',sa.Integer(),nullable=False),
        sa.Column('updated_at_epoch',sa.Integer(),nullable=False))
    op.create_index('ix_mail_ingress_receipts_user_id','mail_ingress_receipts',['user_id'])
    op.create_table('mail_ingress_cursors',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('uid_validity',sa.String(80),nullable=False),sa.Column('last_uid',sa.Integer(),nullable=False),
        sa.Column('last_poll_epoch',sa.Integer(),nullable=False),sa.Column('error_code',sa.String(80)))
    op.add_column('agent_scheduled_tasks',sa.Column('watch_query',sa.String(300)))
    op.add_column('agent_scheduled_tasks',sa.Column('watch_scope',sa.String(16),nullable=False,server_default='all'))
    op.add_column('agent_scheduled_tasks',sa.Column('last_watch_fingerprint',sa.String(64)))
    op.add_column('agent_scheduled_tasks',sa.Column('last_watch_checked_epoch',sa.Integer()))


def downgrade():
    """回退仅删除本版本新增的表和字段。"""
    for name in ('last_watch_checked_epoch','last_watch_fingerprint','watch_scope','watch_query'):
        op.drop_column('agent_scheduled_tasks',name)
    op.drop_table('mail_ingress_cursors')
    op.drop_index('ix_mail_ingress_receipts_user_id',table_name='mail_ingress_receipts')
    op.drop_table('mail_ingress_receipts')
    op.drop_table('mail_ingress_accounts')
    op.drop_index('ix_knowledge_scope_owner_updated',table_name='knowledge_documents')
    op.drop_table('knowledge_documents')
