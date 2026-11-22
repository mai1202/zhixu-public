from flask import g, jsonify, request
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..models import SourceDocument, SourceSegment
from ..schemas.business import (
    ChangeSetUpdateInput,
    CreateSourceInput,
    DemoNotificationInput,
    EventPatchInput,
    NoticePatchInput,
    TaskStatusInput,
    VersionInput,
)
from ..security import authenticated, parse_json_body
from ..services import business_service as service
from ..services import cross_module_service
from . import api_bp


def _pagination() -> tuple[int, int]:
    """限制每页大小和偏移量，避免无界读取个人业务数据。"""
    try:
        limit = int(request.args.get("limit", "20"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        raise ApiProblem("validation_error", "分页参数必须是整数。", 422) from None
    if limit < 1 or limit > 100 or offset < 0 or offset > 1_000_000:
        raise ApiProblem("validation_error", "分页范围无效。", 422)
    return limit, offset


def _status_filter(allowed: set[str]) -> str | None:
    value = request.args.get("status")
    if value is not None and value not in allowed:
        raise ApiProblem("validation_error", "状态筛选值无效。", 422)
    return value


@api_bp.post("/sources")
@authenticated(csrf=True)
def create_source():
    payload = parse_json_body(CreateSourceInput)
    key = request.headers.get("Idempotency-Key")
    if key:
        try:
            key = str(UUID(key))
        except ValueError:
            raise ApiProblem("invalid_idempotency_key", "提交标识无效，请刷新页面后重试。", 422) from None
    # 键按 owner 和来源类型隔离，只合并同一次操作，不按文本全局去重。
    result = service.create_source(g.auth_identity["user_id"], payload, external_key=f"web:{key}" if key else None,
                                   submission_hash=service.submission_fingerprint(payload) if key else None)
    response = jsonify(result)
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.post("/demo/notifications")
@authenticated(csrf=True)
def create_demo_notification():
    """评委随机输入使用正式来源/提取/Agent 工具链，不返回任何预写答案。"""
    payload = parse_json_body(DemoNotificationInput)
    create_payload = CreateSourceInput(
        source_type="text",
        analysis_mode=payload.analysis_mode,
        provider_profile_id=payload.provider_profile_id,
        title=payload.title,
        text=payload.text,
    )
    key = request.headers.get("Idempotency-Key")
    if key:
        try:
            key = str(UUID(key))
        except ValueError:
            raise ApiProblem("invalid_idempotency_key", "测试提交标识无效。", 422) from None
    result = service.create_source(
        g.auth_identity["user_id"],
        create_payload,
        source_type_override="demo",
        external_key=f"demo:{key}" if key else None,
        submission_hash=service.submission_fingerprint(create_payload) if key else None,
    )
    return jsonify({**result, "demo": True})


@api_bp.get("/sources/<uuid:source_id>")
@authenticated()
def get_source(source_id):
    user_id = g.auth_identity["user_id"]
    engine = service._engine()
    with DbSession(engine) as db:
        source = db.scalar(
            select(SourceDocument).where(
                SourceDocument.id == str(source_id),
                SourceDocument.user_id == user_id,
            )
        )
        if source is None:
            raise ApiProblem("not_found", "未找到该材料。", 404)
        segments = db.scalars(
            select(SourceSegment)
            .where(SourceSegment.user_id == user_id, SourceSegment.source_document_id == source.id)
            .order_by(SourceSegment.ordinal)
        ).all()
    return jsonify(
        {
            "source": service.source_public(source),
            "segments": [
                {
                    "id": item.id,
                    "ordinal": item.ordinal,
                    "text": item.text,
                    "start_offset": item.start_offset,
                    "end_offset": item.end_offset,
                }
                for item in segments
            ],
        }
    )


@api_bp.get("/inbox")
@authenticated()
def list_inbox():
    limit, offset = _pagination()
    return jsonify(
        service.list_change_sets(
            g.auth_identity["user_id"],
            limit=limit,
            offset=offset,
            # 旧地址仅返回通知操作历史，通知不再支持核对状态筛选。
            status=None,
            kind="normalize",
        )
    )


@api_bp.get("/inbox/<uuid:change_set_id>")
@authenticated()
def get_inbox_item(change_set_id):
    return jsonify(service.get_change_set(g.auth_identity["user_id"], str(change_set_id)))


@api_bp.patch("/changes/<uuid:change_set_id>")
@authenticated(csrf=True)
def update_change(change_set_id):
    payload = parse_json_body(ChangeSetUpdateInput)
    return jsonify(service.update_change_set(g.auth_identity["user_id"], str(change_set_id), payload))


@api_bp.post("/changes/<uuid:change_set_id>/confirm")
@authenticated(csrf=True)
def confirm_change(change_set_id):
    payload = parse_json_body(VersionInput)
    return jsonify(
        service.confirm_change_set(
            g.auth_identity["user_id"],
            g.auth_identity["user_id"],
            str(change_set_id),
            payload.expected_version,
        )
    )


@api_bp.post("/changes/<uuid:change_set_id>/reject")
@authenticated(csrf=True)
def reject_change(change_set_id):
    payload = parse_json_body(VersionInput)
    return jsonify(
        service.reject_change_set(g.auth_identity["user_id"], str(change_set_id), payload.expected_version)
    )


@api_bp.post("/changes/<uuid:change_set_id>/undo")
@authenticated(csrf=True)
def undo_change(change_set_id):
    return jsonify(
        service.undo_change_set(
            g.auth_identity["user_id"], g.auth_identity["user_id"], str(change_set_id)
        )
    )


@api_bp.post("/changes/<uuid:change_set_id>/reopen")
@authenticated(csrf=True)
def reopen_change(change_set_id):
    """旧入口只返回流程退役错误；忽略通知仍可通过邮箱分组移回收件箱。"""
    payload = parse_json_body(VersionInput)
    return jsonify(service.reopen_change_set(g.auth_identity["user_id"], str(change_set_id), payload.expected_version))


@api_bp.get("/notices")
@authenticated()
def list_notices():
    limit, offset = _pagination()
    status = _status_filter({"active", "archived"})
    return jsonify(
        service.list_notices(
            g.auth_identity["user_id"],
            limit=limit,
            offset=offset,
            status=status or "active",
            query_text=request.args.get("q"),
        )
    )


@api_bp.get("/notices/<uuid:notice_id>")
@authenticated()
def get_notice(notice_id):
    return jsonify(service.get_notice(g.auth_identity["user_id"], str(notice_id)))


@api_bp.patch("/notices/<uuid:notice_id>")
@authenticated(csrf=True)
def patch_notice(notice_id):
    payload = parse_json_body(NoticePatchInput)
    return jsonify(
        cross_module_service.update_notice(
            g.auth_identity["user_id"],
            str(notice_id),
            payload,
            actor_type="user",
            actor_id=g.auth_identity["user_id"],
        )
    )


@api_bp.post("/notices/<uuid:notice_id>/archive")
@authenticated(csrf=True)
def archive_notice(notice_id):
    change_set = service.create_archive_change_set(
        g.auth_identity["user_id"], g.auth_identity["user_id"], str(notice_id)
    )
    response = jsonify({"change_set": change_set})
    response.status_code = 201
    return response


@api_bp.get("/events")
@authenticated()
def list_events():
    limit, offset = _pagination()
    status = _status_filter({"open", "archived"})
    return jsonify(service.list_events(g.auth_identity["user_id"], limit=limit, offset=offset, status=status or "open"))


@api_bp.patch("/events/<uuid:event_id>")
@authenticated(csrf=True)
def patch_event(event_id):
    payload = parse_json_body(EventPatchInput)
    values = payload.model_dump(mode="json", exclude_unset=True)
    expected_version = values.pop("expected_version")
    if "title" in values and values["title"] is None:
        raise ApiProblem("validation_error", "事项标题不能为空。", 422)
    if values.get("eligibility", "unknown") is None:
        raise ApiProblem("validation_error", "适用状态不能为空。", 422)
    for key in ("materials", "unknowns"):
        if key in values and values[key] is None:
            values[key] = []
    if "evidence" in values and values["evidence"] is None:
        values["evidence"] = {}
    if not values:
        raise ApiProblem("empty_patch", "至少需要修改一个字段。", 422)
    return jsonify(
        service.create_event_patch_change_set(
            g.auth_identity["user_id"], str(event_id), expected_version, values
        )
    )


@api_bp.get("/tasks")
@authenticated()
def list_tasks():
    limit, offset = _pagination()
    status = _status_filter({"open", "completed", "cancelled"})
    return jsonify(service.list_tasks(g.auth_identity["user_id"], limit=limit, offset=offset, status=status or "open"))


@api_bp.post("/tasks/<uuid:task_id>/complete")
@authenticated(csrf=True)
def complete_task(task_id):
    payload = parse_json_body(TaskStatusInput)
    return jsonify(
        service.set_task_status(
            g.auth_identity["user_id"],
            g.auth_identity["user_id"],
            str(task_id),
            payload.expected_version,
            "completed",
        )
    )


@api_bp.post("/tasks/<uuid:task_id>/reopen")
@authenticated(csrf=True)
def reopen_task(task_id):
    payload = parse_json_body(TaskStatusInput)
    return jsonify(
        service.set_task_status(
            g.auth_identity["user_id"],
            g.auth_identity["user_id"],
            str(task_id),
            payload.expected_version,
            "open",
        )
    )


@api_bp.post("/tasks/<uuid:task_id>/cancel")
@authenticated(csrf=True)
def cancel_task(task_id):
    payload = parse_json_body(TaskStatusInput)
    return jsonify(
        service.set_task_status(
            g.auth_identity["user_id"],
            g.auth_identity["user_id"],
            str(task_id),
            payload.expected_version,
            "cancelled",
        )
    )
