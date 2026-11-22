"""增加课程导入、学期规则、课程实例与个人日程。"""

from alembic import op
import sqlalchemy as sa

revision = "0004_timetable_schedule"
down_revision = "0003_business_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 学期锚点和作息表按用户保存，不从打印日期猜测开学周。
    op.create_table(
        "academic_terms",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("term_code", sa.String(32), nullable=False),
        sa.Column("week1_monday", sa.String(10), nullable=False),
        sa.Column("week_count", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("period_profile_json", sa.JSON(), nullable=False),
        sa.Column("anchor_source", sa.String(500), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "term_code", name="uq_academic_terms_owner_code"),
    )
    op.create_index("ix_academic_terms_owner", "academic_terms", ["user_id", "week1_monday"])
    op.create_table(
        "timetable_imports",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("term_id", sa.String(36), nullable=True),
        sa.Column("term_code", sa.String(32), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(80), nullable=False),
        sa.Column("file_format", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("apply_mode", sa.String(24), nullable=True),
        sa.Column("draft_json", sa.JSON(), nullable=False),
        sa.Column("diff_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("confirmed_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["term_id"], ["academic_terms.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "file_hash", name="uq_timetable_imports_owner_hash"),
    )
    op.create_index("ix_timetable_imports_owner_created", "timetable_imports", ["user_id", "created_at_epoch"])
    op.create_table(
        "course_series",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("term_id", sa.String(36), nullable=False),
        sa.Column("source_import_id", sa.String(36), nullable=False),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column("teachers_json", sa.JSON(), nullable=False),
        sa.Column("room", sa.String(500), nullable=True),
        sa.Column("week_set_json", sa.JSON(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("periods_json", sa.JSON(), nullable=False),
        sa.Column("raw_text", sa.String(20000), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["term_id"], ["academic_terms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_import_id"], ["timetable_imports.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "term_id", "identity_key", name="uq_course_series_owner_identity"),
    )
    op.create_index("ix_course_series_owner_term_status", "course_series", ["user_id", "term_id", "status"])
    op.create_table(
        "course_occurrences",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("term_id", sa.String(36), nullable=False),
        sa.Column("course_series_id", sa.String(36), nullable=False),
        sa.Column("source_import_id", sa.String(36), nullable=False),
        sa.Column("occurrence_key", sa.String(200), nullable=False),
        sa.Column("local_date", sa.String(10), nullable=False),
        sa.Column("week_number", sa.Integer(), nullable=False),
        sa.Column("display_start", sa.String(5), nullable=False),
        sa.Column("display_end", sa.String(5), nullable=False),
        sa.Column("busy_segments_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["term_id"], ["academic_terms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["course_series_id"], ["course_series.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_import_id"], ["timetable_imports.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "occurrence_key", name="uq_course_occurrences_owner_key"),
    )
    op.create_index("ix_course_occurrences_owner_date", "course_occurrences", ["user_id", "local_date", "status"])
    op.create_table(
        "course_exceptions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("occurrence_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("original_json", sa.JSON(), nullable=False),
        sa.Column("replacement_json", sa.JSON(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["occurrence_id"], ["course_occurrences.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_course_exceptions_one_active",
        "course_exceptions",
        ["user_id", "occurrence_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "schedule_entries",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("source_event_id", sa.String(36), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("local_date", sa.String(10), nullable=False),
        sa.Column("start_at", sa.String(40), nullable=True),
        sa.Column("end_at", sa.String(40), nullable=True),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_epoch", sa.Integer(), nullable=False),
        sa.Column("updated_at_epoch", sa.Integer(), nullable=False),
        sa.Column("archived_at_epoch", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_event_id"], ["events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedule_entries_owner_date", "schedule_entries", ["user_id", "local_date", "status"])


def downgrade() -> None:
    op.drop_index("ix_schedule_entries_owner_date", table_name="schedule_entries")
    op.drop_table("schedule_entries")
    op.drop_index("uq_course_exceptions_one_active", table_name="course_exceptions")
    op.drop_table("course_exceptions")
    op.drop_index("ix_course_occurrences_owner_date", table_name="course_occurrences")
    op.drop_table("course_occurrences")
    op.drop_index("ix_course_series_owner_term_status", table_name="course_series")
    op.drop_table("course_series")
    op.drop_index("ix_timetable_imports_owner_created", table_name="timetable_imports")
    op.drop_table("timetable_imports")
    op.drop_index("ix_academic_terms_owner", table_name="academic_terms")
    op.drop_table("academic_terms")
