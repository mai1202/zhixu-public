"""复用来源外部键，为网页提交增加输入冲突校验。"""

from alembic import op
import sqlalchemy as sa

revision = "0010_source_submission_hash"
down_revision = "0009_mail_and_change_cursors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 可空字段兼容旧来源；不改变内容哈希和既有设备同步键。
    op.add_column("source_documents", sa.Column("submission_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    # 降级只移除网页提交摘要，原始材料保持不变。
    op.drop_column("source_documents", "submission_hash")
