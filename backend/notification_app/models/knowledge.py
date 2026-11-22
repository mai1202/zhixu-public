"""知识文档与运营邮箱入口；个人原始资料仍复用各业务表。"""

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class KnowledgeDocument(Base):
    """可追溯的公共规定或私人笔记；公开不由模型自行决定。"""
    __tablename__ = 'knowledge_documents'
    __table_args__ = (Index('ix_knowledge_scope_owner_updated', 'scope', 'user_id', 'updated_at_epoch'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    scope: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    content_hash: Mapped[str] = mapped_column(String(64))
    # 发布单位、发布日期、生效状态、适用人群和替代关系；JSON 便于后续扩展。
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default='active')
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at_epoch: Mapped[int] = mapped_column(Integer)
    updated_at_epoch: Mapped[int] = mapped_column(Integer)


class KnowledgeChunk(Base):
    """可选向量/关键词融合索引；原文仍保留在原业务表，可随时重建。"""
    __tablename__ = 'knowledge_chunks'
    __table_args__ = (
        Index('ix_knowledge_chunks_owner_source', 'user_id', 'source_kind', 'source_id'),
        Index('ix_knowledge_chunks_owner_updated', 'user_id', 'updated_at_epoch'),
        Index('ix_knowledge_chunks_scope_updated', 'scope', 'updated_at_epoch'),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    source_kind: Mapped[str] = mapped_column(String(24))
    source_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16), default='private')
    source_version: Mapped[int] = mapped_column(Integer, default=1)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding_json: Mapped[list | None] = mapped_column(JSON)
    embedding_model: Mapped[str | None] = mapped_column(String(120))
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)
    chunker_version: Mapped[str] = mapped_column(String(24), default='v1')
    updated_at_epoch: Mapped[int] = mapped_column(Integer)


class MailIngressAccount(Base):
    """收件码绑定已验证邮箱；摘要用于匹配，密文仅供本人再次查看。"""
    __tablename__ = 'mail_ingress_accounts'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    token_ciphertext: Mapped[str] = mapped_column(Text)
    updated_at_epoch: Mapped[int] = mapped_column(Integer)
    command_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    command_token_digest: Mapped[str | None] = mapped_column(String(64))
    command_token_ciphertext: Mapped[str | None] = mapped_column(Text)


class MailIngressReceipt(Base):
    """运营邮箱的 UID 收据；重试保留已处理附件，不重复建立通知。"""
    __tablename__ = 'mail_ingress_receipts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    lease_until_epoch: Mapped[int] = mapped_column(Integer)
    updated_at_epoch: Mapped[int] = mapped_column(Integer)


class MailIngressCursor(Base):
    """只保存 IMAP 游标和受控错误码，不把邮箱密码或其他邮件正文写入日志。"""
    __tablename__ = 'mail_ingress_cursors'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    uid_validity: Mapped[str] = mapped_column(String(80))
    last_uid: Mapped[int] = mapped_column(Integer, default=0)
    last_poll_epoch: Mapped[int] = mapped_column(Integer)
    # 只有完整成功的只读轮询推进；旧游标不能由尝试时间回填成功时间。
    last_success_epoch: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
