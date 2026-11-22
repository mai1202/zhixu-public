"""要求更新与规划使用独立契约，所有外部写入拒绝未知字段。"""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from .business import ContractModel, DeadlineCandidate, EvidenceRef, TimeRangeCandidate
from .journeys import AvailabilityInput


class IdentityInput(ContractModel):
    activity: str | None = Field(default=None, max_length=500)
    edition: str | None = Field(default=None, max_length=100)
    actor: str | None = Field(default=None, max_length=500)
    stage: str | None = Field(default=None, max_length=500)
    scope: str | None = Field(default=None, max_length=1000)


class ClaimInput(ContractModel):
    field: Literal["deadline", "time_range", "material", "submission", "location", "cancelled"]
    value: str | bool | dict | None
    relation: Literal["duplicate", "supplement", "replace", "retract", "conflict"]
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=20)
    supersedes_id: UUID | None = None
    material_id: UUID | None = None

    @model_validator(mode="after")
    def check_value(self):
        """日期和时段复用现有精度校验，不让模型随意写入结构化值。"""
        if self.field in {"deadline", "time_range"} and self.value is None and self.relation != "retract":
            raise ValueError("空日期或时段只能作为明确撤回，不能假装完整新要求")
        if self.field == "deadline" and self.value is not None:
            self.value = DeadlineCandidate.model_validate(self.value).model_dump(mode="json")
        elif self.field == "time_range" and self.value is not None:
            self.value = TimeRangeCandidate.model_validate(self.value).model_dump(mode="json")
        elif self.field == "cancelled":
            if not isinstance(self.value, bool):
                raise ValueError("取消状态必须是布尔值")
        elif self.field in {"submission", "location"} and self.value is None and self.relation == "retract":
            return self
        elif self.field in {"material", "submission", "location"}:
            if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 2000:
                raise ValueError("要求文本不能为空且最多2000字")
        return self


class RequirementUpdateInput(ContractModel):
    source_id: UUID
    event_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    identity: IdentityInput
    claims: list[ClaimInput] = Field(min_length=1, max_length=40)
    idempotency_key: UUID
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_fields(self):
        """同一提案不能让后面的主张静默覆盖前面的同字段主张。"""
        keys = [(c.field, str(c.material_id or c.value)) if c.field == "material" else (c.field, "") for c in self.claims]
        if len(keys) != len(set(keys)):
            raise ValueError("同一提案包含重复字段，请拆分或先解决冲突")
        return self


class ApplyInput(ContractModel):
    expected_version: int = Field(ge=1)


class FieldPolicyInput(ApplyInput):
    field: Literal["deadline", "time_range", "materials", "submission", "location", "cancelled"]
    follow_source: bool


class PlanningPreferenceInput(ApplyInput):
    automatic: bool
    availability: AvailabilityInput
    journey_ids: list[UUID] = Field(default_factory=list, max_length=10)


class SchedulePolicyInput(ApplyInput):
    movable: bool
    locked: bool = False
    earliest_date: date | None = None
    latest_date: date | None = None

    @model_validator(mode="after")
    def ordered_dates(self):
        """用户未给范围时服务端限定为原日期。"""
        if self.earliest_date and self.latest_date and self.latest_date < self.earliest_date:
            raise ValueError("允许日期范围倒置")
        return self


class PlanningPreviewInput(ContractModel):
    journey_ids: list[UUID] = Field(default_factory=list, max_length=10)
    entry_ids: list[UUID] = Field(default_factory=list, max_length=100)
    availability: AvailabilityInput | None = None

    @model_validator(mode="after")
    def selection_required(self):
        """空选择不生成看似成功的空方案。"""
        if not self.journey_ids and not self.entry_ids:
            raise ValueError("请至少选择一条办理路线或一项个人安排")
        return self


class PlanningApplyInput(ContractModel):
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    allow_partial: bool = False
