"""知识库公开范围和版本必须由服务端校验。"""
from datetime import date
from typing import Literal
from uuid import UUID, uuid4
from pydantic import Field, model_validator
from .business import ContractModel


class KnowledgeInput(ContractModel):
    idempotency_key: UUID = Field(default_factory=uuid4)
    scope: Literal['private','public'] = 'private'
    title: str = Field(min_length=1,max_length=500)
    content: str = Field(min_length=1,max_length=500000)
    source_url: str | None = Field(default=None,max_length=2000)
    # 公共规定的最小可追溯字段；私人笔记可留空。
    publisher: str | None = Field(default=None, max_length=300)
    published_date: date | None = None
    effective_status: Literal['effective', 'expired', 'unknown'] = 'unknown'
    audience: str | None = Field(default=None, max_length=300)
    supersedes_id: UUID | None = None

    @model_validator(mode='after')
    def nonempty(self):
        """空白不是有效资料，避免写入无法检索的空记录。"""
        if not self.title.strip() or not self.content.strip():
            raise ValueError('标题和内容不能为空')
        return self


class KnowledgeStateInput(ContractModel):
    expected_version: int = Field(ge=1)
    status: Literal['active','archived']


class SchoolPageInput(ContractModel):
    url: str = Field(min_length=1,max_length=2000)
    scope: Literal['private','public'] = 'private'


class CitationVerifyInput(ContractModel):
    """引用校验只接受定位字段和短引文，不要求上传完整资料。"""
    kind: Literal['document','notification','attachment']
    id: str = Field(min_length=1, max_length=64)
    quote: str = Field(min_length=2, max_length=2000)
