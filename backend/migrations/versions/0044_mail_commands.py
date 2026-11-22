"""邮件操作使用独立凭证，既有收件码始终只能投递资料。"""
from alembic import op
import sqlalchemy as sa
revision = "0044_mail_commands"
down_revision = "0043_privacy_consent"
branch_labels = depends_on = None


def upgrade():
    """旧账户默认不开启邮件操作。"""
    op.add_column("mail_ingress_accounts", sa.Column("command_enabled", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("mail_ingress_accounts", sa.Column("command_token_digest", sa.String(64)))
    op.add_column("mail_ingress_accounts", sa.Column("command_token_ciphertext", sa.Text()))


def downgrade():
    """保留既有收信配置。"""
    with op.batch_alter_table("mail_ingress_accounts") as batch:
        for field in ("command_token_ciphertext", "command_token_digest", "command_enabled"): batch.drop_column(field)
