"""持续关注快照与可选知识向量索引。"""
from alembic import op
import sqlalchemy as sa

revision = '0018_watch_snapshot_and_chunks'
down_revision = '0017_knowledge_and_mail'
branch_labels = depends_on = None


def upgrade():
    """只增加关注状态和可重建索引，不复制或搬移原业务正文。"""
    op.add_column('agent_scheduled_tasks', sa.Column('last_watch_snapshot', sa.JSON()))
    op.add_column('agent_scheduled_tasks', sa.Column('last_watch_delta_json', sa.JSON()))
    op.add_column('agent_scheduled_tasks', sa.Column('last_success_epoch', sa.Integer()))
    op.add_column('agent_scheduled_tasks', sa.Column('last_error_code', sa.String(80)))
    op.add_column('agent_scheduled_tasks', sa.Column('last_notified_epoch', sa.Integer()))
    op.add_column('agent_scheduled_tasks', sa.Column('run_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('agent_scheduled_tasks', sa.Column('stop_at_epoch', sa.Integer()))
    op.create_table('knowledge_chunks',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_kind', sa.String(24), nullable=False),
        sa.Column('source_id', sa.String(64), nullable=False),
        sa.Column('scope', sa.String(16), nullable=False, server_default='private'),
        sa.Column('source_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('chunk_index', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('embedding_json', sa.JSON()),
        sa.Column('embedding_model', sa.String(120)),
        sa.Column('embedding_dimensions', sa.Integer()),
        sa.Column('chunker_version', sa.String(24), nullable=False, server_default='v1'),
        sa.Column('updated_at_epoch', sa.Integer(), nullable=False))
    op.create_index('ix_knowledge_chunks_owner_source', 'knowledge_chunks',
                    ['user_id', 'source_kind', 'source_id'])
    op.create_index('ix_knowledge_chunks_scope_updated', 'knowledge_chunks',
                    ['scope', 'updated_at_epoch'])
    op.create_index('ix_knowledge_chunks_owner_updated', 'knowledge_chunks',
                    ['user_id', 'updated_at_epoch'])


def downgrade():
    """索引和关注快照均可重建；回退不触碰原通知、附件和文档正文。"""
    op.drop_index('ix_knowledge_chunks_scope_updated', table_name='knowledge_chunks')
    op.drop_index('ix_knowledge_chunks_owner_updated', table_name='knowledge_chunks')
    op.drop_index('ix_knowledge_chunks_owner_source', table_name='knowledge_chunks')
    op.drop_table('knowledge_chunks')
    for name in ('stop_at_epoch', 'run_count', 'last_notified_epoch', 'last_error_code',
                 'last_success_epoch', 'last_watch_delta_json', 'last_watch_snapshot'):
        op.drop_column('agent_scheduled_tasks', name)
