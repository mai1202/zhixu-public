"""跨模块任务、课程、报告、提醒与操作审计的网页 API。"""

from uuid import UUID

from flask import g, jsonify, request

from ..errors import ApiProblem
from ..schemas.business import VersionInput
from ..schemas.agent_ops import (
    LinkInput,
    ManualReminderInput,
    ReportGenerateInput,
    TaskCreateInput,
    TaskPatchInput,
)
from ..security import authenticated, parse_json_body
from ..services import (
    cross_module_service,
    dashboard_service,
    insight_service,
    operation_service,
    reminder_service,
    report_service,
)
from . import api_bp


def _pagination() -> tuple[int, int]:
    try:
        limit = int(request.args.get("limit", "50"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        raise ApiProblem("validation_error", "分页参数必须是整数。", 422) from None
    if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
        raise ApiProblem("validation_error", "分页范围无效。", 422)
    return limit, offset


@api_bp.post("/tasks")
@authenticated(csrf=True)
def create_managed_task():
    payload = parse_json_body(TaskCreateInput)
    result = cross_module_service.create_task(
        g.auth_identity["user_id"],
        payload,
        actor_type="user",
        actor_id=g.auth_identity["user_id"],
    )
    response = jsonify(result)
    response.status_code = 201
    return response


@api_bp.get("/tasks/<uuid:task_id>")
@authenticated()
def get_managed_task(task_id):
    return jsonify(cross_module_service.task_detail(g.auth_identity["user_id"], str(task_id)))


@api_bp.patch("/tasks/<uuid:task_id>")
@authenticated(csrf=True)
def patch_managed_task(task_id):
    payload = parse_json_body(TaskPatchInput)
    return jsonify(
        cross_module_service.update_task(
            g.auth_identity["user_id"],
            str(task_id),
            payload,
            actor_type="user",
            actor_id=g.auth_identity["user_id"],
        )
    )


@api_bp.get("/courses/search")
@authenticated()
def search_managed_courses():
    try:
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        from datetime import date

        start = date.fromisoformat(start_date) if start_date else None
        end = date.fromisoformat(end_date) if end_date else None
    except ValueError:
        raise ApiProblem("validation_error", "课程日期格式无效。", 422) from None
    return jsonify(
        cross_module_service.search_courses(
            g.auth_identity["user_id"],
            query=request.args.get("q", ""),
            start_date=start,
            end_date=end,
            limit=_pagination()[0],
        )
    )


@api_bp.get("/courses/occurrences/<uuid:occurrence_id>")
@authenticated()
def get_managed_course(occurrence_id):
    return jsonify(cross_module_service.course_detail(g.auth_identity["user_id"], str(occurrence_id)))


@api_bp.post("/links")
@authenticated(csrf=True)
def create_link():
    payload = parse_json_body(LinkInput)
    return jsonify(
        cross_module_service.link_entities(
            g.auth_identity["user_id"],
            payload,
            actor_type="user",
            actor_id=g.auth_identity["user_id"],
        )
    )


@api_bp.delete("/links/<uuid:link_id>")
@authenticated(csrf=True)
def remove_link(link_id):
    return jsonify(cross_module_service.unlink_entity(g.auth_identity["user_id"], str(link_id)))


@api_bp.get("/operations")
@authenticated()
def list_operations():
    limit, offset = _pagination()
    return jsonify(
        operation_service.list_operations(
            g.auth_identity["user_id"],
            limit=limit,
            offset=offset,
            object_type=request.args.get("object_type"),
            object_id=request.args.get("object_id"),
        )
    )


@api_bp.get("/sources/<uuid:source_id>/agent-progress")
@authenticated()
def notification_agent_progress(source_id):
    """返回后台通知 Agent 的受控进度，供现场测试展示真实事件。"""
    return jsonify(operation_service.source_agent_progress(g.auth_identity["user_id"], str(source_id)))


@api_bp.get("/reports")
@authenticated()
def list_reports():
    return jsonify(report_service.list_reports(g.auth_identity["user_id"], limit=_pagination()[0]))


@api_bp.post("/reports")
@authenticated(csrf=True)
def generate_report():
    payload = parse_json_body(ReportGenerateInput)
    result = report_service.generate_report(
        g.auth_identity["user_id"],
        kind=payload.kind,
        period_start=payload.period_start,
        send=payload.send,
    )
    response = jsonify(result)
    response.status_code = 201
    return response


@api_bp.get("/reports/<uuid:report_id>")
@authenticated()
def get_report(report_id):
    return jsonify(report_service.get_report(g.auth_identity["user_id"], str(report_id)))


@api_bp.post("/reports/<uuid:report_id>/send")
@authenticated(csrf=True)
def send_report(report_id):
    return jsonify(report_service.queue_report_email(g.auth_identity["user_id"], str(report_id)))


@api_bp.get("/reminders")
@authenticated()
def list_reminders():
    return jsonify(reminder_service.list_reminders(g.auth_identity["user_id"], limit=_pagination()[0]))


@api_bp.post("/reminders")
@authenticated(csrf=True)
def create_reminder():
    payload = parse_json_body(ManualReminderInput)
    return jsonify(
        reminder_service.create_manual_reminder(
            g.auth_identity["user_id"],
            task_id=str(payload.task_id),
            scheduled_for=payload.scheduled_for,
            note=payload.note,
        )
    )


@api_bp.get("/dashboard")
@authenticated()
def dashboard():
    return jsonify(dashboard_service.dashboard(g.auth_identity["user_id"]))


@api_bp.get("/insights")
@authenticated()
def list_insights():
    module = request.args.get("module")
    include_dismissed = request.args.get("include_dismissed") == "1"
    return jsonify(
        insight_service.list_insights(
            g.auth_identity["user_id"],
            module=module,
            include_dismissed=include_dismissed,
        )
    )


@api_bp.post("/insights/scan")
@authenticated(csrf=True)
def scan_insights():
    result = insight_service.sync_user_insights(g.auth_identity["user_id"])
    return jsonify({**result, **insight_service.list_insights(g.auth_identity["user_id"])})


@api_bp.post("/insights/<uuid:insight_id>/dismiss")
@authenticated(csrf=True)
def dismiss_insight(insight_id):
    payload = parse_json_body(VersionInput)
    return jsonify(
        insight_service.dismiss_insight(
            g.auth_identity["user_id"],
            str(insight_id),
            payload.expected_version,
        )
    )
