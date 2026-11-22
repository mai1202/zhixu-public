"""办理路线、轻量责任交接与机会偏好；旧业务表保持原样。"""

from alembic import op
import sqlalchemy as sa

revision = "0027_student_journeys"
down_revision = "0026_user_onboarding"
branch_labels = depends_on = None


def upgrade():
    """新增可级联清理的账户资料，并为单任务路线建立唯一约束。"""
    op.create_table("student_journeys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False), sa.Column("shared_title", sa.String(200)),
        sa.Column("steps_json", sa.JSON(), nullable=False), sa.Column("source_version", sa.Integer()),
        sa.Column("invite_digest", sa.String(64), unique=True), sa.Column("invite_expires_at_epoch", sa.Integer()),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.UniqueConstraint("task_id", name="uq_student_journeys_task"))
    op.create_table("journey_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("journey_id", sa.String(36), sa.ForeignKey("student_journeys.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alias", sa.String(40), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.UniqueConstraint("journey_id", "user_id", name="uq_journey_members_user"))
    op.create_index("ix_journey_members_user", "journey_members", ["user_id", "status"])
    op.create_table("student_intents",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("profile_json", sa.JSON(), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False))


def downgrade():
    """先删除成员子表，再删除路线与偏好表。"""
    op.drop_table("journey_members")
    op.drop_table("student_journeys")
    op.drop_table("student_intents")
