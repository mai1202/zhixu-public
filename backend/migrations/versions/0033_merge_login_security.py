"""合并事务规划与登录设备两条迁移链，不修改已有业务记录。"""

revision = '0033_merge_login_security'
down_revision = ('0031_planning_runs', '0032_web_session_devices')
branch_labels = depends_on = None


def upgrade():
    """两条父迁移分别建表与补充会话字段，此处只收敛到单一版本。"""
    pass


def downgrade():
    """回退合并标记时保留两个父版本的数据与结构。"""
    pass
