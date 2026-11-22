"""办理步骤与时间试算的有界输入，拒绝依赖环、无时区时间和隐式资格推断。"""

from datetime import time
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from .business import ContractModel


class JourneyStepInput(ContractModel):
    id: UUID
    title: str = Field(min_length=1, max_length=300)
    depends_on: list[UUID] = Field(default_factory=list, max_length=40)
    estimate_minutes: int | None = Field(default=None, strict=True, ge=5, le=1440)
    assigned_user_id: UUID | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value):
        """空白文本不构成有效办理步骤。"""
        if not value.strip():
            raise ValueError("步骤标题不能为空")
        return value.strip()


class JourneySaveInput(ContractModel):
    expected_version: int = Field(ge=1)
    steps: list[JourneyStepInput] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def ensure_dag(self):
        """依赖必须存在且无环；输入顺序不替代依赖关系。"""
        graph = {str(step.id): [str(item) for item in step.depends_on] for step in self.steps}
        if len(graph) != len(self.steps):
            raise ValueError("步骤标识不能重复")
        visited, visiting = set(), set()

        def visit(key):
            if key not in graph or key in visiting:
                raise ValueError("步骤依赖不存在或形成循环")
            if key in visited:
                return
            visiting.add(key)
            for parent in graph[key]:
                visit(parent)
            visiting.remove(key)
            visited.add(key)

        for key in graph:
            visit(key)
        return self


class JourneyVersionInput(ContractModel):
    expected_version: int = Field(ge=1)


class JourneyStepActionInput(JourneyVersionInput):
    action: Literal["complete", "reopen"]


class JourneyInviteInput(JourneyVersionInput):
    # 单独填写分享标题，避免自动把私人通知标题带给协作成员。
    shared_title: str = Field(min_length=1, max_length=200)
    share_steps: Literal[True]

    @field_validator("shared_title")
    @classmethod
    def meaningful_title(cls, value):
        """共享标题需要明确填写，不能以空白绕过分享范围说明。"""
        if not value.strip():
            raise ValueError("请填写分享标题")
        return value.strip()


class JourneyJoinInput(ContractModel):
    code: str = Field(min_length=32, max_length=100)
    alias: str = Field(min_length=1, max_length=40)

    @field_validator("alias")
    @classmethod
    def meaningful_alias(cls, value):
        """昵称只用于协作显示，不暴露账户邮箱。"""
        if not value.strip():
            raise ValueError("请填写成员昵称")
        return value.strip()


class AvailabilityInput(ContractModel):
    timezone: str = Field(default="Asia/Shanghai", max_length=100)
    start_time: str = Field(default="18:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(default="22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[Annotated[int, Field(strict=True, ge=1, le=7)]] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7], min_length=1, max_length=7)

    @model_validator(mode="after")
    def validate_window(self):
        """首版采用同日时间窗，明确拒绝跨午夜与非法时区。"""
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("时区无效") from None
        if time.fromisoformat(self.end_time) <= time.fromisoformat(self.start_time):
            raise ValueError("可用时间结束必须晚于开始")
        if any(isinstance(day, bool) or day < 1 or day > 7 for day in self.weekdays):
            raise ValueError("星期必须为 1 至 7")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("星期不能重复")
        return self


class JourneyPlanInput(JourneyVersionInput):
    availability: AvailabilityInput
    plan_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class StudentIntentInput(ContractModel):
    expected_version: int = Field(ge=0)
    interests: list[str] = Field(default_factory=list, max_length=20)
    facts: str = Field(default="", max_length=1000)
    availability: AvailabilityInput = Field(default_factory=AvailabilityInput)

    @field_validator("interests")
    @classmethod
    def clean_interests(cls, values):
        """只保留本人明确输入的短关键词，不从私聊提取个人画像。"""
        if any(not value.strip() or len(value.strip()) > 40 for value in values):
            raise ValueError("兴趣关键词需为 1 至 40 字")
        return list(dict.fromkeys(value.strip() for value in values))
