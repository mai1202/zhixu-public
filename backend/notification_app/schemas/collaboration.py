"""协作输入沿用统一契约，不接受替别人授权或隐式扩大文件范围。"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from .business import ContractModel
from .journeys import AvailabilityInput


class CommentInput(ContractModel):
    content: str = Field(min_length=1, max_length=2000)


class SharedFileInput(ContractModel):
    attachment_id: UUID
    audience: Literal["owner", "members"] = "owner"


class AvailabilityConsentInput(ContractModel):
    expected_version: int = Field(strict=True, ge=0)
    enabled: bool = Field(strict=True)
    availability: AvailabilityInput = Field(default_factory=AvailabilityInput)
    valid_days: int = Field(default=7, strict=True, ge=1, le=7)


class SourceTrustInput(ContractModel):
    enabled: bool = Field(strict=True)


class PublicationInput(ContractModel):
    expected_version: int = Field(strict=True, ge=1)
    share_details: Literal[True]
    expected_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
