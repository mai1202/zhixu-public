"""平台密钥逐账户每日额度，调用前原子预留，已知用量后幂等结算。"""
from alembic import op
import sqlalchemy as sa

revision = "0035_platform_token_quota"
down_revision = "0034_mail_poll_observation"
branch_labels = depends_on = None


def upgrade():
    op.create_table("platform_token_days",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False), sa.Column("reserved", sa.Integer(), nullable=False))
    op.create_table("platform_token_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.String(10), nullable=False), sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("charged", sa.Integer()), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("run_id", sa.String(36)), sa.Column("profile_id", sa.String(36), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False))
    op.create_index("ix_platform_tokens_owner_day", "platform_token_reservations", ["user_id", "day"])


def downgrade():
    """仅回退额度表，保留用户和业务数据。"""
    op.drop_table("platform_token_reservations")
    op.drop_table("platform_token_days")
