"""保存用户主动确认的告知版本，历史账户不伪造同意记录。"""
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class UserPrivacyConsent(Base):
    __tablename__ = "user_privacy_consents"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    policy_version: Mapped[str] = mapped_column(String(40))
    accepted_at_epoch: Mapped[int] = mapped_column(Integer)
