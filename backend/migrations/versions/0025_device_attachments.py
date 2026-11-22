"""设备附件原文件、解析状态、通知转化与独立幂等收据。"""
from alembic import op
import sqlalchemy as sa

revision = '0025_device_attachments'
down_revision = '0024_plan_tool_receipts'
branch_labels = depends_on = None


def upgrade():
    op.add_column('sync_items', sa.Column('chat_id', sa.String(256)))
    op.create_table('device_attachments',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('device_id', sa.String(36), sa.ForeignKey('devices.id', ondelete='CASCADE'), nullable=False),
        sa.Column('parent_source_id', sa.String(36), sa.ForeignKey('source_documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('attachment_key', sa.String(64), nullable=False),
        sa.Column('filename', sa.String(240), nullable=False), sa.Column('kind', sa.String(12), nullable=False),
        sa.Column('content_hash', sa.String(64)), sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('material_id', sa.String(36), sa.ForeignKey('agent_attachments.id', ondelete='SET NULL')),
        sa.Column('job_id', sa.String(36), sa.ForeignKey('jobs.id', ondelete='SET NULL')),
        sa.Column('source_document_id', sa.String(36), sa.ForeignKey('source_documents.id', ondelete='SET NULL')),
        sa.Column('status', sa.String(24), nullable=False), sa.Column('error_code', sa.String(80)),
        sa.Column('auto_notice', sa.Boolean(), nullable=False),
        sa.Column('created_at_epoch', sa.Integer(), nullable=False), sa.Column('updated_at_epoch', sa.Integer(), nullable=False),
        sa.UniqueConstraint('device_id', 'attachment_key', name='uq_device_attachment_key'))
    op.create_index('ix_device_attachments_owner_created', 'device_attachments', ['user_id', 'created_at_epoch'])


def downgrade():
    op.drop_index('ix_device_attachments_owner_created', table_name='device_attachments')
    op.drop_table('device_attachments')
    op.drop_column('sync_items', 'chat_id')
