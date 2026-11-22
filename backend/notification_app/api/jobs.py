from uuid import UUID

from flask import current_app, g, jsonify, request
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..jobs.queue import (
    cancel_owned_job,
    job_public_view,
    retry_owned_job,
)
from ..models import Job
from ..security import authenticated
from . import api_bp


def _parse_job_id(value: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError):
        raise ApiProblem("not_found", "未找到该任务。", 404) from None


@api_bp.get("/jobs")
@authenticated()
def list_jobs():
    raw_limit = request.args.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        raise ApiProblem("validation_error", "limit 必须是整数。", 422) from None
    if not 1 <= limit <= 100:
        raise ApiProblem("validation_error", "limit 必须在 1 到 100 之间。", 422)

    engine = current_app.extensions["notification_db_engine"]
    with DbSession(engine) as db:
        jobs = db.scalars(
            select(Job)
            .where(Job.user_id == g.auth_identity["user_id"])
            .order_by(Job.created_at_epoch.desc(), Job.id)
            .limit(limit)
        ).all()
    return jsonify({"items": [job_public_view(job) for job in jobs]})


@api_bp.get("/jobs/<job_id>")
@authenticated()
def get_job(job_id: str):
    parsed_id = _parse_job_id(job_id)
    engine = current_app.extensions["notification_db_engine"]
    with DbSession(engine) as db:
        job = db.scalar(
            select(Job).where(
                Job.id == str(parsed_id),
                Job.user_id == g.auth_identity["user_id"],
            )
        )
    if job is None:
        raise ApiProblem("not_found", "未找到该任务。", 404)
    return jsonify(job_public_view(job))


@api_bp.post("/jobs/<job_id>/cancel")
@authenticated(csrf=True)
def cancel_job(job_id: str):
    result = cancel_owned_job(
        current_app.extensions["notification_db_engine"],
        user_id=g.auth_identity["user_id"],
        job_id=_parse_job_id(job_id),
    )
    if result is None:
        raise ApiProblem("not_found", "未找到该任务。", 404)
    if not result:
        raise ApiProblem(
            "job_not_cancellable",
            "任务已开始或已结束，当前不能取消。",
            409,
        )
    return jsonify({"status": "cancelled"})


@api_bp.post("/jobs/<job_id>/retry")
@authenticated(csrf=True)
def retry_job(job_id: str):
    result = retry_owned_job(
        current_app.extensions["notification_db_engine"],
        user_id=g.auth_identity["user_id"],
        job_id=_parse_job_id(job_id),
    )
    if result is None:
        raise ApiProblem("not_found", "未找到该任务。", 404)
    if not result:
        raise ApiProblem(
            "job_not_retryable",
            "只有失败或过期任务可以重新排队。",
            409,
        )
    return jsonify({"status": "queued"})
