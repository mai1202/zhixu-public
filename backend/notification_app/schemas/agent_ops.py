"""Agent 跨模块工具与主动服务使用的输入契约。"""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .business import ContractModel, DeadlineCandidate, EvidenceRef
from .timetable import CourseExceptionInput


TaskKind = Literal["assignment", "exam", "preparation", "activity", "course", "personal"]
TaskPriority = Literal["low", "normal", "high", "urgent"]


class TaskCreateInput(ContractModel):
    """Agent 创建普通任务时只接受结构化字段，不接受任意 SQL 或权限参数。"""

    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    task_kind: TaskKind = "assignment"
    priority: TaskPriority = "normal"
    deadline: DeadlineCandidate | None = None
    original_requirement: str | None = Field(default=None, max_length=1000)
    notice_id: UUID | None = None
    course_series_id: UUID | None = None
    course_occurrence_id: UUID | None = None
    reminder_enabled: bool = True


class TaskPatchInput(ContractModel):
    """任务更新必须带页面或工具读取到的版本，阻止旧上下文覆盖人工修改。"""

    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    task_kind: TaskKind | None = None
    priority: TaskPriority | None = None
    deadline: DeadlineCandidate | None = None
    original_requirement: str | None = Field(default=None, max_length=1000)
    course_series_id: UUID | None = None
    course_occurrence_id: UUID | None = None
    reminder_enabled: bool | None = None


class LinkInput(ContractModel):
    """关系两端必须是当前账户内已存在的实体，由服务层再次核验。"""

    from_type: Literal["notice", "task", "course", "course_occurrence"]
    from_id: UUID
    to_type: Literal["notice", "task", "course", "course_occurrence"]
    to_id: UUID
    relation: str = Field(default="related", min_length=1, max_length=32)
    source_document_id: UUID | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def ensure_distinct_endpoints(self):
        if self.from_type == self.to_type and self.from_id == self.to_id:
            raise ValueError("不能把对象关联到自身")
        return self


class ReportGenerateInput(ContractModel):
    """手动生成报告；发送必须再经过用户显式请求。"""

    kind: Literal["daily", "weekly"]
    period_start: date | None = None
    send: bool = False


class ManualReminderInput(ContractModel):
    """用户或 Agent 为明确任务设置的额外提醒。"""

    task_id: UUID
    scheduled_for: date
    note: str | None = Field(default=None, max_length=500)


class NotificationCourseChange(ContractModel):
    """自动通知中的单次课程调整，必须指向确定实例并保留证据。"""

    occurrence_id: UUID
    change: CourseExceptionInput
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=20)


class NotificationTaskChange(ContractModel):
    """自动通知中的任务动作；create/update/complete 各自使用独立契约。"""

    action: Literal["create", "update", "complete", "reopen"]
    task_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    create: TaskCreateInput | None = None
    patch: TaskPatchInput | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def ensure_action_payload(self):
        if self.action == "create":
            if self.create is None or self.task_id is not None:
                raise ValueError("创建任务需要 create 且不能同时提供 task_id")
        elif self.action in {"update", "complete", "reopen"}:
            if self.task_id is None or self.expected_version is None:
                raise ValueError("已有任务操作需要 task_id 和 expected_version")
        if self.action == "update" and self.patch is None:
            raise ValueError("更新任务需要 patch")
        if self.action != "update" and self.patch is not None:
            raise ValueError("仅更新任务可以包含 patch")
        return self


class NotificationChangePlan(ContractModel):
    """一次通知产生的跨模块写入；服务层按顺序执行，失败时补偿已执行部分。"""

    summary: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=900)
    course_changes: list[NotificationCourseChange] = Field(default_factory=list, max_length=20)
    task_changes: list[NotificationTaskChange] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def ensure_has_change(self):
        if not self.course_changes and not self.task_changes:
            raise ValueError("跨模块计划至少需要一项课程或任务变更")
        return self
