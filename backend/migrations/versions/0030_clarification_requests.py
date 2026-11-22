"""可恢复的版本化核对问题。"""
from alembic import op
import sqlalchemy as sa

revision = "0030_clarification_requests"
down_revision = "0029_requirement_claims"
branch_labels = depends_on = None


def upgrade():
    """核对答案不替代用户偏好或通知证据。"""
    op.create_table("clarification_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("change_set_id", sa.String(36), sa.ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.String(36)), sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("question", sa.String(1000), nullable=False), sa.Column("candidates_json", sa.JSON(), nullable=False),
        sa.Column("answer_json", sa.JSON()), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("answered_at_epoch", sa.Integer()))
    op.create_index("ix_clarifications_owner_status", "clarification_requests", ["user_id", "status"])


def downgrade():
    """回退只移除核对记录。"""
    op.drop_table("clarification_requests")
