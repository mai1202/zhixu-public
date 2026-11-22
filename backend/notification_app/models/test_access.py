"""管理员明确开通的密码测试账号与服务商余额告警，不开放自助注册或改额度。"""
from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class TestAccountAccess(Base):
    __tablename__ = 'test_account_access'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    daily_budget_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class OperationsAlert(Base):
    __tablename__ = 'operations_alerts'
    __table_args__ = (UniqueConstraint('incident_key', name='uq_operations_alert_incident'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    incident_key: Mapped[str] = mapped_column(String(150), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='queued')
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at_epoch: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
