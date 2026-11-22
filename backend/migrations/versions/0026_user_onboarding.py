"""首次引导、实际配置进度与可关闭的一次性邮件提醒。"""
from alembic import op
import sqlalchemy as sa

revision = '0026_user_onboarding'
down_revision = '0025_device_attachments'
branch_labels = depends_on = None


def upgrade():
    op.create_table('user_onboarding',
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('skipped_sections_json', sa.JSON(), nullable=False),
        sa.Column('reminder_enabled', sa.Boolean(), nullable=False),
        sa.Column('reminder_due_at_epoch', sa.Integer(), nullable=False),
        sa.Column('reminder_outbox_id', sa.String(36), sa.ForeignKey('mail_outbox.id', ondelete='SET NULL')),
        sa.Column('snoozed_until_epoch', sa.Integer()),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at_epoch', sa.Integer(), nullable=False), sa.Column('updated_at_epoch', sa.Integer(), nullable=False))
    op.create_index('ix_user_onboarding_reminder_due', 'user_onboarding', ['reminder_enabled', 'reminder_outbox_id', 'reminder_due_at_epoch'])


def downgrade():
    op.drop_index('ix_user_onboarding_reminder_due', table_name='user_onboarding')
    op.drop_table('user_onboarding')
