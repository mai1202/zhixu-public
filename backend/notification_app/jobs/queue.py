from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..models import Job, JobAttempt, OcrArtifact

ALLOWED_JOB_KINDS = frozenset(
    {
        "source.normalize",
        "ocr.server",
        "agent.run",
        "agent.notification",
        "digest.compose",
        "mail.send",
        # 周报由定时调度创建，和日报一样需要通过队列白名单后才能被 Worker 认领。
        "report.generate",
        "local_inference",
        "knowledge.index",
        "attachment.process",
        # 旧通知直接生效的迁移任务取代人工核对后的恢复任务。
        "notification.apply_legacy",
        "requirements.reminders",
        "planning.replan",
        # 订阅源拉取与可信协作分发和其他任务一样经过工作类型白名单。
        "feed.poll",
        "journey.distribute",
        "operations.alert",
        "operations.balance_check",
    }
)
JOB_STATES = frozenset(
    {
        "queued",
        "leased",
        "running",
        "succeeded",
        "retry_wait",
        "failed",
        "cancelled",
        "expired",
    }
)


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    user_id: str
    kind: str
    payload: dict[str, Any]
    attempt: int
    input_version: int
    lock_version: int


def _canonical_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sync_ocr_state(db: DbSession, job: Job, status: str, error_code: str | None, now: int) -> None:
    """与队列状态同事务更新关联截图；不覆盖已保存成功结果或其他 owner 的记录。"""
    if job.kind != "ocr.server":
        return
    # JSON 字段的 None 可能存为 JSON null，使用 ORM 值判断以兼容两种数据库。
    for artifact in db.scalars(select(OcrArtifact).where(OcrArtifact.job_id == job.id, OcrArtifact.user_id == job.user_id)):
        if artifact.result_json is None:
            artifact.status = status
            artifact.failure_code = error_code
            artifact.updated_at_epoch = now


def enqueue_job(
    engine,
    *,
    user_id: str,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
    run_at_epoch: int | None = None,
    max_attempts: int = 5,
    input_version: int = 1,
    priority: int = 50,
) -> Job:
    """仅由业务服务创建白名单任务，并拒绝同键不同输入。"""
    if kind not in ALLOWED_JOB_KINDS:
        raise ApiProblem("invalid_job_kind", "任务类型未获允许。", 422)
    if not idempotency_key or len(idempotency_key) > 200:
        raise ApiProblem("invalid_idempotency_key", "任务幂等键格式不正确。", 422)
    if not 1 <= max_attempts <= 20 or input_version < 1 or not 0 <= priority <= 100:
        raise ApiProblem("invalid_job_limits", "任务重试或版本参数超出范围。", 422)

    now = int(time.time())
    payload_hash = _canonical_payload_hash(payload)
    with DbSession(engine, expire_on_commit=False) as db:
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            insert = sqlite_insert
        elif dialect == "postgresql":
            insert = postgresql_insert
        else:
            raise RuntimeError("任务幂等写入仅支持已规划的 SQLite 与 PostgreSQL。")

        new_id = str(uuid4())
        statement = insert(Job).values(
            id=new_id,
            user_id=user_id,
            kind=kind,
            status="queued",
            priority=priority,
            payload_json=payload,
            payload_hash=payload_hash,
            result_json=None,
            run_at_epoch=run_at_epoch if run_at_epoch is not None else now,
            lease_owner=None,
            lease_until_epoch=None,
            attempt=0,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            input_version=input_version,
            lock_version=1,
            error_code=None,
            created_at_epoch=now,
            updated_at_epoch=now,
            finished_at_epoch=None,
        ).on_conflict_do_nothing(
            index_elements=[Job.user_id, Job.kind, Job.idempotency_key],
        ).returning(Job.id)
        inserted_id = db.execute(statement).scalar_one_or_none()
        job = db.scalar(
            select(Job).where(
                Job.id == (inserted_id or new_id),
            )
        )
        if job is None:
            job = db.scalar(
                select(Job).where(
                    Job.user_id == user_id,
                    Job.kind == kind,
                    Job.idempotency_key == idempotency_key,
                )
            )
        if job is None:
            db.rollback()
            raise RuntimeError("任务幂等写入后无法读取任务。")
        if job.payload_hash != payload_hash:
            db.rollback()
            raise ApiProblem(
                "idempotency_conflict",
                "该幂等键已经用于不同的任务输入。",
                409,
            )
        db.commit()
        return job


def _claimable(now: int):
    return and_(
        Job.attempt < Job.max_attempts,
        or_(
            and_(
                Job.status.in_(("queued", "retry_wait")),
                Job.run_at_epoch <= now,
            ),
            and_(
                Job.status.in_(("leased", "running")),
                Job.lease_until_epoch.is_not(None),
                Job.lease_until_epoch <= now,
            ),
        ),
    )


def claim_next_job(
    engine,
    *,
    worker_id: str,
    now_epoch: int | None = None,
    lease_seconds: int = 60,
    allowed_kinds: tuple[str, ...] | None = None,
) -> ClaimedJob | None:
    """用条件更新和行锁争抢租约，进程崩溃后可在租约到期时重新领取。"""
    if not worker_id or len(worker_id) > 100 or lease_seconds < 1:
        raise ValueError("worker_id 或租约时长无效")
    worker_kinds = allowed_kinds or tuple(ALLOWED_JOB_KINDS - {"local_inference"})
    if not worker_kinds or any(kind not in ALLOWED_JOB_KINDS for kind in worker_kinds):
        raise ValueError("worker 任务类型必须来自服务端白名单")
    now = int(time.time()) if now_epoch is None else now_epoch

    with DbSession(engine) as db:
        with db.begin():
            exhausted = or_(
                and_(
                    Job.status.in_(("queued", "retry_wait")),
                    Job.run_at_epoch <= now,
                    Job.attempt >= Job.max_attempts,
                ),
                and_(
                    Job.status.in_(("leased", "running")),
                    Job.lease_until_epoch.is_not(None),
                    Job.lease_until_epoch <= now,
                    Job.attempt >= Job.max_attempts,
                ),
            )
            exhausted_jobs = db.execute(
                select(Job).where(exhausted)
            ).scalars().all()
            for exhausted_job in exhausted_jobs:
                # 进程崩溃后租约耗尽也必须收敛材料状态。
                _sync_ocr_state(db, exhausted_job, "failed", "lease_expired", now)
                db.execute(
                    update(JobAttempt)
                    .where(
                        JobAttempt.job_id == exhausted_job.id,
                        JobAttempt.attempt == exhausted_job.attempt,
                        JobAttempt.outcome == "running",
                    )
                    .values(
                        finished_at_epoch=now,
                        outcome="lease_expired",
                        error_code="lease_expired",
                    )
                )
            db.execute(
                update(Job)
                .where(exhausted)
                .values(
                    status="expired",
                    lease_owner=None,
                    lease_until_epoch=None,
                    finished_at_epoch=now,
                    updated_at_epoch=now,
                    lock_version=Job.lock_version + 1,
                )
            )

            statement = (
                select(Job)
                .where(
                    _claimable(now),
                    Job.kind.in_(worker_kinds),
                )
                .order_by(Job.priority.desc(), Job.run_at_epoch, Job.created_at_epoch, Job.id)
                .limit(1)
            )
            if db.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            job = db.scalar(statement)
            if job is None:
                return None
            if (
                job.status in {"leased", "running"}
                and job.lease_until_epoch is not None
                and job.lease_until_epoch <= now
            ):
                db.execute(
                    update(JobAttempt)
                    .where(
                        JobAttempt.job_id == job.id,
                        JobAttempt.attempt == job.attempt,
                        JobAttempt.outcome == "running",
                    )
                    .values(
                        finished_at_epoch=now,
                        outcome="lease_expired",
                        error_code="lease_expired",
                    )
                )

            previous_version = job.lock_version
            row = db.execute(
                update(Job)
                .where(
                    Job.id == job.id,
                    Job.lock_version == previous_version,
                    _claimable(now),
                )
                .values(
                    status="leased",
                    lease_owner=worker_id,
                    lease_until_epoch=now + lease_seconds,
                    attempt=Job.attempt + 1,
                    updated_at_epoch=now,
                    finished_at_epoch=None,
                    lock_version=Job.lock_version + 1,
                )
                .returning(
                    Job.id,
                    Job.user_id,
                    Job.kind,
                    Job.payload_json,
                    Job.attempt,
                    Job.input_version,
                    Job.lock_version,
                )
            ).one_or_none()
            if row is None:
                return None

            db.add(
                JobAttempt(
                    id=str(uuid4()),
                    job_id=row.id,
                    attempt=row.attempt,
                    worker_id=worker_id,
                    started_at_epoch=now,
                    finished_at_epoch=None,
                    outcome="running",
                    error_code=None,
                )
            )
            return ClaimedJob(
                id=row.id,
                user_id=row.user_id,
                kind=row.kind,
                payload=row.payload_json,
                attempt=row.attempt,
                input_version=row.input_version,
                lock_version=row.lock_version,
            )


def mark_job_running(
    engine,
    claim: ClaimedJob,
    *,
    worker_id: str,
    now_epoch: int | None = None,
) -> bool:
    now = int(time.time()) if now_epoch is None else now_epoch
    with DbSession(engine) as db:
        result = db.execute(
            update(Job)
            .where(
                Job.id == claim.id,
                Job.lease_owner == worker_id,
                Job.status == "leased",
                Job.lease_until_epoch > now,
                Job.lock_version == claim.lock_version,
            )
            .values(
                status="running",
                updated_at_epoch=now,
                lock_version=Job.lock_version + 1,
            )
        )
        db.commit()
        return result.rowcount == 1


def heartbeat_job(
    engine,
    claim: ClaimedJob,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    now_epoch: int | None = None,
) -> bool:
    now = int(time.time()) if now_epoch is None else now_epoch
    with DbSession(engine) as db:
        result = db.execute(
            update(Job)
            .where(
                Job.id == claim.id,
                Job.lease_owner == worker_id,
                Job.status.in_(("leased", "running")),
                Job.lease_until_epoch > now,
            )
            .values(
                lease_until_epoch=now + lease_seconds,
                updated_at_epoch=now,
                lock_version=Job.lock_version + 1,
            )
        )
        db.commit()
        return result.rowcount == 1


def complete_job(
    engine,
    claim: ClaimedJob,
    *,
    worker_id: str,
    result_json: dict[str, Any],
    now_epoch: int | None = None,
) -> bool:
    now = int(time.time()) if now_epoch is None else now_epoch
    with DbSession(engine) as db:
        result = db.execute(
            update(Job)
            .where(
                Job.id == claim.id,
                Job.lease_owner == worker_id,
                Job.status.in_(("leased", "running")),
                Job.lease_until_epoch > now,
            )
            .values(
                status="succeeded",
                result_json=result_json,
                lease_owner=None,
                lease_until_epoch=None,
                error_code=None,
                updated_at_epoch=now,
                finished_at_epoch=now,
                lock_version=Job.lock_version + 1,
            )
        )
        if result.rowcount == 1:
            db.execute(
                update(JobAttempt)
                .where(
                    JobAttempt.job_id == claim.id,
                    JobAttempt.attempt == claim.attempt,
                )
                .values(
                    finished_at_epoch=now,
                    outcome="succeeded",
                    error_code=None,
                )
            )
        db.commit()
        return result.rowcount == 1


def fail_job(
    engine,
    claim: ClaimedJob,
    *,
    worker_id: str,
    error_code: str,
    retry_delay_seconds: int = 30,
    retryable: bool = True,
    now_epoch: int | None = None,
) -> bool:
    now = int(time.time()) if now_epoch is None else now_epoch
    safe_error_code = "".join(
        character for character in error_code.lower() if character.isalnum() or character in "_-"
    )[:80] or "worker_error"
    with DbSession(engine) as db:
        job = db.scalar(
            select(Job).where(
                Job.id == claim.id,
                Job.lease_owner == worker_id,
                Job.status.in_(("leased", "running")),
                Job.lease_until_epoch > now,
            )
        )
        if job is None:
            db.rollback()
            return False

        exhausted = not retryable or job.attempt >= job.max_attempts
        db.execute(
            update(Job)
            .where(Job.id == claim.id, Job.lock_version == job.lock_version)
            .values(
                status="failed" if exhausted else "retry_wait",
                run_at_epoch=now if exhausted else now + max(1, retry_delay_seconds),
                lease_owner=None,
                lease_until_epoch=None,
                error_code=safe_error_code,
                updated_at_epoch=now,
                finished_at_epoch=now if exhausted else None,
                lock_version=Job.lock_version + 1,
            )
        )
        _sync_ocr_state(db, job, "failed" if exhausted else "retry_wait", safe_error_code, now)
        db.execute(
            update(JobAttempt)
            .where(
                JobAttempt.job_id == claim.id,
                JobAttempt.attempt == claim.attempt,
            )
            .values(
                finished_at_epoch=now,
                outcome="failed",
                error_code=safe_error_code,
            )
        )
        db.commit()
        return True


def cancel_owned_job(engine, *, user_id: str, job_id: UUID) -> bool | None:
    now = int(time.time())
    with DbSession(engine) as db:
        job = db.scalar(
            select(Job).where(Job.id == str(job_id), Job.user_id == user_id)
        )
        if job is None:
            return None
        if job.status not in {"queued", "retry_wait"}:
            return False
        job.status = "cancelled"
        job.finished_at_epoch = now
        job.updated_at_epoch = now
        job.lock_version += 1
        _sync_ocr_state(db, job, "cancelled", None, now)
        db.commit()
        return True


def retry_owned_job(engine, *, user_id: str, job_id: UUID, payload_updates: dict | None = None) -> bool | None:
    now = int(time.time())
    with DbSession(engine) as db:
        job = db.scalar(
            select(Job).where(Job.id == str(job_id), Job.user_id == user_id)
        )
        if job is None:
            return None
        if job.status not in {"failed", "expired"}:
            return False
        # 只允许调用方传入已校验的显式字段，避免重试改写业务载荷。
        if payload_updates:
            job.payload_json = {**job.payload_json, **payload_updates}
        job.status = "queued"
        job.run_at_epoch = now
        job.lease_owner = None
        job.lease_until_epoch = None
        job.result_json = None
        job.error_code = None
        job.finished_at_epoch = None
        # 人工明确重试时保留旧 attempt 审计，并追加一个新的有限尝试窗口。
        job.max_attempts = min(100, job.max_attempts + max(1, job.attempt))
        job.updated_at_epoch = now
        job.lock_version += 1
        _sync_ocr_state(db, job, "queued", None, now)
        db.commit()
        return True


def job_public_view(job: Job) -> dict[str, Any]:
    """不向浏览器返回 payload、租约令牌或其他用户任务信息。"""
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "run_at_epoch": job.run_at_epoch,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "input_version": job.input_version,
        "result": job.result_json if job.status == "succeeded" else None,
        "error_code": job.error_code,
        "created_at_epoch": job.created_at_epoch,
        "updated_at_epoch": job.updated_at_epoch,
    }
