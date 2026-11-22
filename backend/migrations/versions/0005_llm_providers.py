"""增加用户私有模型配置、加密密钥存储与无原文运行记录。"""

from alembic import op
import sqlalchemy as sa

revision = "0005_llm_providers"
down_revision = "0004_timetable_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_profiles",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("api_family", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("execution_target", sa.String(16), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=True),
        sa.Column("secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("fallback_policy", sa.String(24), nullable=False),
        sa.Column("last_test_status", sa.String(24), nullable=False),
        sa.Column("last_test_epoch", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "label", name="uq_provider_profiles_owner_label"),
    )
    op.create_index("ix_provider_profiles_owner_task", "provider_profiles", ["user_id", "purpose", "enabled"])
    op.create_table(
        "llm_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("provider_profile_id", sa.String(36), nullable=True),
        sa.Column("task_kind", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 8), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_profile_id"], ["provider_profiles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_runs_owner_created", "llm_runs", ["user_id", "created_at_epoch"])
    op.add_column("source_documents", sa.Column("analysis_mode", sa.String(16), server_default="rules", nullable=False))
    op.add_column("source_documents", sa.Column("provider_profile_id", sa.String(36), nullable=True))


def downgrade() -> None:
    op.drop_column("source_documents", "provider_profile_id")
    op.drop_column("source_documents", "analysis_mode")
    op.drop_index("ix_llm_runs_owner_created", table_name="llm_runs")
    op.drop_table("llm_runs")
    op.drop_index("ix_provider_profiles_owner_task", table_name="provider_profiles")
    op.drop_table("provider_profiles")
