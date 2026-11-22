"""跨模块关系、可追溯操作、主动提醒与已生成报告。"""

from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class BusinessLink(Base):
    """通知、任务、课程之间的真实多对多关系，避免把业务字段塞进一张大表。"""

    __tablename__ = "business_links"
    __table_args__ = (
        # 同一关系只保留一行；解除后重新关联时复用原记录，避免无限增长。
        UniqueConstraint(
            "user_id",
            "from_type",
            "from_id",
            "to_type",
            "to_id",
            "relation",
            name="uq_business_links_owner_edge",
        ),
        Index("ix_business_links_owner_from", "user_id", "from_type", "from_id", "status"),
        Index("ix_business_links_owner_to", "user_id", "to_type", "to_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    from_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_id: Mapped[str] = mapped_column(String(36), nullable=False)
    to_type: Mapped[str] = mapped_column(String(32), nullable=False)
    to_id: Mapped[str] = mapped_column(String(36), nullable=False)
    relation: Mapped[str] = mapped_column(String(32), nullable=False, default="related")
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class OperationLog(Base):
    """统一记录人工、Agent 和系统产生的业务变更，供活动流与撤销入口使用。"""

    __tablename__ = "operation_logs"
    __table_args__ = (
        Index("ix_operation_logs_owner_created", "user_id", "created_at_epoch"),
        Index("ix_operation_logs_owner_object", "user_id", "object_type", "object_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(36), nullable=False)
    change_set_id: Mapped[str | None] = mapped_column(ForeignKey("change_sets.id", ondelete="SET NULL"))
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="applied")
    undoable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class Reminder(Base):
    """任务或课程触发的提醒计划；唯一键确保 worker 重试不会重复发信。"""

    __tablename__ = "reminders"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_reminders_dedupe_key"),
        Index("ix_reminders_owner_status_due", "user_id", "status", "scheduled_at_epoch"),
        Index("ix_reminders_task", "user_id", "task_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    notice_id: Mapped[str | None] = mapped_column(ForeignKey("notices.id", ondelete="SET NULL"))
    course_occurrence_id: Mapped[str | None] = mapped_column(
        ForeignKey("course_occurrences.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    scheduled_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    offset_minutes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    dedupe_key: Mapped[str] = mapped_column(String(250), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    outbox_id: Mapped[str | None] = mapped_column(ForeignKey("mail_outbox.id", ondelete="SET NULL"))
    sent_at_epoch: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class ActivityReport(Base):
    """日报/周报的不可变快照，支持网页回看和邮件投递状态追踪。"""

    __tablename__ = "activity_reports"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_activity_reports_dedupe_key"),
        Index("ix_activity_reports_owner_kind_period", "user_id", "kind", "period_start"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[str] = mapped_column(String(10), nullable=False)
    period_end: Mapped[str] = mapped_column(String(10), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="generated")
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    sections_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(250), nullable=False)
    outbox_id: Mapped[str | None] = mapped_column(ForeignKey("mail_outbox.id", ondelete="SET NULL"))
    generated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at_epoch: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AgentInsight(Base):
    """后台主动扫描得到的事实型提醒，支持模块筛选、关闭和自动恢复。"""

    __tablename__ = "agent_insights"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_agent_insights_dedupe_key"),
        Index("ix_agent_insights_owner_module", "user_id", "module", "status", "severity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    module: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[str] = mapped_column(String(2000), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(32))
    object_id: Mapped[str | None] = mapped_column(String(36))
    action_href: Mapped[str | None] = mapped_column(String(500))
    dedupe_key: Mapped[str] = mapped_column(String(250), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
