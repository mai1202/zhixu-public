"""通知整理与助手定时计划，独立于需要精确时间的日程和事务。"""

from sqlalchemy import ForeignKey, Integer, JSON, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class NotificationOrganization(Base):
    """一份原文对应一份整理结果，已读状态不等同于是否完成任务。"""

    __tablename__ = 'notification_organizations'
    source_id: Mapped[str] = mapped_column(ForeignKey('source_documents.id', ondelete='CASCADE'), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    title: Mapped[str | None] = mapped_column(String(100))
    summary: Mapped[str | None] = mapped_column(Text)
    advice: Mapped[str | None] = mapped_column(Text)
    # 归组只改变阅读入口，所有成员原文及各自整理结果保持可追溯。
    group_source_id: Mapped[str | None] = mapped_column(ForeignKey('source_documents.id', ondelete='SET NULL'), index=True)
    group_reason: Mapped[str | None] = mapped_column(Text)
    grouping_locked: Mapped[int] = mapped_column(Integer, default=0)
    mailbox: Mapped[str] = mapped_column(String(16), default='inbox')
    read_at_epoch: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at_epoch: Mapped[int] = mapped_column(Integer)


class AgentExecutionPlan(Base):
    """复杂请求的持久短计划；步骤关联对象版本和工具回执，便于失败恢复。"""
    __tablename__ = 'agent_execution_plans'
    __table_args__ = (Index('ix_agent_plans_owner_status', 'user_id', 'status', 'updated_at_epoch'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default='active')
    steps_json: Mapped[list] = mapped_column(JSON, default=list)
    # 跨运行写工具回执：恢复/重试时按工具名和参数哈希返回原结果，避免重复副作用。
    tool_receipts_json: Mapped[list | None] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer)
    updated_at_epoch: Mapped[int] = mapped_column(Integer)


class AgentScheduledTask(Base):
    """可取消的持久唤醒计划；执行生成普通 Agent Job，不另造模型循环。"""

    __tablename__ = 'agent_scheduled_tasks'
    __table_args__ = (Index('ix_agent_schedules_due', 'status', 'next_run_at_epoch'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    title: Mapped[str] = mapped_column(String(150))
    message: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16))
    next_run_at_epoch: Mapped[int] = mapped_column(Integer)
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    attachment_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default='active')
    version: Mapped[int] = mapped_column(Integer, default=1)
    last_run_id: Mapped[str | None] = mapped_column(String(36))
    # 持续关注只在有意义的资料变化时唤醒，避免定时生成无变化的消息。
    watch_query: Mapped[str | None] = mapped_column(String(300))
    watch_scope: Mapped[str] = mapped_column(String(16), default='all')
    # 官网关注保存公开 HTTPS 地址，按周期条件读取并比较正文。
    watch_url: Mapped[str | None] = mapped_column(String(2000))
    last_watch_fingerprint: Mapped[str | None] = mapped_column(String(64))
    last_watch_checked_epoch: Mapped[int | None] = mapped_column(Integer)
    # 快照用于区分新增、日期/地点/材料要求变化和仅排版变化。
    last_watch_snapshot: Mapped[list | None] = mapped_column(JSON)
    last_watch_delta_json: Mapped[dict | None] = mapped_column(JSON)
    last_success_epoch: Mapped[int | None] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_notified_epoch: Mapped[int | None] = mapped_column(Integer)
    # 关注变化在模型运行成功前只进入待确认状态，不能提前推进快照或标记已通知。
    pending_watch_snapshot: Mapped[list | None] = mapped_column(JSON)
    pending_watch_delta_json: Mapped[dict | None] = mapped_column(JSON)
    pending_run_id: Mapped[str | None] = mapped_column(String(36))
    pending_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    pending_since_epoch: Mapped[int | None] = mapped_column(Integer)
    run_count: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    stop_at_epoch: Mapped[int | None] = mapped_column(Integer)
    created_at_epoch: Mapped[int] = mapped_column(Integer)
