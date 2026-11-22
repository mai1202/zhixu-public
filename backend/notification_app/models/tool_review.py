"""工具执行前的独立审查回执，不存储提示词与个人通知正文。"""
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class AgentToolReview(Base):
    __tablename__ = "agent_tool_reviews"
    __table_args__ = (UniqueConstraint("run_id", "tool_call_id", name="uq_agent_tool_review_call"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    tool_call_id: Mapped[str] = mapped_column(String(200))
    tool_name: Mapped[str] = mapped_column(String(100))
    input_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(1000))
    created_at_epoch: Mapped[int] = mapped_column(Integer)
