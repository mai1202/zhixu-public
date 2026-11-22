from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import Field, model_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .business import ContractModel


class PeriodGroupInput(ContractModel):
    periods: list[int] = Field(min_length=1, max_length=10)
    start_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    source_label: str = Field(default="用户确认的作息时段", max_length=300)

    @model_validator(mode="after")
    def ensure_periods_and_times(self):
        """小节编号不能重复，时段两端必须同时存在且顺序有效。"""
        if len(self.periods) != len(set(self.periods)) or any(item < 1 or item > 30 for item in self.periods):
            raise ValueError("小节编号范围或重复项无效")
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("开始与结束时刻必须同时填写")
        if self.start_time is not None and self.start_time >= self.end_time:
            raise ValueError("结束时刻必须晚于开始时刻")
        return self


class PreviewTimetableInput(ContractModel):
    week1_monday: date | None = None
    week_count: int = Field(default=16, ge=1, le=30)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    term_code: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def ensure_preview_timezone(self):
        """预览可以暂缺首周锚点，但时区必须有效。"""
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("时区名称无效") from None
        if self.week1_monday and self.week1_monday.weekday() != 0:
            raise ValueError("学期第 1 周日期必须是星期一")
        return self


class CourseTimeFields(ContractModel):
    """实际开始和结束必须成对提供，空值表示按节次表生成。"""

    start_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def ensure_course_times(self):
        if bool(self.start_time) != bool(self.end_time):
            raise ValueError("课程开始时间和结束时间必须一起填写")
        if self.start_time and self.end_time <= self.start_time:
            raise ValueError("课程结束时间必须晚于开始时间")
        return self


class CourseOverrideInput(CourseTimeFields):
    source_cell: str = Field(min_length=1, max_length=20)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    display_name: str | None = Field(default=None, min_length=1, max_length=500)
    teachers: list[str] | None = Field(default=None, max_length=50)
    room: str | None = Field(default=None, max_length=500)
    week_expression: str | None = Field(default=None, max_length=200)


class ConfirmTimetableInput(ContractModel):
    expected_version: int = Field(ge=1)
    week1_monday: date
    week_count: int = Field(ge=1, le=30)
    timezone: str = Field(min_length=1, max_length=100)
    anchor_confirmed: bool
    apply_mode: Literal["merge", "replace_term"]
    period_profile: list[PeriodGroupInput] = Field(min_length=1, max_length=30)
    course_overrides: list[CourseOverrideInput] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def ensure_schedule_config(self):
        """首周必须是周一，且确认前必须显式检查锚点和作息表。"""
        if self.week1_monday.weekday() != 0:
            raise ValueError("学期第 1 周日期必须是星期一")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("时区名称无效") from None
        return self


class ScheduleEntryInput(ContractModel):
    title: str = Field(min_length=1, max_length=500)
    kind: Literal["busy_block", "all_day", "deadline_marker"]
    local_date: date
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str = Field(min_length=1, max_length=100)
    source_event_id: str | None = Field(default=None, max_length=36)
    allow_conflict: bool = False

    @model_validator(mode="after")
    def ensure_schedule_entry(self):
        """忙碌时段需有带时区的半开区间；日期标记不可伪造占用时间。"""
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("时区名称无效") from None
        if self.kind == "busy_block":
            if self.start_at is None or self.end_at is None:
                raise ValueError("忙碌时段必须填写开始和结束时间")
            if self.start_at.tzinfo is None or self.end_at.tzinfo is None or self.end_at <= self.start_at:
                raise ValueError("时段必须带时区且结束晚于开始")
            actual_duration = self.end_at.astimezone(timezone.utc) - self.start_at.astimezone(timezone.utc)
            if actual_duration > timedelta(days=7):
                raise ValueError("单个忙碌时段最多跨越 7 天")
        elif self.start_at is not None or self.end_at is not None:
            raise ValueError("全天和截止标记不能填写时刻区间")
        return self


class UpdateScheduleEntryInput(ScheduleEntryInput):
    expected_version: int = Field(ge=1)


class CourseExceptionInput(ContractModel):
    expected_version: int = Field(ge=1)
    kind: Literal["cancel", "reschedule", "room_change"]
    reason: str = Field(min_length=1, max_length=500)
    replacement_start: datetime | None = None
    replacement_end: datetime | None = None
    replacement_room: str | None = Field(default=None, max_length=500)
    notice_id: str | None = Field(default=None, max_length=36)
    evidence: list[dict] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def ensure_exception_range(self):
        """改期需提供完整的时区时间范围，取消不接收时间字段。"""
        if self.kind == "reschedule":
            if self.replacement_start is None or self.replacement_end is None:
                raise ValueError("改期例外必须填写新的开始和结束时刻")
            if (
                self.replacement_start.tzinfo is None
                or self.replacement_end.tzinfo is None
                or self.replacement_end <= self.replacement_start
            ):
                raise ValueError("改期时段必须带时区且结束晚于开始")
        elif self.replacement_start is not None or self.replacement_end is not None:
            raise ValueError("取消例外不能填写改期时刻")
        if self.kind == "room_change" and not self.replacement_room:
            raise ValueError("地点变更必须填写新地点")
        if self.kind == "cancel" and self.replacement_room:
            raise ValueError("取消课程不能填写新地点")
        return self


class CourseMakeupInput(ContractModel):
    """临时补课创建为独立课程实例，不修改常规课程系列。"""

    local_date: date
    start_at: datetime
    end_at: datetime
    room: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    notice_id: str | None = Field(default=None, max_length=36)
    evidence: list[dict] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def ensure_makeup_time(self):
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None or self.end_at <= self.start_at:
            raise ValueError("补课时段必须带时区且结束晚于开始")
        if self.start_at.date() != self.local_date:
            raise ValueError("补课日期与开始时间不一致")
        return self


class CourseSeriesFields(CourseTimeFields):
    """手动维护常规课程系列时的可编辑字段；单次变化仍使用课程例外。"""

    title: str = Field(min_length=1, max_length=500)
    display_name: str | None = Field(default=None, min_length=1, max_length=500)
    teachers: list[str] = Field(default_factory=list, max_length=50)
    room: str | None = Field(default=None, max_length=500)
    weekday: int = Field(ge=1, le=7)
    periods: list[int] = Field(min_length=1, max_length=10)
    week_numbers: list[int] = Field(min_length=1, max_length=30)
    raw_text: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def ensure_manual_course_fields(self):
        """标题、节次和周次必须可物化为具体课程实例。"""
        if not self.title.strip():
            raise ValueError("课程名称不能为空")
        if self.display_name is not None and not self.display_name.strip():
            raise ValueError("课表显示名称不能为空")
        if len(self.periods) != len(set(self.periods)) or any(
            item < 1 or item > 30 for item in self.periods
        ):
            raise ValueError("课程节次范围或重复项无效")
        if len(self.week_numbers) != len(set(self.week_numbers)) or any(
            item < 1 or item > 30 for item in self.week_numbers
        ):
            raise ValueError("教学周范围或重复项无效")
        # 过滤空姓名，同时保留用户明确填写的其他教师顺序。
        cleaned_teachers = [item.strip()[:200] for item in self.teachers if item.strip()]
        if len(cleaned_teachers) > 50:
            raise ValueError("课程教师数量过多")
        self.teachers = cleaned_teachers
        self.title = self.title.strip()
        self.display_name = self.display_name.strip() if self.display_name else self.title
        if self.room is not None:
            self.room = self.room.strip() or None
        return self


class CourseSeriesCreateInput(CourseSeriesFields):
    """未提供学期时由服务层选择当前教学周所在的最近学期。"""

    term_id: str | None = Field(default=None, max_length=36)


class CourseSeriesUpdateInput(CourseSeriesFields):
    """修改常规课程系列必须携带当前版本，防止覆盖课表导入后的新状态。"""

    expected_version: int = Field(ge=1)
