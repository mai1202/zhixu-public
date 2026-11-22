"""保存明确分享的结构化要求与成员自行选择的来源信任。"""
from alembic import op
import sqlalchemy as sa
revision = "0042_trusted_journey_sources"
down_revision = "0041_agent_tool_review"
branch_labels = depends_on = None


def upgrade():
    """新成员默认不信任来源，不向任何用户自动复制历史通知。"""
    for table, columns in (
        ("journey_publications", [sa.Column("event_json", sa.JSON(), nullable=False), sa.Column("created_at_epoch", sa.Integer(), nullable=False)]),
        ("journey_source_trust", [sa.Column("enabled", sa.Boolean(), nullable=False), sa.UniqueConstraint("journey_id", "user_id", name="uq_journey_source_trust")]),
    ):
        op.create_table(table, sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("journey_id", sa.String(36), sa.ForeignKey("student_journeys.id", ondelete="CASCADE"), nullable=False), *columns)
        op.create_index(f"ix_{table}_journey_id", table, ["journey_id"])


def downgrade():
    """已接收的私人通知不随信任配置删除。"""
    op.drop_table("journey_source_trust")
    op.drop_table("journey_publications")
