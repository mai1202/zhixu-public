from typing import Literal
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from .business import ContractModel


class BillingPricesInput(ContractModel):
    """每百万 token 的人民币单价，限制精度且拒绝无穷与负数。"""
    cache_hit: Decimal = Field(ge=0, le=100000, max_digits=12, decimal_places=6)
    cache_miss: Decimal = Field(ge=0, le=100000, max_digits=12, decimal_places=6)
    output: Decimal = Field(ge=0, le=100000, max_digits=12, decimal_places=6)


class ProviderProfileCreateInput(ContractModel):
    label: str = Field(min_length=1, max_length=100)
    provider: Literal["lm_studio", "deepseek", "mimo", "openrouter", "custom_openai"]
    purpose: Literal["extraction", "planning", "mail_suggestion"] = "extraction"
    base_url: str | None = Field(default=None, max_length=500)
    model_id: str = Field(min_length=1, max_length=200)
    execution_target: Literal["server", "device"] = "server"
    device_id: str | None = Field(default=None, max_length=36)
    api_key: SecretStr | None = None
    timeout_seconds: int = Field(default=30, ge=3, le=120)
    max_output_tokens: int = Field(default=1500, ge=64, le=8192)
    fallback_policy: Literal["none", "rules"] = "rules"
    billing_prices: BillingPricesInput | None = None

    @field_validator("label", "model_id")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_provider_target(self):
        """只接受支持的执行位置和受限地址形态，防止把密钥发向任意主机。"""
        if self.execution_target == "device" and not self.device_id:
            raise ValueError("设备执行配置需要指定设备")
        if self.execution_target == "server" and self.device_id:
            raise ValueError("服务器执行配置不能指定设备")
        if self.provider in {"deepseek", "mimo", "openrouter"} and self.base_url:
            raise ValueError("云提供方使用系统固定地址，不接受自定义 base_url")
        if self.provider == "lm_studio" and self.base_url:
            parsed = urlsplit(self.base_url)
            if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("LM Studio 服务器地址只能使用本机 HTTP 回环地址")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("LM Studio 地址不能包含凭证、查询参数或片段")
        return self


class ProviderProfileUpdateInput(ContractModel):
    expected_version: int = Field(ge=1)
    label: str | None = Field(default=None, min_length=1, max_length=100)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: SecretStr | None = None
    timeout_seconds: int | None = Field(default=None, ge=3, le=120)
    max_output_tokens: int | None = Field(default=None, ge=64, le=8192)
    fallback_policy: Literal["none", "rules"] | None = None
    enabled: bool | None = None
    billing_prices: BillingPricesInput | None = None

    @field_validator("label", "model_id")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None
