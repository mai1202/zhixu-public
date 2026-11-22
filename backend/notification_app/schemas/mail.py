from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, StringConstraints, model_validator

from .business import ContractModel

ClockTime = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class DigestContentOptions(ContractModel):
    """日报各事实栏目可单独启用，默认都显示。"""

    courses: bool = True
    arrangements: bool = True
    deadlines: bool = True
    changes: bool = True
    tomorrow: bool = True
    agent_summary: bool = True
    suggestions: bool = True


class QuietHoursInput(ContractModel):
    """安静时段只抑制非紧急提醒，不阻断课程和截止变更。"""

    enabled: bool = False
    start: ClockTime = "22:00"
    end: ClockTime = "07:00"


class DigestPreferenceInput(ContractModel):
    """保存经确认的日报设置；收件地址必须属于当前用户且已验证。"""

    expected_version: int = Field(ge=1)
    enabled: bool
    timezone: str = Field(min_length=1, max_length=100)
    send_time: ClockTime
    recipient_address_id: UUID | None = None
    include: DigestContentOptions = Field(default_factory=DigestContentOptions)
    # 新字段保持可选，旧前端只提交原有字段时不会重置用户新增偏好。
    weekly_enabled: bool | None = None
    weekly_send_day: int | None = Field(default=None, ge=1, le=7)
    weekly_send_time: ClockTime | None = None
    reminder_enabled: bool | None = None
    reminder_offsets: list[int] | None = Field(default=None, max_length=8)
    notification_flags: dict[str, bool] | None = None
    quiet_hours: QuietHoursInput | None = None
    merge_window_minutes: int | None = Field(default=None, ge=0, le=240)

    @model_validator(mode="after")
    def validate_preferences(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("时区名称无效") from None
        if self.enabled and self.recipient_address_id is None:
            raise ValueError("启用日报前必须选择已验证收件地址")
        if self.weekly_enabled and self.recipient_address_id is None:
            raise ValueError("启用周报前必须选择已验证收件地址")
        if self.reminder_enabled and self.recipient_address_id is None:
            raise ValueError("启用主动提醒前必须选择已验证收件地址")
        if self.reminder_offsets is not None:
            cleaned = sorted({value for value in self.reminder_offsets if value >= 0}, reverse=True)
            if len(cleaned) != len(self.reminder_offsets) or any(value > 20160 for value in cleaned):
                raise ValueError("提醒提前量必须是唯一非负整数，且不超过 14 天")
        if self.notification_flags is not None:
            allowed = {
                "task_reminder", "course_reminder", "important_notice",
                "course_change", "conflict_warning",
            }
            if set(self.notification_flags) - allowed:
                raise ValueError("通知提醒开关包含未知项目")
        if self.quiet_hours and self.quiet_hours.enabled and self.quiet_hours.start == self.quiet_hours.end:
            raise ValueError("安静时段的开始和结束不能相同")
        return self
