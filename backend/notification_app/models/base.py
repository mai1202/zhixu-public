from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 模型的共同基类。"""


class AppMetadata(Base):
    """保存可部署实例的基础版本信息，不含任何用户业务资料。"""

    __tablename__ = "app_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    foundation_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
