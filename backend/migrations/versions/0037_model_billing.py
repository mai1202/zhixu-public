"""逐调用人民币账本；原有 token 记录保留，历史费用明确标为未知。"""
from alembic import op
import sqlalchemy as sa

revision = "0037_model_billing"
down_revision = "0036_course_actual_times"
branch_labels = depends_on = None


def upgrade():
    # 原表新增列即可，不重建带外键的父表，兼容 SQLite 与 PostgreSQL。
    for name in ("spent_micros", "held_micros"):
        op.add_column("platform_token_days", sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("platform_token_reservations", sa.Column("platform_paid", sa.Boolean(), nullable=False, server_default=sa.true()))
    for name in ("reserved_micros", "charged_micros"):
        op.add_column("platform_token_reservations", sa.Column(name, sa.BigInteger()))
    for name in ("pricing_json", "usage_json"):
        op.add_column("platform_token_reservations", sa.Column(name, sa.JSON()))
    op.add_column("provider_profiles", sa.Column("billing_prices_json", sa.JSON()))


def downgrade():
    # SQLite 原生删列不触发 ORM 级联删除，业务数据不受回退影响。
    op.drop_column("provider_profiles", "billing_prices_json")
    for name in ("usage_json", "pricing_json", "charged_micros", "reserved_micros", "platform_paid"):
        op.drop_column("platform_token_reservations", name)
    for name in ("held_micros", "spent_micros"):
        op.drop_column("platform_token_days", name)
