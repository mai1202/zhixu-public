from typing import Any

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Job(Base):
    """持久任务及其租约；队列工作与 HTTP 请求进程分离。"""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "kind",
            "idempotency_key",
            name="uq_jobs_owner_kind_idempotency",
        ),
        Index("ix_jobs_status_run_at", "status", "run_at_epoch"),
        Index("ix_jobs_lease_until", "status", "lease_until_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    run_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_until_epoch: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    input_version: Mapped[int] = mapped_column(Integer, nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    finished_at_epoch: Mapped[int | None] = mapped_column(Integer)


class JobAttempt(Base):
    """任务每次执行的有限审计记录，不保存正文或异常原文。"""

    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_job_attempts_job_attempt"),
        Index("ix_job_attempts_job_started", "job_id", "started_at_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    finished_at_epoch: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
