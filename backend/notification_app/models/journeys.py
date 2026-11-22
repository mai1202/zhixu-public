"""办理路线、主动加入的协作成员和用户明确填写的机会偏好。"""

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class StudentJourney(Base):
    """路线附着于既有任务；通知事实仍从原事项读取，不复制私人原文。"""

    __tablename__ = "student_journeys"
    __table_args__ = (UniqueConstraint("task_id", name="uq_student_journeys_task"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    shared_title: Mapped[str | None] = mapped_column(String(200))
    steps_json: Mapped[list] = mapped_column(JSON, nullable=False)
    source_version: Mapped[int | None] = mapped_column(Integer)
    # 邀请码只保存摘要；关闭或轮换后旧码立即失效。
    invite_digest: Mapped[str | None] = mapped_column(String(64), unique=True)
    invite_expires_at_epoch: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class JourneyMember(Base):
    """成员主动使用邀请码加入；身份与来源任务 owner 分开校验。"""

    __tablename__ = "journey_members"
    __table_args__ = (
        UniqueConstraint("journey_id", "user_id", name="uq_journey_members_user"),
        Index("ix_journey_members_user", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    journey_id: Mapped[str] = mapped_column(ForeignKey("student_journeys.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class StudentIntent(Base):
    """兴趣与可用时间由本人填写，缺失资料不被模型推断成报名资格。"""

    __tablename__ = "student_intents"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    profile_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
