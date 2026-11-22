from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AgentConversation(Base):
    """属于单个用户的持久对话及其递增版本。"""

    __tablename__ = "agent_conversations"
    __table_args__ = (
        Index("ix_agent_conversations_owner_updated", "user_id", "updated_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at_epoch: Mapped[int | None] = mapped_column(Integer)
    memory_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AgentTurn(Base):
    """对话输入与助手输出；运行结果由持久 Job 负责恢复和取消。"""

    __tablename__ = "agent_turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_agent_turns_conversation_sequence"),
        UniqueConstraint("conversation_id", "run_id", "role", name="uq_agent_turns_run_role"),
        Index("ix_agent_turns_owner_conversation", "user_id", "conversation_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentAttachment(Base):
    """账户私有附件，存储键由服务器生成，解析内容不作为指令执行。"""

    __tablename__ = "agent_attachments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(240))
    storage_key: Mapped[str] = mapped_column(String(100), unique=True)
    media_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    ocr_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("ocr_artifacts.id"))
    created_at_epoch: Mapped[int] = mapped_column(Integer)


class AgentPreference(Base):
    """主助手的明确托管开关、模型、思考强度与上下文容量。"""

    __tablename__ = "agent_preferences"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("provider_profiles.id", ondelete="SET NULL"))
    # 新账户默认启用通知维护；既有用户明确保存的选择不被覆盖。
    automatic_notifications: Mapped[int] = mapped_column(Integer, default=1)
    context_budget: Mapped[int] = mapped_column(Integer, default=64000)
    # 思考强度由账户本人选择；运行时仍会根据具体模型的声明自动降级。
    thinking_level: Mapped[str] = mapped_column(String(16), default="medium")
    main_conversation_id: Mapped[str | None] = mapped_column(ForeignKey("agent_conversations.id", ondelete="SET NULL"))


class AgentMemory(Base):
    """用户级长期记忆，跨对话注入；只保存用户明确要求记住的稳定信息。"""

    __tablename__ = "agent_memories"
    __table_args__ = (
        UniqueConstraint("user_id", "normalized_content", name="uq_agent_memories_owner_content"),
        Index("ix_agent_memories_owner_updated", "user_id", "updated_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="SET NULL")
    )
    category: Mapped[str] = mapped_column(String(24), nullable=False, default="other")
    content: Mapped[str] = mapped_column(String(1000), nullable=False)
    normalized_content: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentRunEvent(Base):
    """流式消息与工具进度持久保存，刷新页面可按序号追上。"""

    __tablename__ = "agent_run_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class AgentRunControl(Base):
    """取消请求与工具幂等收据独立于不可修改的队列输入。"""

    __tablename__ = "agent_run_controls"
    run_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    cancelled: Mapped[int] = mapped_column(Integer, default=0)


class AgentToolReceipt(Base):
    """同一工作中的工具调用只应用一次，返回原执行结果。"""

    __tablename__ = "agent_tool_receipts"
    run_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    tool_call_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON)
