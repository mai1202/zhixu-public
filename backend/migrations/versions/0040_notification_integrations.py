"""增加可撤销 webhook 和公开 RSS/Atom 通知源。"""
from alembic import op
import sqlalchemy as sa

revision = "0040_notification_integrations"
down_revision = "0039_journey_collaboration"
branch_labels = depends_on = None


def upgrade():
    """只增加接入配置，不自动订阅、抓取或公开已有资料。"""
    op.create_table("webhook_credentials",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False))
    op.create_table("notification_feeds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False), sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False), sa.Column("next_poll_epoch", sa.Integer(), nullable=False),
        sa.Column("last_success_epoch", sa.Integer()), sa.Column("error_code", sa.String(80)),
        sa.UniqueConstraint("user_id", "url", name="uq_notification_feed_url"))
    op.create_index("ix_notification_feeds_next_poll_epoch", "notification_feeds", ["next_poll_epoch"])


def downgrade():
    """移除订阅配置，已收到的用户通知仍然保留。"""
    op.drop_table("notification_feeds")
    op.drop_table("webhook_credentials")
