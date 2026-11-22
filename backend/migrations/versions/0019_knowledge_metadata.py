"""知识文档发布单位、日期、适用人群与替代关系。"""
from alembic import op
import sqlalchemy as sa

revision = '0019_knowledge_metadata'
down_revision = '0018_watch_snapshot_and_chunks'
branch_labels = depends_on = None


def upgrade():
    """只增加可选元数据，不改变原文和既有版本。"""
    op.add_column('knowledge_documents', sa.Column('metadata_json', sa.JSON()))


def downgrade():
    op.drop_column('knowledge_documents', 'metadata_json')
