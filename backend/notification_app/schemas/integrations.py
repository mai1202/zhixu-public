"""外部投递只携带通知资料，不提供用户身份或助手权限。"""
from pydantic import Field
from .business import ContractModel


class WebhookNotificationInput(ContractModel):
    source_key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=100000)


class FeedInput(ContractModel):
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
