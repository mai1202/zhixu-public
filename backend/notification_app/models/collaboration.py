"""路线协作内容与逐人授权；私人课表及来源原文不复制到共享空间。"""

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class JourneyComment(Base):
    """成员主动发表的留言，访问资格始终以当前成员状态为准。"""
    __tablename__ = "journey_comments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("student_journeys.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at_epoch: Mapped[int] = mapped_column(Integer)


class JourneyFile(Base):
    """文件复用已有私有附件存储，分享范围单独记录并支持撤回。"""
    __tablename__ = "journey_files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("student_journeys.id", ondelete="CASCADE"), index=True)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("agent_attachments.id", ondelete="CASCADE"))
    audience: Mapped[str] = mapped_column(String(16))
    created_at_epoch: Mapped[int] = mapped_column(Integer)


class JourneyAvailability(Base):
    """本人明确授权的空闲窗口，有效期结束或退出后不再参与统筹。"""
    __tablename__ = "journey_availability"
    __table_args__ = (UniqueConstraint("journey_id", "user_id", name="uq_journey_availability_member"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("student_journeys.id", ondelete="CASCADE"), index=True)
    availability_json: Mapped[dict] = mapped_column(JSON)
    expires_at_epoch: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)


class JourneyPublication(Base):
    """发起人明确分享的结构化要求，不复制私人原文和对话。"""
    __tablename__ = "journey_publications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("student_journeys.id", ondelete="CASCADE"), index=True)
    event_json: Mapped[dict] = mapped_column(JSON)
    created_at_epoch: Mapped[int] = mapped_column(Integer)


class JourneySourceTrust(Base):
    """成员本人对该路线发布来源的持续信任，可随时撤销。"""
    __tablename__ = "journey_source_trust"
    __table_args__ = (UniqueConstraint("journey_id", "user_id", name="uq_journey_source_trust"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    journey_id: Mapped[str] = mapped_column(ForeignKey("student_journeys.id", ondelete="CASCADE"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
