"""后台 Agent 主动扫描得到的模块化建议。"""

from alembic import op
import sqlalchemy as sa

revision = "0015_agent_insights"
down_revision = "0014_cross_module_agent"
branch_labels = depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_insights",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("module", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("detail", sa.String(2000), nullable=False),
        sa.Column("object_type", sa.String(32), nullable=True),
        sa.Column("object_id", sa.String(36), nullable=True),
        sa.Column("action_href", sa.String(500), nullable=True),
        sa.Column("dedupe_key", sa.String(250), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_agent_insights_dedupe_key"),
    )
    op.create_index(
        "ix_agent_insights_owner_module",
        "agent_insights",
        ["user_id", "module", "status", "severity"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_insights_owner_module", table_name="agent_insights")
    op.drop_table("agent_insights")
