"""增加登录告知版本与经过身份验证的同意记录。"""
from alembic import op
import sqlalchemy as sa
revision = "0043_privacy_consent"
down_revision = "0042_trusted_journey_sources"
branch_labels = depends_on = None


def upgrade():
    """不为旧账户补写同意，留待用户下次主动确认。"""
    op.add_column("email_challenges", sa.Column("policy_version", sa.String(40)))
    op.create_table("user_privacy_consents",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("policy_version", sa.String(40), nullable=False), sa.Column("accepted_at_epoch", sa.Integer(), nullable=False))


def downgrade():
    """仅删除新增告知记录。"""
    op.drop_table("user_privacy_consents")
    with op.batch_alter_table("email_challenges") as batch: batch.drop_column("policy_version")
