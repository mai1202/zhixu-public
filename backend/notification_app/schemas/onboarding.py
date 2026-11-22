"""引导只修改本人进度，实际业务配置仍走既有服务。"""
from typing import Literal
from uuid import UUID, uuid4
from pydantic import Field
from .business import ContractModel

Section = Literal['model', 'collection', 'attachments', 'tasks', 'courses', 'reminders']


class OnboardingUpdateInput(ContractModel):
    expected_version: int = Field(ge=1)
    action: Literal['save', 'complete', 'snooze', 'resume'] = 'save'
    skipped_sections: list[Section] | None = Field(default=None, max_length=6)
    reminder_enabled: bool | None = None


class OnboardingStartInput(ContractModel):
    goals: str = Field(default='', max_length=4000)
    idempotency_key: UUID = Field(default_factory=uuid4)
