from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from .business import ContractModel

# Pi SDK 支持的通用档位；模型不支持时会在运行时自动降级。
ThinkingLevel = Literal["off", "low", "medium", "high", "max"]


class AgentTurnInput(ContractModel):
    """对话请求只带用户文字、会话、可选用户模型配置和幂等键。"""

    conversation_id: UUID | None = None
    idempotency_key: UUID = Field(default_factory=uuid4)
    profile_id: UUID | None = None
    thinking_level: ThinkingLevel | None = None
    message: str = Field(min_length=1, max_length=32000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=10)
    page_context: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def trim_message(self):
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("对话内容不能为空")
        return self


class AgentIntent(ContractModel):
    """有限工具循环可执行的意图；模型不能提交数据库 ID 或权限参数。"""

    action: Literal[
        "agenda",
        "free_slots",
        "create_schedule",
        "update_schedule",
        "archive_schedule",
        "restore_schedule",
        "clarify",
        "reply",
        "read_attachment",
        "search_history",
    ]
    start_date: date | None = None
    end_date: date | None = None
    title: str | None = Field(default=None, max_length=500)
    target_title: str | None = Field(default=None, max_length=500)
    kind: Literal["busy_block", "all_day", "deadline_marker"] | None = None
    local_date: date | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    clarification: str | None = Field(default=None, max_length=32000)
    attachment_id: UUID | None = None
    offset: int = Field(default=0, ge=0)
    query: str | None = Field(default=None, max_length=300)


    @model_validator(mode="after")
    def validate_intent(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("时区名称无效") from None
        if self.action in {"create_schedule", "update_schedule"}:
            if not self.title or not self.title.strip() or self.local_date is None or self.kind is None:
                raise ValueError("日程变更需要标题、类型和日期")
            if self.kind == "busy_block":
                if (
                    self.start_at is None
                    or self.end_at is None
                    or self.start_at.tzinfo is None
                    or self.end_at.tzinfo is None
                    or self.end_at <= self.start_at
                ):
                    raise ValueError("忙碌日程需要有效的时区开始和结束时间")
            elif self.start_at is not None or self.end_at is not None:
                raise ValueError("全天或截止标记不能包含时间段")
        if self.action == "update_schedule" and not (self.target_title and self.target_title.strip()):
            raise ValueError("更改日程需要指出现有标题")
        if self.action in {"archive_schedule", "restore_schedule"} and not (
            self.target_title and self.target_title.strip()
        ):
            raise ValueError("归档或恢复日程需要指出现有标题")
        if self.start_date is not None and self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("查询结束日期早于开始日期")
        if self.start_date and self.end_date and (self.end_date - self.start_date).days > 62:
            raise ValueError("单次查询最多覆盖 63 天")
        return self


class AgentPreferenceInput(ContractModel):
    """自动处理仅针对新微信通知，模型与容量可由用户自行调整。"""

    profile_id: UUID | None = None
    automatic_notifications: bool = True
    context_budget: int = Field(default=64000, ge=16000, le=1000000)
    thinking_level: ThinkingLevel = "medium"


class AgentConversationActionInput(ContractModel):
    """归档或恢复对话时携带页面看到的版本，防止覆盖并发变化。"""

    expected_version: int = Field(ge=1)


class AgentRetryInput(ContractModel):
    """失败运行重试时可换成另一个已启用的规划模型。"""

    profile_id: UUID | None = None
    thinking_level: ThinkingLevel | None = None


class AgentMemoryInput(ContractModel):
    """用户手动补充长期记忆；正文长度与自动记忆保持一致。"""

    content: str = Field(min_length=1, max_length=600)
    category: Literal["preference", "constraint", "routine", "goal", "other"] = "other"

