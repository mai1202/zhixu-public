"""补充路线留言、私有文件收集和可撤回的共同空闲授权。"""

from alembic import op
import sqlalchemy as sa

revision = "0039_journey_collaboration"
down_revision = "0038_remove_notification_review"
branch_labels = depends_on = None


def upgrade():
    """复用既有用户、路线及附件，不迁移或修改已有用户内容。"""
    for name, extra in (
        ("journey_comments", [sa.Column("content", sa.Text(), nullable=False), sa.Column("created_at_epoch", sa.Integer(), nullable=False)]),
        ("journey_files", [sa.Column("attachment_id", sa.String(36), sa.ForeignKey("agent_attachments.id", ondelete="CASCADE"), nullable=False),
                           sa.Column("audience", sa.String(16), nullable=False), sa.Column("created_at_epoch", sa.Integer(), nullable=False)]),
        ("journey_availability", [sa.Column("availability_json", sa.JSON(), nullable=False), sa.Column("expires_at_epoch", sa.Integer(), nullable=False),
                                  sa.Column("version", sa.Integer(), nullable=False), sa.UniqueConstraint("journey_id", "user_id", name="uq_journey_availability_member")]),
    ):
        op.create_table(name, sa.Column("id", sa.String(36), primary_key=True),
                        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
                        sa.Column("journey_id", sa.String(36), sa.ForeignKey("student_journeys.id", ondelete="CASCADE"), nullable=False), *extra)
        op.create_index(f"ix_{name}_journey_id", name, ["journey_id"])


def downgrade():
    """仅回退本次新增协作表，不清除既有附件。"""
    for name in ("journey_availability", "journey_files", "journey_comments"):
        op.drop_table(name)
