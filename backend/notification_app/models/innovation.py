"""字段证据、核对请求与有明确授权的规划记录。"""

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RequirementClaim(Base):
    """值和证据不覆盖；决策状态随变更集更新，历史始终可查。"""

    __tablename__ = "requirement_claims"
    __table_args__ = (Index("ix_claims_owner_event", "user_id", "event_id"),
        UniqueConstraint("user_id", "fingerprint", name="uq_claims_owner_fingerprint"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id", ondelete="RESTRICT"))
    change_set_id: Mapped[str | None] = mapped_column(ForeignKey("change_sets.id", ondelete="SET NULL"))
    field_key: Mapped[str] = mapped_column(String(120))
    value_json: Mapped[dict] = mapped_column(JSON)
    evidence_json: Mapped[list] = mapped_column(JSON)
    relation: Mapped[str] = mapped_column(String(24))
    # 不建立自引用外键，隐私删除按 owner 拓扑排序时不会形成表依赖环。
    supersedes_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(24))
    fingerprint: Mapped[str] = mapped_column(String(64))
    event_version: Mapped[int] = mapped_column(Integer)
    created_at_epoch: Mapped[int] = mapped_column(Integer)


class UserPlanningState(Base):
    """账户级写入序列与明确授权；版本同时用于过期方案检查。"""

    __tablename__ = "user_planning_states"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)
    availability_json: Mapped[dict] = mapped_column(JSON, default=dict)
    journey_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    changed_at_epoch: Mapped[int] = mapped_column(Integer, default=0)


class PlanningRun(Base):
    """持久化试算、输入指纹和实际应用回执，响应丢失后可重读。"""

    __tablename__ = "planning_runs"
    __table_args__ = (Index("ix_planning_runs_owner_created", "user_id", "created_at_epoch"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    input_json: Mapped[dict] = mapped_column(JSON)
    result_json: Mapped[dict] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    change_set_id: Mapped[str | None] = mapped_column(ForeignKey("change_sets.id", ondelete="SET NULL"))
    created_at_epoch: Mapped[int] = mapped_column(Integer)
    applied_at_epoch: Mapped[int | None] = mapped_column(Integer)
