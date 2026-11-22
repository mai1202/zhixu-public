"""单个模型配置的资料处理同意，不由助手替用户勾选。"""
from pydantic import Field
from .business import ContractModel


class ModelDataConsentInput(ContractModel):
    expected_version: int = Field(strict=True, ge=1)
    enabled: bool = Field(strict=True)
