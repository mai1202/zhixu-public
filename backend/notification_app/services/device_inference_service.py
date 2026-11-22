"""绑定设备领取和回传本机 LM Studio 的有限推理任务。"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..jobs.queue import ClaimedJob, fail_job
from ..models import ChangeSet, Device, Job, JobAttempt, LLMRun, ProviderProfile, SourceDocument, SourceSegment
from ..schemas.business import NormalizedBundle
from . import provider_service, business_service


def _engine():
    """读取当前应用数据库，便于测试注入隔离引擎。"""
    from flask import current_app

    return current_app.extensions["notification_db_engine"]


def _owned_device(db: DbSession, user_id: str, device_id: str) -> Device:
    """确认设备归属和网页确认状态。"""
    device = db.scalar(select(Device).where(Device.id == device_id, Device.user_id == user_id))
    if device is None or device.status != "active":
        raise ApiProblem("device_inactive", "设备已撤销或未确认。", 403)
    return device


def claim_next_local_job(device_id: str, user_id: str, *, now_epoch: int | None = None) -> dict[str, Any] | None:
    """只领取明确绑定本设备的 LM Studio 任务，并用数据库租约防止并发重复执行。"""
    now = int(time.time()) if now_epoch is None else now_epoch
    worker_id = f"device:{device_id}"
    with DbSession(_engine()) as db:
        with db.begin():
            _owned_device(db, user_id, device_id)
            statement = (
                select(Job)
                .where(
                    Job.kind == "local_inference",
                    Job.user_id == user_id,
                    Job.status.in_(("queued", "retry_wait", "leased", "running")),
                    Job.attempt < Job.max_attempts,
                    or_(
                        and_(Job.status.in_(("queued", "retry_wait")), Job.run_at_epoch <= now),
                        and_(Job.status.in_(("leased", "running")), Job.lease_until_epoch <= now),
                    ),
                )
                .order_by(Job.priority.desc(), Job.run_at_epoch, Job.created_at_epoch, Job.id)
                .limit(100)
            )
            if db.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            candidates = db.scalars(statement).all()
            for job in candidates:
                payload = job.payload_json if isinstance(job.payload_json, dict) else {}
                if payload.get("device_id") != device_id:
                    continue
                profile = db.get(ProviderProfile, str(payload.get("profile_id", "")))
                if (
                    profile is None
                    or profile.user_id != user_id
                    or profile.device_id != device_id
                    or profile.execution_target != "device"
                    or profile.provider != "lm_studio"
                ):
                    continue
                task_kind = payload.get("task_kind")
                if task_kind == "agent_completion":
                    # 临时主助手请求超时或父运行已取消后不能再投递到设备。
                    from .local_completion_service import _active
                    try:
                        _active(db, profile, payload.get("parent_run_id"))
                        if payload.get("deadline_epoch", 0) <= now:
                            raise ApiProblem("local_model_timeout", "本机请求已超时。", 409)
                    except ApiProblem:
                        _finish_failed(db, job, now, "local_completion_stopped")
                        continue
                if task_kind == "connection_test":
                    if profile.last_test_status != "pending":
                        continue
                # 已经由用户提交的候选任务即使之后停用配置也继续完成，避免来源永久停在处理中。

                previous_attempt = job.attempt
                update_result = db.execute(
                    update(Job)
                    .where(
                        Job.id == job.id,
                        Job.lock_version == job.lock_version,
                        or_(
                            and_(Job.status.in_(("queued", "retry_wait")), Job.run_at_epoch <= now),
                            and_(Job.status.in_(("leased", "running")), Job.lease_until_epoch <= now),
                        ),
                    )
                    .values(
                        status="running",
                        lease_owner=worker_id,
                        lease_until_epoch=now + max(30, min(600, int(payload.get("timeout_seconds", 30)) + 30)),
                        attempt=Job.attempt + 1,
                        updated_at_epoch=now,
                        finished_at_epoch=None,
                        lock_version=Job.lock_version + 1,
                    )
                )
                if update_result.rowcount != 1:
                    continue
                if previous_attempt:
                    db.execute(
                        update(JobAttempt)
                        .where(
                            JobAttempt.job_id == job.id,
                            JobAttempt.attempt == previous_attempt,
                            JobAttempt.outcome == "running",
                        )
                        .values(finished_at_epoch=now, outcome="lease_expired", error_code="lease_expired")
                    )
                attempt = previous_attempt + 1
                db.add(
                    JobAttempt(
                        id=str(uuid4()),
                        job_id=job.id,
                        attempt=attempt,
                        worker_id=worker_id,
                        started_at_epoch=now,
                        finished_at_epoch=None,
                        outcome="running",
                        error_code=None,
                    )
                )
                return {
                    "id": job.id,
                    "task_kind": task_kind,
                    "model_id": str(payload.get("model_id", "")),
                    "base_url": str(payload.get("base_url", "")),
                    "timeout_seconds": max(3, min(120, int(payload.get("timeout_seconds", 30)))),
                    "max_tokens": max(32, min(8192, int(payload.get("max_tokens", 1200)))),
                    "messages": payload.get("messages", []),
                    **({"completion_body": payload.get("completion_body")} if task_kind == "agent_completion" else {}),
                }
            return None


def complete_local_job(device_id: str, user_id: str, *, job_id: str, content: str, model_id: str,
                       prompt_tokens: int | None, completion_tokens: int | None,
                       now_epoch: int | None = None) -> dict[str, Any]:
    """验证并消化模型响应，只将受控状态和指标写入持久任务。"""
    now = int(time.time()) if now_epoch is None else now_epoch
    worker_id = f"device:{device_id}"
    with DbSession(_engine()) as db, db.begin():
        _owned_device(db, user_id, device_id)
        job = db.scalar(
            select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.kind == "local_inference").with_for_update()
        )
        if (
            job is None
            or job.status != "running"
            or job.lease_owner != worker_id
            or job.lease_until_epoch is None
            or job.lease_until_epoch <= now
        ):
            raise ApiProblem("local_job_lease_lost", "本机推理任务租约已失效，请重新领取。", 409)
        payload = job.payload_json if isinstance(job.payload_json, dict) else {}
        if payload.get("device_id") != device_id:
            raise ApiProblem("local_job_device_mismatch", "该任务未授权给当前设备。", 403)
        profile = db.scalar(
            select(ProviderProfile).where(
                ProviderProfile.id == str(payload.get("profile_id", "")),
                ProviderProfile.user_id == user_id,
                ProviderProfile.device_id == device_id,
                ProviderProfile.execution_target == "device",
                ProviderProfile.provider == "lm_studio",
            )
        )
        if profile is None:
            raise ApiProblem("local_profile_unavailable", "本机模型配置已删除或撤销。", 409)

        task_kind = str(payload.get("task_kind", ""))
        if task_kind == "agent_completion":
            from .local_completion_service import _active, validate_response
            try:
                _active(db, profile, payload.get("parent_run_id"))
                if payload.get("deadline_epoch", 0) <= now:
                    raise ApiProblem("local_model_timeout", "本机请求已超时。", 409)
                value = validate_response(json.loads(content), profile.model_id)
            except (ApiProblem, ValueError, TypeError):
                _finish_failed(db, job, now, "local_response_invalid")
                return {"status": "failed", "error_code": "local_response_invalid"}
            # 中间返回仅供等待中的 Pi/审查请求消费，消费后清除；不直接执行任何业务工具。
            _finish_succeeded(db, job, now, value)
            return {"status": "success"}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        if task_kind == "connection_test":
            if not isinstance(parsed, dict) or parsed.get("status") != "ok":
                _finish_failed(db, job, now, "json_mode_unverified")
                profile.enabled = False
                profile.last_test_status = "failed"
                profile.last_test_epoch = now
                profile.version += 1
                profile.updated_at_epoch = now
                return {"status": "failed", "error_code": "json_mode_unverified"}
            profile.last_test_status = "success"
            profile.last_test_epoch = now
            profile.capabilities_json = {
                **profile.capabilities_json,
                "connection": "verified",
                "json_object": "verified",
                "execution_target": "device",
            }
            profile.version += 1
            profile.updated_at_epoch = now
            _record_local_run(db, job, profile, model_id, prompt_tokens, completion_tokens, now)
            _finish_succeeded(db, job, now, {"status": "success", "profile_id": profile.id})
            return {"status": "success", "profile_id": profile.id}

        if task_kind == "extract":
            from ..services.business_service import _verify_bundle_evidence

            source = db.scalar(
                select(SourceDocument).where(
                    SourceDocument.id == str(payload.get("source_id", "")),
                    SourceDocument.user_id == user_id,
                ).with_for_update()
            )
            change_set = db.scalar(
                select(ChangeSet).where(
                    ChangeSet.id == str(payload.get("change_set_id", "")),
                    ChangeSet.user_id == user_id,
                ).with_for_update()
            )
            if (
                source is None
                or change_set is None
                or source.content_hash != payload.get("source_hash")
                or source.analysis_state != "processing"
                or not business_service.change_unapplied(change_set)
                or change_set.version != int(payload.get("expected_change_set_version", 0))
            ):
                _finish_failed(db, job, now, "source_changed")
                _mark_extraction_done(db, payload, user_id, now, "source_changed")
                _record_extraction_run(db, job, profile, model_id, prompt_tokens, completion_tokens, now, "failed", "source_changed")
                return {"status": "failed", "error_code": "source_changed"}
            try:
                bundle = NormalizedBundle.model_validate_json(content)
                segments = db.scalars(
                    select(SourceSegment).where(
                        SourceSegment.user_id == user_id,
                        SourceSegment.source_document_id == source.id,
                    )
                ).all()
                segment_values = [{"id": item.id, "text": item.text} for item in segments]
                provider_service._check_bundle_references(bundle, source.id, segment_values)
                _verify_bundle_evidence(db, user_id=user_id, source=source, bundle=bundle)
            except (ValueError, TypeError, provider_service.ProviderFailure, ApiProblem) as error:
                code = getattr(error, "code", "invalid_model_output")
                code = "".join(character for character in str(code).lower() if character.isalnum() or character in "_-")[:64] or "invalid_model_output"
                _finish_failed(db, job, now, code)
                _mark_extraction_done(db, payload, user_id, now, code)
                _record_extraction_run(db, job, profile, model_id, prompt_tokens, completion_tokens, now, "failed", code)
                return {"status": "failed", "error_code": code}
            source.analysis_state = "processed"
            source.updated_at_epoch = now
            change_set.bundle_json = bundle.model_dump(mode="json")
            change_set.version += 1
            change_set.updated_at_epoch = now
            business_service._apply_in_session(db, user_id, user_id, change_set, now, actor_type="system")
            _record_extraction_run(db, job, profile, model_id, prompt_tokens, completion_tokens, now, "success", None)
            _finish_succeeded(db, job, now, {"status": "success", "change_set_id": change_set.id})
            return {"status": "success", "change_set_id": change_set.id}

        _finish_failed(db, job, now, "unsupported_local_task")
        return {"status": "failed", "error_code": "unsupported_local_task"}


def fail_local_job(device_id: str, user_id: str, *, job_id: str, error_code: str,
                   now_epoch: int | None = None) -> dict[str, str]:
    """校验设备租约后记录受限失败码，按任务预算重试或结束。"""
    now = int(time.time()) if now_epoch is None else now_epoch
    worker_id = f"device:{device_id}"
    with DbSession(_engine()) as db:
        job = db.scalar(
            select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.kind == "local_inference")
        )
        if job is None:
            raise ApiProblem("not_found", "未找到本机推理任务。", 404)
        payload = job.payload_json if isinstance(job.payload_json, dict) else {}
        if payload.get("device_id") != device_id:
            raise ApiProblem("local_job_device_mismatch", "该任务未授权给当前设备。", 403)
        if job.status != "running" or job.lease_owner != worker_id or not job.lease_until_epoch or job.lease_until_epoch <= now:
            raise ApiProblem("local_job_lease_lost", "本机推理任务租约已失效，请重新领取。", 409)
        claim = ClaimedJob(job.id, job.user_id, job.kind, payload, job.attempt, job.input_version, job.lock_version)
    safe_code = "".join(character for character in error_code.lower() if character.isalnum() or character in "_-")[:80] or "local_inference_failed"
    failed = fail_job(_engine(), claim, worker_id=worker_id, error_code=safe_code, retry_delay_seconds=15, now_epoch=now)
    if not failed:
        raise ApiProblem("local_job_lease_lost", "本机推理任务租约已失效，请重新领取。", 409)
    if payload.get("task_kind") == "connection_test" and job.attempt >= job.max_attempts:
        with DbSession(_engine()) as db, db.begin():
            profile = db.scalar(
                select(ProviderProfile).where(
                    ProviderProfile.id == str(payload.get("profile_id", "")),
                    ProviderProfile.user_id == user_id,
                )
            )
            if profile is not None:
                profile.last_test_status = "failed"
                profile.last_test_epoch = now
                profile.enabled = False
                profile.version += 1
                profile.updated_at_epoch = now
    if payload.get("task_kind") == "extract" and job.attempt >= job.max_attempts:
        with DbSession(_engine()) as db, db.begin():
            _mark_extraction_done(db, payload, user_id, now, safe_code)
    return {"status": "failed" if job.attempt >= job.max_attempts else "retry_wait", "error_code": safe_code}


def _finish_succeeded(db: DbSession, job: Job, now: int, result: dict[str, Any]) -> None:
    """结束任务并关闭当前尝试，不保留原始模型响应。"""
    job.status = "succeeded"
    job.result_json = result
    job.lease_owner = None
    job.lease_until_epoch = None
    job.error_code = None
    job.updated_at_epoch = now
    job.finished_at_epoch = now
    job.lock_version += 1
    attempt = db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id, JobAttempt.attempt == job.attempt))
    if attempt is not None:
        attempt.finished_at_epoch = now
        attempt.outcome = "succeeded"
        attempt.error_code = None


def _finish_failed(db: DbSession, job: Job, now: int, code: str) -> None:
    """结束无效模型输出，不以同一任务自动反复调用设备模型。"""
    job.status = "failed"
    job.error_code = code
    job.result_json = None
    job.lease_owner = None
    job.lease_until_epoch = None
    job.updated_at_epoch = now
    job.finished_at_epoch = now
    job.lock_version += 1
    attempt = db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id, JobAttempt.attempt == job.attempt))
    if attempt is not None:
        attempt.finished_at_epoch = now
        attempt.outcome = "failed"
        attempt.error_code = code


def _record_local_run(db: DbSession, job: Job, profile: ProviderProfile, model_id: str,
                      prompt_tokens: int | None, completion_tokens: int | None, now: int) -> None:
    """保存合成连接测试的最小审计信息，不保存请求或模型返回正文。"""
    from hashlib import sha256

    db.add(
        LLMRun(
            id=str(uuid4()),
            user_id=job.user_id,
            provider_profile_id=profile.id,
            task_kind="connection_test",
            prompt_version="connection_test_v1",
            schema_version="1.0",
            provider="lm_studio",
            model_id=model_id[:200],
            input_hash=sha256(b"synthetic-provider-connection-check-v1").hexdigest(),
            status="success",
            failure_code=None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=0,
            latency_ms=max(0, int((now - job.created_at_epoch) * 1000)),
            created_at_epoch=now,
        )
    )


def _record_extraction_run(db: DbSession, job: Job, profile: ProviderProfile, model_id: str,
                           prompt_tokens: int | None, completion_tokens: int | None, now: int,
                           status: str, failure_code: str | None) -> None:
    """仅记录本机解析哈希、状态和计量，不保存提示词或模型正文。"""
    from hashlib import sha256

    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    db.add(
        LLMRun(
            id=str(uuid4()),
            user_id=job.user_id,
            provider_profile_id=profile.id,
            task_kind="extract",
            prompt_version="extract_v1",
            schema_version="1.0",
            provider="lm_studio",
            model_id=model_id[:200],
            input_hash=str(payload.get("source_hash") or sha256(b"local-extraction-unavailable").hexdigest()),
            status=status,
            failure_code=failure_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=0,
            latency_ms=max(0, int((now - job.created_at_epoch) * 1000)),
            created_at_epoch=now,
        )
    )


def _mark_extraction_done(db: DbSession, payload: dict[str, Any], user_id: str, now: int, code: str) -> None:
    """结束解析中的来源并保留候选，向用户展示可处理的安全错误代码。"""
    source = db.scalar(
        select(SourceDocument).where(
            SourceDocument.id == str(payload.get("source_id", "")),
            SourceDocument.user_id == user_id,
        )
    )
    change_set = db.scalar(
        select(ChangeSet).where(
            ChangeSet.id == str(payload.get("change_set_id", "")),
            ChangeSet.user_id == user_id,
        )
    )
    if source is None or change_set is None or source.analysis_state != "processing":
        return
    source.analysis_state = "processed"
    source.updated_at_epoch = now
    try:
        bundle = NormalizedBundle.model_validate(change_set.bundle_json)
        bundle.warnings.append(f"本机模型分析未能应用（{code}）；已保留原文并使用当前可用解析结果。")
        change_set.bundle_json = bundle.model_dump(mode="json")
        change_set.updated_at_epoch = now
        change_set.version += 1
    except Exception:
        # 损坏候选不会阻止任务和原始材料结束处理状态。
        change_set.version += 1
        change_set.updated_at_epoch = now
        return
    # 实际业务写入失败必须回滚外层事务，不吞掉异常后提交部分结果。
    business_service._apply_in_session(db, user_id, user_id, change_set, now, actor_type="system")
