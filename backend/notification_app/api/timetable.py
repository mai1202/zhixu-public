from datetime import date
from uuid import UUID

from flask import g, jsonify, request
from pydantic import ValidationError

from ..errors import ApiProblem
from ..schemas.business import VersionInput
from ..schemas.timetable import (
    ConfirmTimetableInput,
    CourseExceptionInput,
    CourseMakeupInput,
    CourseSeriesCreateInput,
    CourseSeriesUpdateInput,
    PreviewTimetableInput,
    ScheduleEntryInput,
    UpdateScheduleEntryInput,
)
from ..security import authenticated, parse_json_body
from ..services import timetable_service as service
from ..timetable.parser import MAX_UPLOAD_BYTES
from . import api_bp


@api_bp.post("/timetable/imports/preview")
@authenticated(csrf=True)
def preview_timetable_import():
    uploaded = request.files.get("file")
    if uploaded is None:
        raise ApiProblem("file_required", "请选择 .xls 或 .xlsx 课表文件。", 422)
    config_data = {key: value for key, value in request.form.to_dict().items() if value != ""}
    try:
        config = PreviewTimetableInput.model_validate(config_data)
    except ValidationError:
        raise ApiProblem("validation_error", "课表预览配置无效。", 422) from None
    file_bytes = uploaded.read(MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ApiProblem("content_too_large", "课表文件不能超过 10 MB。", 413)
    result = service.preview_import(
        g.auth_identity["user_id"],
        file_bytes=file_bytes,
        filename=uploaded.filename or "timetable.xls",
        week1_monday=config.week1_monday,
        week_count=config.week_count,
        timezone_name=config.timezone,
        term_code_override=config.term_code,
    )
    response = jsonify(result)
    response.status_code = 200 if result["already_seen"] else 201
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/timetable/imports")
@authenticated()
def list_timetable_imports():
    return jsonify({"items": service.list_imports(g.auth_identity["user_id"])})


@api_bp.get("/timetable/imports/<uuid:import_id>")
@authenticated()
def get_timetable_import(import_id: UUID):
    """恢复当前账户的核对页面，不公开其他用户的预览。"""
    return jsonify({"import": service.get_import(g.auth_identity["user_id"], str(import_id))})


@api_bp.get("/timetable/terms")
@authenticated()
def list_timetable_terms():
    return jsonify({"items": service.list_terms(g.auth_identity["user_id"])})


@api_bp.get("/timetable/series")
@authenticated()
def list_course_series():
    """课程页与 Agent 共用同一课程系列列表，包含可恢复的归档课程。"""
    return jsonify(
        service.list_course_series(
            g.auth_identity["user_id"],
            term_id=request.args.get("term_id"),
            status=request.args.get("status", "all"),
            query=request.args.get("q", ""),
        )
    )


@api_bp.post("/timetable/series")
@authenticated(csrf=True)
def create_course_series():
    payload = parse_json_body(CourseSeriesCreateInput)
    response = jsonify(
        service.create_course_series(
            g.auth_identity["user_id"],
            payload,
            actor_type="user",
            actor_id=g.auth_identity["user_id"],
        )
    )
    response.status_code = 201
    return response


@api_bp.patch("/timetable/series/<uuid:series_id>")
@authenticated(csrf=True)
def update_course_series(series_id):
    payload = parse_json_body(CourseSeriesUpdateInput)
    return jsonify(
        service.update_course_series(
            g.auth_identity["user_id"],
            str(series_id),
            payload,
            actor_type="user",
            actor_id=g.auth_identity["user_id"],
        )
    )


@api_bp.post("/timetable/series/<uuid:series_id>/archive")
@authenticated(csrf=True)
def archive_course_series(series_id):
    payload = parse_json_body(VersionInput)
    return jsonify(
        service.set_course_series_status(
            g.auth_identity["user_id"],
            str(series_id),
            payload.expected_version,
            restore=False,
            actor_type="user",
            actor_id=g.auth_identity["user_id"],
        )
    )


@api_bp.post("/timetable/series/<uuid:series_id>/restore")
@authenticated(csrf=True)
def restore_course_series(series_id):
    payload = parse_json_body(VersionInput)
    return jsonify(
        service.set_course_series_status(
            g.auth_identity["user_id"],
            str(series_id),
            payload.expected_version,
            restore=True,
            actor_type="user",
            actor_id=g.auth_identity["user_id"],
        )
    )


@api_bp.post("/timetable/imports/<uuid:import_id>/confirm")
@authenticated(csrf=True)
def confirm_timetable_import(import_id):
    payload = parse_json_body(ConfirmTimetableInput)
    return jsonify(
        service.confirm_import(
            g.auth_identity["user_id"],
            str(import_id),
            expected_version=payload.expected_version,
            week1_monday=payload.week1_monday,
            week_count=payload.week_count,
            timezone_name=payload.timezone,
            anchor_confirmed=payload.anchor_confirmed,
            apply_mode=payload.apply_mode,
            period_profile=payload.period_profile,
            course_overrides=payload.course_overrides,
        )
    )


@api_bp.get("/schedule")
@authenticated()
def list_schedule():
    try:
        start_date = date.fromisoformat(request.args["start_date"])
        end_date = date.fromisoformat(request.args["end_date"])
    except (KeyError, ValueError):
        raise ApiProblem("validation_error", "日程查询需要有效的 start_date 和 end_date。", 422) from None
    return jsonify(service.list_schedule(g.auth_identity["user_id"], start_date, end_date))


@api_bp.post("/schedule")
@authenticated(csrf=True)
def create_schedule_entry():
    payload = parse_json_body(ScheduleEntryInput)
    response = jsonify(service.create_schedule_entry(g.auth_identity["user_id"], payload))
    response.status_code = 201
    return response


@api_bp.patch("/schedule/<uuid:entry_id>")
@authenticated(csrf=True)
def update_schedule_entry(entry_id):
    payload = parse_json_body(UpdateScheduleEntryInput)
    return jsonify(service.update_schedule_entry(g.auth_identity["user_id"], str(entry_id), payload))


@api_bp.post("/schedule/<uuid:entry_id>/archive")
@authenticated(csrf=True)
def archive_schedule_entry(entry_id):
    payload = parse_json_body(VersionInput)
    return jsonify(service.archive_schedule_entry(g.auth_identity["user_id"], str(entry_id), payload.expected_version))


@api_bp.post("/schedule/<uuid:entry_id>/restore")
@authenticated(csrf=True)
def restore_schedule_entry(entry_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) - {"expected_version", "allow_conflict"}:
        raise ApiProblem("validation_error", "恢复日程请求格式无效。", 422)
    payload = VersionInput.model_validate({"expected_version": body.get("expected_version")})
    if "allow_conflict" in body and not isinstance(body["allow_conflict"], bool):
        raise ApiProblem("validation_error", "冲突确认必须是布尔值。", 422)
    return jsonify(
        service.restore_schedule_entry(
            g.auth_identity["user_id"],
            str(entry_id),
            payload.expected_version,
            allow_conflict=bool(body.get("allow_conflict", False)),
        )
    )


@api_bp.post("/timetable/occurrences/<uuid:occurrence_id>/exceptions")
@authenticated(csrf=True)
def create_course_exception(occurrence_id):
    payload = parse_json_body(CourseExceptionInput)
    return jsonify(service.apply_course_exception(g.auth_identity["user_id"], str(occurrence_id), payload))


@api_bp.post("/timetable/series/<uuid:series_id>/makeup")
@authenticated(csrf=True)
def create_course_makeup(series_id):
    """常规课表保持不变，补课作为独立实例追加。"""
    payload = parse_json_body(CourseMakeupInput)
    response = jsonify(
        service.create_course_makeup(
            g.auth_identity["user_id"],
            str(series_id),
            payload,
            actor_type="user",
            actor_id=g.auth_identity["user_id"],
        )
    )
    response.status_code = 201
    return response


@api_bp.post("/course-exceptions/<uuid:exception_id>/undo")
@authenticated(csrf=True)
def undo_course_exception(exception_id):
    payload = parse_json_body(VersionInput)
    return jsonify(service.undo_course_exception(g.auth_identity["user_id"], str(exception_id), payload.expected_version))
