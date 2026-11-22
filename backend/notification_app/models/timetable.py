from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AcademicTerm(Base):
    """一个用户的学期锚点、周数、时区和已确认节次作息表。"""

    __tablename__ = "academic_terms"
    __table_args__ = (
        UniqueConstraint("user_id", "term_code", name="uq_academic_terms_owner_code"),
        Index("ix_academic_terms_owner", "user_id", "week1_monday"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    term_code: Mapped[str] = mapped_column(String(32), nullable=False)
    week1_monday: Mapped[str] = mapped_column(String(10), nullable=False)
    week_count: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    period_profile_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    anchor_source: Mapped[str] = mapped_column(String(500), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class TimetableImport(Base):
    """同用户同文件哈希幂等的课表预览、变更差异和确认记录。"""

    __tablename__ = "timetable_imports"
    __table_args__ = (
        UniqueConstraint("user_id", "file_hash", name="uq_timetable_imports_owner_hash"),
        Index("ix_timetable_imports_owner_created", "user_id", "created_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    term_id: Mapped[str | None] = mapped_column(ForeignKey("academic_terms.id", ondelete="SET NULL"))
    term_code: Mapped[str] = mapped_column(String(32), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(80), nullable=False)
    file_format: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    apply_mode: Mapped[str | None] = mapped_column(String(24))
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    diff_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_at_epoch: Mapped[int | None] = mapped_column(Integer)


class CourseSeries(Base):
    """按周集合和星期定义的课程系列；每个学期有独立规则版本。"""

    __tablename__ = "course_series"
    __table_args__ = (
        UniqueConstraint("user_id", "term_id", "identity_key", name="uq_course_series_owner_identity"),
        Index("ix_course_series_owner_term_status", "user_id", "term_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    term_id: Mapped[str] = mapped_column(ForeignKey("academic_terms.id", ondelete="CASCADE"), nullable=False)
    source_import_id: Mapped[str] = mapped_column(ForeignKey("timetable_imports.id", ondelete="RESTRICT"), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    teachers_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    room: Mapped[str | None] = mapped_column(String(500))
    week_set_json: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    periods_json: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    # 原文或用户确认的真实时刻优先于节次作息，空值表示继续按学期作息生成。
    start_time: Mapped[str | None] = mapped_column(String(5))
    end_time: Mapped[str | None] = mapped_column(String(5))
    raw_text: Mapped[str] = mapped_column(String(20_000), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class CourseOccurrence(Base):
    """物化的上课实例，显示范围与排冲突忙碌片段分开存储。"""

    __tablename__ = "course_occurrences"
    __table_args__ = (
        UniqueConstraint("user_id", "occurrence_key", name="uq_course_occurrences_owner_key"),
        Index("ix_course_occurrences_owner_date", "user_id", "local_date", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    term_id: Mapped[str] = mapped_column(ForeignKey("academic_terms.id", ondelete="CASCADE"), nullable=False)
    course_series_id: Mapped[str] = mapped_column(ForeignKey("course_series.id", ondelete="CASCADE"), nullable=False)
    source_import_id: Mapped[str] = mapped_column(ForeignKey("timetable_imports.id", ondelete="RESTRICT"), nullable=False)
    occurrence_key: Mapped[str] = mapped_column(String(200), nullable=False)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    week_number: Mapped[int] = mapped_column(Integer, nullable=False)
    display_start: Mapped[str] = mapped_column(String(5), nullable=False)
    display_end: Mapped[str] = mapped_column(String(5), nullable=False)
    busy_segments_json: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False)
    # 单次地点变化只覆盖本次实例，不修改整学期课程系列。
    room_override: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CourseException(Base):
    """单次取消或改期，不直接改写整条课程系列。"""

    __tablename__ = "course_exceptions"
    __table_args__ = (
        Index(
            "uq_course_exceptions_one_active",
            "user_id",
            "occurrence_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey("course_occurrences.id", ondelete="CASCADE"), nullable=False)
    # 通知依据与操作来源作为一等字段，通知详情可以反查受影响的课程实例。
    notice_id: Mapped[str | None] = mapped_column(ForeignKey("notices.id", ondelete="SET NULL"))
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    change_set_id: Mapped[str | None] = mapped_column(ForeignKey("change_sets.id", ondelete="SET NULL"))
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False, default="user", server_default="user")
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    original_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    replacement_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class ScheduleEntry(Base):
    """用户个人的时间块、全天事项或仅显示在截止区的日期标记。"""

    __tablename__ = "schedule_entries"
    __table_args__ = (
        Index("ix_schedule_entries_owner_date", "user_id", "local_date", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # 历史和新建安排默认均不可移动；页面明确授权后才保存许可与范围。
    planning_policy_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    start_at: Mapped[str | None] = mapped_column(String(40))
    end_at: Mapped[str | None] = mapped_column(String(40))
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at_epoch: Mapped[int | None] = mapped_column(Integer)
