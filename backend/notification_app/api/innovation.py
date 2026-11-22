"""复用共享 Blueprint 的要求、核对、规划和运行状态入口。"""

from flask import g, jsonify, request

from . import api_bp
from ..security import authenticated, parse_json_body
from ..schemas.innovation import (FieldPolicyInput,
    PlanningApplyInput, PlanningPreferenceInput, PlanningPreviewInput, RequirementUpdateInput, SchedulePolicyInput)
from ..services import requirement_service as requirements, planning_service as planning


def _user():
    return g.auth_identity["user_id"]


@api_bp.get("/events/<uuid:event_id>/requirements")
@authenticated()
def event_requirements(event_id):
    return jsonify(requirements.get_requirements(_user(), str(event_id)))


@api_bp.patch("/events/<uuid:event_id>/field-policy")
@authenticated(csrf=True)
def event_field_policy(event_id):
    return jsonify(requirements.field_policy(_user(), str(event_id), parse_json_body(FieldPolicyInput)))


@api_bp.post("/requirement-changes")
@authenticated(csrf=True)
def requirements_update():
    """字段证据通过后直接应用，不创建或等待人工核对。"""
    return jsonify(requirements.update_requirements(_user(), parse_json_body(RequirementUpdateInput)))


@api_bp.get("/requirement-changes/<uuid:change_id>")
@authenticated()
def requirements_get(change_id):
    return jsonify(requirements.get_change(_user(), str(change_id)))


@api_bp.get("/planning/preferences")
@authenticated()
def planning_get_preferences():
    return jsonify(planning.preferences(_user()))


@api_bp.put("/planning/preferences")
@authenticated(csrf=True)
def planning_save_preferences():
    return jsonify(planning.save_preferences(_user(), parse_json_body(PlanningPreferenceInput)))


@api_bp.post("/planning/preview")
@authenticated(csrf=True)
def planning_preview():
    return jsonify(planning.preview(_user(), parse_json_body(PlanningPreviewInput)))


@api_bp.get("/planning/runs/<uuid:run_id>")
@authenticated()
def planning_get(run_id):
    return jsonify(planning.get_run(_user(), str(run_id)))


@api_bp.post("/planning/runs/<uuid:run_id>/apply")
@authenticated(csrf=True)
def planning_apply(run_id):
    return jsonify(planning.apply(_user(), str(run_id), parse_json_body(PlanningApplyInput)))


@api_bp.patch("/schedule-entries/<uuid:entry_id>/planning-policy")
@authenticated(csrf=True)
def planning_entry_policy(entry_id):
    return jsonify(planning.set_policy(_user(), str(entry_id), parse_json_body(SchedulePolicyInput)))


@api_bp.get("/today/actions")
@authenticated()
def today_actions():
    from ..services.innovation_status_service import today_actions
    return jsonify(today_actions(_user()))


@api_bp.get("/sources/health")
@authenticated()
def source_health():
    from ..services.innovation_status_service import source_health
    return jsonify(source_health(_user()))
