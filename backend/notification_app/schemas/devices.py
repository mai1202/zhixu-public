from uuid import UUID
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import re


class DeviceClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{10}$")
    name: str = Field(min_length=1, max_length=120)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_name(self):
        if not self.name.strip():
            raise ValueError("设备名称不能为空")
        return self


class DeviceChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1, max_length=256)
    chat_name: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_name(self):
        if not self.chat_name.strip():
            raise ValueError("会话名称不能为空")
        return self


class DeviceChatsInput(BaseModel):
    """本机 CLI 发现的会话目录，仅供账户本人选择上传范围。"""

    model_config = ConfigDict(extra="forbid")

    chats: list[DeviceChatInput] = Field(max_length=200)

    @model_validator(mode="after")
    def unique_chats(self):
        ids = [chat.chat_id for chat in self.chats]
        if len(ids) != len(set(ids)):
            raise ValueError("会话目录中的 chat_id 不能重复")
        return self


class DeviceSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    selected_chats: list[DeviceChatInput] = Field(max_length=200)
    attachments_enabled: bool = False
    attachment_kinds: list[Literal['image', 'file']] | None = Field(default=None, max_length=2)
    attachment_auto_notice: bool | None = None
    # 新配对设备默认逐项选择；全群模式必须由用户另行开启。
    chat_selection_mode: Literal['all', 'selected'] | None = None
    excluded_chat_ids: list[str] | None = Field(default=None, max_length=200)
    # 旧版客户端省略该字段时保留已有间隔；网页允许按整分钟设置 1～60 分钟。
    sync_interval_seconds: int | None = Field(default=None, ge=60, le=3600, multiple_of=60, strict=True)

    @model_validator(mode="after")
    def unique_chats(self):
        if self.attachments_enabled and self.attachment_kinds == []:
            raise ValueError('开启附件采集时需至少选择一种附件类型')
        ids = [chat.chat_id for chat in self.selected_chats]
        if len(ids) != len(set(ids)):
            raise ValueError("群组选择不能重复")
        return self


class DeviceAttachmentInput(BaseModel):
    """附件字节只从认证设备上传，绝不接受服务器要读取的本地路径。"""

    model_config = ConfigDict(extra='forbid')
    source_key: str = Field(min_length=1, max_length=200)
    chat_id: str = Field(min_length=1, max_length=256)
    attachment_key: str = Field(pattern=r'^[0-9a-f]{64}$')
    filename: str = Field(min_length=1, max_length=240)
    kind: Literal['image', 'file']
    content_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    data_base64: str | None = Field(default=None, max_length=14_000_000)
    unavailable_reason: Literal['not_downloaded', 'identity_unavailable', 'unsupported_format', 'too_large', 'hash_mismatch', 'path_rejected'] | None = None

    @model_validator(mode='after')
    def validate_transfer(self):
        if '/' in self.filename or '\\' in self.filename or ':' in self.filename or not self.filename.strip():
            raise ValueError('附件名不能包含路径')
        if self.unavailable_reason is None and (not self.data_base64 or not self.content_hash):
            raise ValueError('可读取附件必须携带内容和 SHA-256')
        if self.unavailable_reason is not None and self.data_base64 is not None:
            raise ValueError('不可读取附件不能携带文件内容')
        return self


class AttachmentMetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern=r"^(image|file|link|unknown)$")
    state: str = Field(pattern=r"^(available|unavailable|metadata_only)$")
    label: str | None = Field(default=None, max_length=160)
    size_bytes: int | None = Field(default=None, ge=0, le=20 * 1024 * 1024)

    @model_validator(mode="after")
    def reject_path_labels(self):
        if self.label and ("/" in self.label or "\\" in self.label or re.match(r"^[A-Za-z]:", self.label)):
            raise ValueError("附件标签不能包含本地路径")
        return self


class SyncMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(
        pattern=r"^(message:[A-Za-z0-9:_-]{1,160}|import-fingerprint:[0-9a-f]{64})$",
        max_length=200,
    )
    chat_id: str = Field(min_length=1, max_length=256)
    chat_name: str = Field(min_length=1, max_length=160)
    sent_at_epoch: int | None = Field(default=None, ge=0)
    # 仅接受组件核实后的布尔身份；旧客户端缺省为未知。
    is_self: bool | None = Field(default=None, strict=True)
    formatted_text: str = Field(min_length=1, max_length=50_000)
    attachment_metadata: list[AttachmentMetadataInput] = Field(default_factory=list, max_length=20)


class SyncFailedPageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset: int = Field(ge=0)
    error_code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class SyncBatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    window_start_epoch: int = Field(ge=0)
    window_end_epoch: int = Field(ge=0)
    complete: bool
    failed_pages: list[SyncFailedPageInput] = Field(default_factory=list, max_length=50)
    messages: list[SyncMessageInput] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_window_and_items(self):
        if self.window_end_epoch < self.window_start_epoch:
            raise ValueError("同步窗口结束时间早于开始时间")
        if self.window_end_epoch - self.window_start_epoch > 7 * 24 * 60 * 60:
            raise ValueError("单个同步窗口最多覆盖七天")
        keys = [message.source_key for message in self.messages]
        if len(keys) != len(set(keys)):
            raise ValueError("同一批次的来源键不能重复")
        return self


class DeviceRefreshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=40, max_length=256)


class DeviceHeartbeatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collected_at_epoch: int | None = Field(default=None, ge=0)
    sync_request_id: UUID | None = None
    # running 只上报进度，服务端不会把它当成完成回执。
    sync_status: Literal['running', 'completed', 'partial', 'waiting_for_chat_selection'] | None = None


class DeviceLocalJobResultInput(BaseModel):
    """本机模型只回传有限长度响应和用量，不接受任意对象写入。"""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    content: str = Field(min_length=1, max_length=40_000)
    model_id: str = Field(min_length=1, max_length=200)
    prompt_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    completion_tokens: int | None = Field(default=None, ge=0, le=10_000_000)


class DeviceLocalJobFailureInput(BaseModel):
    """失败回传只包含受限错误码，不携带模型正文或本机异常详情。"""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    error_code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
