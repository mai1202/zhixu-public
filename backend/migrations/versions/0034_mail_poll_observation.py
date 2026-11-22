"""邮件轮询完成后才记录成功时间，不能把开始轮询当作成功。"""
from alembic import op
import sqlalchemy as sa

revision = "0034_mail_poll_observation"
down_revision = "0033_merge_login_security"
branch_labels = depends_on = None


def upgrade():
    """历史游标缺少可靠观测，保持未知。"""
    op.add_column("mail_ingress_cursors", sa.Column("last_success_epoch", sa.Integer(), nullable=True))


def downgrade():
    """不改变既有收信UID和内容指纹。"""
    with op.batch_alter_table("mail_ingress_cursors") as batch:
        batch.drop_column("last_success_epoch")
