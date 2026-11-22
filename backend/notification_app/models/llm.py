from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ProviderProfile(Base):
    """用户私有模型配置；密钥只存储认证加密后的密文。"""

    __tablename__ = "provider_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "label", name="uq_provider_profiles_owner_label"),
        Index("ix_provider_profiles_owner_task", "user_id", "purpose", "enabled"),
        Index("ix_provider_profiles_default", "user_id", "is_default"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    api_family: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    execution_target: Mapped[str] = mapped_column(String(16), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(36))
    # 服务端统一配置的默认模型；用户不可改写或删除，但仍可测试。
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 自配云服务允许填写人民币参考单价，平台默认配置不采用用户单价。
    billing_prices_json: Mapped[dict | None] = mapped_column(JSON)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    fallback_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="rules")
    last_test_status: Mapped[str] = mapped_column(String(24), nullable=False, default="untested")
    last_test_epoch: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)


class LLMRun(Base):
    """模型调用审计只保留哈希、状态、用量和耗时，不记录提示词或原文。"""

    __tablename__ = "llm_runs"
    __table_args__ = (
        Index("ix_llm_runs_owner_created", "user_id", "created_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider_profile_id: Mapped[str | None] = mapped_column(ForeignKey("provider_profiles.id", ondelete="SET NULL"))
    task_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 8))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
