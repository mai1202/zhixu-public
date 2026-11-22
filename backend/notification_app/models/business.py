from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SourceDocument(Base):
    """不可静默覆盖的用户原始材料。"""

    __tablename__ = "source_documents"
    __table_args__ = (
        Index("ix_source_documents_owner_created", "user_id", "created_at_epoch"),
        Index(
            "uq_source_documents_owner_external_key",
            "user_id",
            "source_type",
            "external_key",
            unique=True,
            sqlite_where=text("external_key IS NOT NULL"),
            postgresql_where=text("external_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_key: Mapped[str | None] = mapped_column(String(200))
    # 浏览器同次提交的请求摘要；与来源内容哈希分开，包含模型/标题等操作参数。
    submission_hash: Mapped[str | None] = mapped_column(String(64))
    analysis_state: Mapped[str] = mapped_column(String(24), nullable=False, default="received")
    analysis_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="rules", server_default="rules")
    provider_profile_id: Mapped[str | None] = mapped_column(String(36))
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at_epoch: Mapped[int | None] = mapped_column(Integer)


class SourceSegment(Base):
    """原文的稳定片段；后续修正不改变已有片段的引用位置。"""

    __tablename__ = "source_segments"
    __table_args__ = (
        UniqueConstraint("source_document_id", "ordinal", name="uq_source_segments_document_ordinal"),
        Index("ix_source_segments_owner_document", "user_id", "source_document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_document_id: Mapped[str] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class Notice(Base):
    """一份通知的当前状态；官方事实与个人安排分开存储。"""

    __tablename__ = "notices"
    __table_args__ = (
        Index("ix_notices_owner_status_updated", "user_id", "status", "updated_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_document_id: Mapped[str] = mapped_column(
        ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # 重要性与信息完整性用于面板排序，也用于决定是否触发主动邮件。
    importance: Mapped[str] = mapped_column(String(12), nullable=False, default="normal", server_default="normal")
    # 最近一次确认的变更集；详情页据此跳转到可撤销的操作记录。
    last_change_set_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at_epoch: Mapped[int | None] = mapped_column(Integer)


class Event(Base):
    """需要关注或完成的事项，期限精度与时段分别保留。"""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_owner_status_updated", "user_id", "status", "updated_at_epoch"),
        Index("ix_events_owner_notice", "user_id", "notice_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    notice_id: Mapped[str | None] = mapped_column(ForeignKey("notices.id", ondelete="RESTRICT"))
    source_document_id: Mapped[str] = mapped_column(
        ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # 身份与字段跟随策略独立于 1.0 提取契约，旧客户端无需升级。
    identity_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    field_policies_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    actor: Mapped[str | None] = mapped_column(String(500))
    stage: Mapped[str | None] = mapped_column(String(500))
    scope: Mapped[str | None] = mapped_column(String(1000))
    deadline_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    time_range_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    materials_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    submission: Mapped[str | None] = mapped_column(String(2000))
    eligibility: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    unknowns_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at_epoch: Mapped[int | None] = mapped_column(Integer)


class Task(Base):
    """用户待办；来源事件变化不会静默覆盖已完成的任务。"""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_owner_status_due", "user_id", "status", "due_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    # 直接来源与课程上下文是可查询的一等关联；关系表仍用于多对多证据链。
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    notice_id: Mapped[str | None] = mapped_column(ForeignKey("notices.id", ondelete="SET NULL"))
    course_series_id: Mapped[str | None] = mapped_column(ForeignKey("course_series.id", ondelete="SET NULL"))
    course_occurrence_id: Mapped[str | None] = mapped_column(
        ForeignKey("course_occurrences.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    task_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="assignment", server_default="assignment")
    priority: Mapped[str] = mapped_column(String(12), nullable=False, default="normal", server_default="normal")
    due_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 仅用于可比较的时刻排序与提醒扫描；原始日期精度仍保存在 due_json。
    due_at_epoch: Mapped[int | None] = mapped_column(Integer)
    # “上课前”“本周内”等无法转换成精确时刻的要求保留原文。
    original_requirement: Mapped[str | None] = mapped_column(String(1000))
    reminder_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at_epoch: Mapped[int | None] = mapped_column(Integer)
    archived_at_epoch: Mapped[int | None] = mapped_column(Integer)


class Revision(Base):
    """以对象版本保存确认前后的结构化快照。"""

    __tablename__ = "revisions"
    __table_args__ = (
        UniqueConstraint("user_id", "object_type", "object_id", "version", name="uq_revisions_object_version"),
        Index("ix_revisions_owner_object", "user_id", "object_type", "object_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_document_id: Mapped[str | None] = mapped_column(ForeignKey("source_documents.id", ondelete="SET NULL"))
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class ChangeSet(Base):
    """待预览、确认、驳回或撤销的一组受控操作。"""

    __tablename__ = "change_sets"
    __table_args__ = (
        Index("ix_change_sets_owner_operation_created", "user_id", "operation_state", "created_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_document_id: Mapped[str | None] = mapped_column(ForeignKey("source_documents.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # 通知此列为空；只有独立日程等操作保留事务生命周期，通知没有人工核对状态。
    status: Mapped[str | None] = mapped_column("operation_state", String(24), nullable=True)
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    bundle_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    operations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    change_seq: Mapped[int | None] = mapped_column(Integer)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_at_epoch: Mapped[int | None] = mapped_column(Integer)
    undone_at_epoch: Mapped[int | None] = mapped_column(Integer)


# 通知在所有写入路径都没有核对状态，统一约束也覆盖旧任务重放与通用审计工具。
from sqlalchemy import event as model_event


@model_event.listens_for(ChangeSet, "before_insert")
@model_event.listens_for(ChangeSet, "before_update")
def clear_notification_review_state(_mapper, _connection, target):
    if target.source_document_id or target.kind in {"normalize", "requirements"}:
        target.status = None
