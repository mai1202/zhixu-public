"""课程系列保存真实起止时间；旧课表保持按学期作息生成。"""
from alembic import op
import sqlalchemy as sa
from datetime import date, datetime, time
import json
import re
from zoneinfo import ZoneInfo

revision = "0036_course_actual_times"
down_revision = "0035_platform_token_quota"
branch_labels = None
depends_on = None


def upgrade():
    # 可空字段保证历史课程和正在运行的旧版程序仍可读取已有记录。
    op.add_column("course_series", sa.Column("start_time", sa.String(5), nullable=True))
    op.add_column("course_series", sa.Column("end_time", sa.String(5), nullable=True))
    # 历史原文明示时间时修复整组作息扩张；人工修改后的说明不含时刻，不猜测回填。
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT s.id, s.raw_text, t.timezone FROM course_series s JOIN academic_terms t ON t.id=s.term_id")).mappings()
    clock = r"(?:[01]?\d|2[0-3])[:：][0-5]\d"
    for row in rows:
        matches = list(re.finditer(rf"(?<!\d)({clock})\s*[-—–~～至]\s*({clock})(?!\d)", row["raw_text"]))
        if len(matches) != 1:
            continue  # 多个区间无法唯一解释，保留旧记录供用户核对。
        start, end = [f"{int(value.replace('：', ':').split(':')[0]):02d}:{value[-2:]}" for value in matches[0].groups()]
        if end <= start:
            continue
        connection.execute(sa.text("UPDATE course_series SET start_time=:start,end_time=:end,version=version+1 WHERE id=:id"), dict(start=start, end=end, id=row['id']))
        occurrences = connection.execute(sa.text("SELECT o.id,o.local_date FROM course_occurrences o WHERE o.course_series_id=:id AND o.status='scheduled' AND o.occurrence_key=(o.course_series_id || '|' || o.local_date) AND NOT EXISTS (SELECT 1 FROM course_exceptions e WHERE e.occurrence_id=o.id AND e.status='active')"), dict(id=row['id'])).mappings().all()
        for occurrence in occurrences:
            day = date.fromisoformat(occurrence['local_date']); zone = ZoneInfo(row['timezone'])
            segments = [dict(start_at=datetime.combine(day, time.fromisoformat(start), zone).isoformat(), end_at=datetime.combine(day, time.fromisoformat(end), zone).isoformat())]
            connection.execute(sa.text("UPDATE course_occurrences SET display_start=:start,display_end=:end,busy_segments_json=:segments,version=version+1 WHERE id=:id"), dict(start=start, end=end, segments=json.dumps(segments), id=occurrence['id']))


def downgrade():
    # 当前SQLite和PostgreSQL均支持原生删列，避免重建父表触发课程实例外键级联删除。
    op.drop_column("course_series", "end_time")
    op.drop_column("course_series", "start_time")
