"""用户确认时可补充标题和日期，不能由网页伪造来源归属。"""
from pydantic import Field
from .business import ContractModel, ManualEventInput


class SuggestionConfirmInput(ContractModel):
    expected_version: int = Field(strict=True, ge=1)
    events: list[ManualEventInput] = Field(min_length=1, max_length=20)


class SuggestionDismissInput(ContractModel):
    expected_version: int = Field(strict=True, ge=1)
