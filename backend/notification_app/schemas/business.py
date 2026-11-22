from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ContractModel(BaseModel):
    """共享业务契约拒绝未定义字段，避免静默吞掉模型或客户端输入。"""

    model_config = ConfigDict(extra="forbid")


class EvidenceRef(ContractModel):
    segment_id: str = Field(min_length=1, max_length=80)
    quote: str = Field(min_length=1, max_length=2000)


class DeadlineCandidate(ContractModel):
    raw: str = Field(min_length=1, max_length=500)
    local_date: date | None
    local_time: str | None = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])?$")
    timezone: str | None = Field(max_length=100)
    precision: Literal["unknown", "year", "month", "day", "minute", "second"]
    utc: datetime | None
    basis: str | None = Field(max_length=1000)

    @model_validator(mode="after")
    def ensure_precision_matches_values(self):
        """截止精度与日期、时刻和 UTC 值必须一致，禁止陈旧时间字段。"""
        if self.precision == "day":
            if self.local_date is None or self.local_time is not None or self.utc is not None:
                raise ValueError("日期精度不能伪造时刻或 UTC 值")
            return self
        if self.precision in {"unknown", "year", "month"}:
            if self.local_date is not None or self.local_time is not None or self.utc is not None:
                raise ValueError("未到日精度的期限只能保留原文和说明")
            return self
        if self.precision in {"minute", "second"}:
            if self.local_date is None or self.local_time is None or self.timezone is None or self.utc is None:
                raise ValueError("时刻精度必须包含日期、时区和 UTC 值")
            parsed_time = time.fromisoformat(self.local_time)
            if self.precision == "minute" and parsed_time.second != 0:
                raise ValueError("分钟精度不能带非零秒数")
            try:
                if self.timezone.startswith(("+", "-")):
                    zone_text = self.timezone.replace(":", "")
                    sign = 1 if zone_text[0] == "+" else -1
                    zone = timezone(sign * timedelta(hours=int(zone_text[1:3]), minutes=int(zone_text[3:5])))
                else:
                    zone = ZoneInfo(self.timezone)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("时区名称无效") from None
            local_value = datetime.combine(self.local_date, parsed_time, tzinfo=zone)
            if self.utc.astimezone(timezone.utc) != local_value.astimezone(timezone.utc):
                raise ValueError("UTC 时刻与本地日期、时刻和时区不一致")
        return self


class TimeRangeCandidate(ContractModel):
    start: datetime
    end: datetime
    timezone: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def ensure_order(self):
        """时段必须有先后顺序且带明确时区。"""
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("时段时间必须带时区")
        if self.end <= self.start:
            raise ValueError("时段结束时间必须晚于开始时间")
        return self


class NoticeCandidate(ContractModel):
    temp_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    source_ids: list[str] = Field(max_length=100)


class EventCandidate(ContractModel):
    temp_id: str = Field(min_length=1, max_length=80)
    notice_temp_id: str | None = Field(max_length=80)
    title: str = Field(min_length=1, max_length=500)
    actor: str | None = Field(max_length=500)
    stage: str | None = Field(max_length=500)
    scope: str | None = Field(max_length=1000)
    deadline: DeadlineCandidate | None
    time_range: TimeRangeCandidate | None
    materials: list[str] = Field(max_length=100)
    submission: str | None = Field(max_length=2000)
    eligibility: Literal["eligible", "ineligible", "unknown", "conditional"]
    evidence: dict[str, list[EvidenceRef]]
    unknowns: list[str] = Field(max_length=100)


class ScheduleEntryCandidate(ContractModel):
    temp_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    kind: Literal["busy_block", "all_day", "deadline_marker"]
    date: date | None
    start: datetime | None
    end: datetime | None
    all_day: bool
    source_event_temp_id: str | None = Field(max_length=80)


class ChangeCandidate(ContractModel):
    temp_id: str = Field(min_length=1, max_length=80)
    target_event_temp_id: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=1000)
    patch: dict = Field(max_length=40)
    evidence: list[EvidenceRef] = Field(max_length=100)


class NormalizedBundle(ContractModel):
    schema_version: Literal["1.0"]
    notices: list[NoticeCandidate] = Field(max_length=100)
    events: list[EventCandidate] = Field(max_length=500)
    schedule_entries: list[ScheduleEntryCandidate] = Field(max_length=500)
    change_candidates: list[ChangeCandidate] = Field(max_length=100)
    warnings: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def ensure_unique_ids(self):
        """临时标识只允许在本候选包内唯一，不能指向任意数据库对象。"""
        identifiers = [
            entry.temp_id
            for group in (self.notices, self.events, self.schedule_entries, self.change_candidates)
            for entry in group
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("候选临时标识必须唯一")
        return self


class ManualEventInput(ContractModel):
    title: str = Field(min_length=1, max_length=500)
    actor: str | None = Field(default=None, max_length=500)
    stage: str | None = Field(default=None, max_length=500)
    scope: str | None = Field(default=None, max_length=1000)
    deadline_date: date | None = None
    deadline_time: str | None = Field(default=None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    # 可信协作来源保留已有时间精度和时区，不重新猜测截止时刻。
    structured_deadline: DeadlineCandidate | None = None
    materials: list[str] = Field(default_factory=list, max_length=100)
    submission: str | None = Field(default=None, max_length=2000)


class CreateSourceInput(ContractModel):
    source_type: Literal["text", "manual"]
    analysis_mode: Literal["rules", "llm", "auto"] = "rules"
    provider_profile_id: str | None = Field(default=None, max_length=36)
    title: str | None = Field(default=None, max_length=500)
    text: str | None = Field(default=None, max_length=100_000)
    manual_event: ManualEventInput | None = None

    @model_validator(mode="after")
    def check_source_payload(self):
        """手工表单和文本输入使用互斥字段，防止产生含义不明的候选。"""
        if self.source_type == "text":
            if not self.text or not self.text.strip() or self.manual_event is not None:
                raise ValueError("文本来源必须填写 text 且不能附带 manual_event")
        elif self.manual_event is None or self.text is not None:
            raise ValueError("手工来源必须填写 manual_event 且不能附带 text")
        if self.source_type == "manual" and (self.analysis_mode != "rules" or self.provider_profile_id is not None):
            raise ValueError("手工录入只使用规则/用户输入路径")
        if self.analysis_mode == "llm" and not self.provider_profile_id:
            raise ValueError("模型模式需要指定用户自己的模型配置")
        return self


class DemoNotificationInput(ContractModel):
    """比赛随机测试入口：输入走真实提取与 Agent 链路，但明确标为测试来源。"""

    title: str | None = Field(default=None, max_length=500)
    text: str = Field(min_length=1, max_length=100_000)
    analysis_mode: Literal["rules", "llm", "auto"] = "auto"
    provider_profile_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def ensure_demo_payload(self):
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("测试通知不能为空")
        if self.analysis_mode == "llm" and not self.provider_profile_id:
            raise ValueError("模型模式需要指定模型配置")
        return self


class ChangeSetUpdateInput(ContractModel):
    expected_version: int = Field(ge=1)
    bundle: NormalizedBundle


class VersionInput(ContractModel):
    expected_version: int = Field(ge=1)


class TaskStatusInput(ContractModel):
    expected_version: int = Field(ge=1)


class NoticePatchInput(ContractModel):
    """通知标题、重要性和信息完整性的受控修正。"""

    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    importance: Literal["low", "normal", "high", "critical"] | None = None


class EventPatchInput(ContractModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    actor: str | None = Field(default=None, max_length=500)
    stage: str | None = Field(default=None, max_length=500)
    scope: str | None = Field(default=None, max_length=1000)
    deadline: DeadlineCandidate | None = None
    time_range: TimeRangeCandidate | None = None
    materials: list[str] | None = Field(default=None, max_length=100)
    submission: str | None = Field(default=None, max_length=2000)
    eligibility: Literal["eligible", "ineligible", "unknown", "conditional"] | None = None
    evidence: dict[str, list[EvidenceRef]] | None = None
    unknowns: list[str] | None = Field(default=None, max_length=100)
