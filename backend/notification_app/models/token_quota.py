"""逐模型调用账本；平台密钥受人民币额度限制，自有云端密钥只记录费用。"""

from sqlalchemy import ForeignKey, Integer, String, Index, BigInteger, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class PlatformTokenDay(Base):
    __tablename__ = "platform_token_days"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 金额以百万分之一人民币存储，避免浮点累计导致额度边界误差。
    spent_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    held_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class PlatformTokenReservation(Base):
    __tablename__ = "platform_token_reservations"
    __table_args__ = (Index("ix_platform_tokens_owner_day", "user_id", "day"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    charged: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    # 绑定后台运行与配置，能力凭证不能拿去结算其他账户或运行的调用。
    run_id: Mapped[str | None] = mapped_column(String(36))
    profile_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reserved_micros: Mapped[int | None] = mapped_column(BigInteger)
    charged_micros: Mapped[int | None] = mapped_column(BigInteger)
    # 单价及原始用量按请求保存快照，后续改配置不会改写历史账单。
    pricing_json: Mapped[dict | None] = mapped_column(JSON)
    usage_json: Mapped[dict | None] = mapped_column(JSON)
