"""通知整理不要求日期，计划执行时间必须有明确时区。"""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4
from pydantic import Field, model_validator
from .business import ContractModel


class NotificationSummaryEvidence(ContractModel):
    """摘要事实须能回到组内原文，不能用另一个无关来源冒充依据。"""
    source_id: UUID
    quote: str = Field(min_length=2, max_length=500)


class OrganizationInput(ContractModel):
    # 字数是服务端契约，防止模型把逐条复述保存成列表摘要。
    title: str = Field(min_length=1, max_length=60)
    summary: str = Field(min_length=1, max_length=240)
    advice: str = Field(default='', max_length=240)
    expected_version: int | None = Field(default=None, ge=1)
    evidence: list[NotificationSummaryEvidence] = Field(default_factory=list, max_length=12)


class NotificationGroupEvidence(ContractModel):
    """每个成员以真实原文摘录说明其与同一具体事项的关系。"""
    source_id: UUID
    quote: str = Field(min_length=2, max_length=500)
    relation: Literal['duplicate', 'supplement', 'correction']


class NotificationGroupInput(ContractModel):
    source_ids: list[UUID] = Field(min_length=2, max_length=20)
    expected_versions: dict[str, int]
    evidence: list[NotificationGroupEvidence] = Field(min_length=2, max_length=20)
    reason: str = Field(min_length=5, max_length=500)


class MailboxInput(ContractModel):
    mailbox: Literal['inbox', 'archived', 'ignored'] | None = None
    read: bool | None = None
    expected_version: int | None = Field(default=None, ge=1)


class AgentScheduleStatusInput(ContractModel):
    expected_version: int = Field(ge=1)
    action: Literal['pause', 'resume', 'cancel']


class AgentScheduleInput(ContractModel):
    idempotency_key: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=150)
    message: str = Field(min_length=1, max_length=16000)
    mode: Literal['main', 'child'] = 'main'
    run_at: datetime
    interval_seconds: int | None = Field(default=None, ge=900, le=366 * 86400)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=10)
    watch_query: str | None = Field(default=None,min_length=1,max_length=300)
    watch_scope: Literal['all','private','public'] = 'all'
    watch_url: str | None = Field(default=None, max_length=2000)

    @model_validator(mode='after')
    def validate_schedule(self):
        """拒绝含糊时间与空任务，避免后台无意义地持续唤醒。"""
        if self.run_at.tzinfo is None or not self.title.strip() or not self.message.strip():
            raise ValueError('请提供带时区的执行时间及任务内容')
        if self.watch_query and (not self.watch_query.strip() or not self.interval_seconds):
            raise ValueError('持续关注需要非空关键词和重复周期')
        if self.watch_url and not self.watch_query:
            raise ValueError('官网关注还需要一个用于比较内容的关键词')
        if self.watch_url and not self.watch_url.startswith('https://'):
            raise ValueError('官网关注只接受 HTTPS 地址')
        return self
