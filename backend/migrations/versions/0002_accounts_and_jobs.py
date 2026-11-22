"""增加邮箱验证、用户会话和持久任务表。"""

from alembic import op
import sqlalchemy as sa

revision = "0002_accounts_and_jobs"
down_revision = "0001_p0_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 身份、验证地址和会话分别建表，令授权来源与普通业务字段保持清晰。
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("primary_email", sa.String(length=254), nullable=False),
        sa.Column("email_key", sa.String(length=254), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("verified_at_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email_key", name="uq_users_email_key"),
    )
    op.create_table(
        "verified_addresses",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("email_key", sa.String(length=254), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("verified_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email_key", name="uq_verified_addresses_email_key"),
        sa.UniqueConstraint("user_id", "email_key", name="uq_verified_addresses_owner_email"),
    )
    op.create_index("ix_verified_addresses_user", "verified_addresses", ["user_id"])
    op.create_table(
        "email_challenges",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("email_key", sa.String(length=254), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("issued_at_epoch", sa.Integer(), nullable=False),
        sa.Column("expires_at_epoch", sa.Integer(), nullable=False),
        sa.Column("consumed_at_epoch", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_challenges_email_issued",
        "email_challenges",
        ["email_key", "issued_at_epoch"],
    )
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("expires_at_epoch", sa.Integer(), nullable=False),
        sa.Column("revoked_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_web_sessions_token_digest"),
    )
    op.create_index("ix_web_sessions_user_expiry", "web_sessions", ["user_id", "expires_at_epoch"])
    op.create_table(
        "rate_limit_buckets",
        sa.Column("bucket_key", sa.String(length=64), nullable=False),
        sa.Column("window_started_epoch", sa.Integer(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("bucket_key"),
    )
    # 任务的幂等键按用户和类型唯一，防止重试跨用户复用结果。
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("run_at_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_until_epoch", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("input_version", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("finished_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "kind",
            "idempotency_key",
            name="uq_jobs_owner_kind_idempotency",
        ),
    )
    op.create_index("ix_jobs_status_run_at", "jobs", ["status", "run_at_epoch"])
    op.create_index("ix_jobs_lease_until", "jobs", ["status", "lease_until_epoch"])
    op.create_table(
        "job_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("started_at_epoch", sa.Integer(), nullable=False),
        sa.Column("finished_at_epoch", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt", name="uq_job_attempts_job_attempt"),
    )
    op.create_index("ix_job_attempts_job_started", "job_attempts", ["job_id", "started_at_epoch"])


def downgrade() -> None:
    op.drop_index("ix_job_attempts_job_started", table_name="job_attempts")
    op.drop_table("job_attempts")
    op.drop_index("ix_jobs_lease_until", table_name="jobs")
    op.drop_index("ix_jobs_status_run_at", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("rate_limit_buckets")
    op.drop_index("ix_web_sessions_user_expiry", table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_index("ix_email_challenges_email_issued", table_name="email_challenges")
    op.drop_table("email_challenges")
    op.drop_index("ix_verified_addresses_user", table_name="verified_addresses")
    op.drop_table("verified_addresses")
    op.drop_table("users")
