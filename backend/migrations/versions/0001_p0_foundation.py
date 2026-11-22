"""建立部署实例元信息表。"""

from alembic import op
import sqlalchemy as sa

revision = "0001_p0_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 该表只记录数据库结构起点，不包含注册用户或任何业务数据。
    op.create_table(
        "app_metadata",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("foundation_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_app_metadata_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO app_metadata (id, foundation_version) "
            "VALUES (:id, :foundation_version)"
        ).bindparams(id=1, foundation_version="p0-1")
    )


def downgrade() -> None:
    op.drop_table("app_metadata")
