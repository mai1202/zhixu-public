from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID


class RequestCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    policy_version: str | None = Field(default=None, max_length=40)


class VerifyCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: UUID
    code: str = Field(pattern=r"^[0-9]{8}$")


class PasswordLoginInput(BaseModel):
    """密码仅用于明确预置的测试账号，不在 HTTP 接口中提供注册或额度设置。"""
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    policy_version: str | None = Field(default=None, max_length=40)
