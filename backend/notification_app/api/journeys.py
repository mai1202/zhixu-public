"""办理、时间试算、主动加入协作与可解释机会匹配的网页登录入口。"""

from flask import g, jsonify

from ..schemas.journeys import (JourneyInviteInput, JourneyJoinInput, JourneyPlanInput,
    JourneySaveInput, JourneyStepActionInput, JourneyVersionInput, StudentIntentInput)
from ..security import authenticated, parse_json_body
from ..services import journey_service as service
from . import api_bp


@api_bp.get("/journeys")
@authenticated()
def journeys_list():
    return jsonify(service.list_journeys(g.auth_identity["user_id"]))


@api_bp.post("/tasks/<uuid:task_id>/journey")
@authenticated(csrf=True)
def journeys_create(task_id):
    return jsonify(service.create_journey(g.auth_identity["user_id"], str(task_id)))


@api_bp.get("/journeys/<uuid:journey_id>")
@authenticated()
def journeys_get(journey_id):
    return jsonify(service.get_journey(g.auth_identity["user_id"], str(journey_id)))


@api_bp.put("/journeys/<uuid:journey_id>")
@authenticated(csrf=True)
def journeys_save(journey_id):
    return jsonify(service.save_journey(g.auth_identity["user_id"], str(journey_id), parse_json_body(JourneySaveInput)))


@api_bp.post("/journeys/<uuid:journey_id>/refresh-source")
@authenticated(csrf=True)
def journeys_refresh(journey_id):
    return jsonify(service.refresh_source(g.auth_identity["user_id"], str(journey_id), parse_json_body(JourneyVersionInput).expected_version))


@api_bp.post("/journeys/<uuid:journey_id>/steps/<uuid:step_id>")
@authenticated(csrf=True)
def journeys_step(journey_id, step_id):
    return jsonify(service.step_action(g.auth_identity["user_id"], str(journey_id), str(step_id), parse_json_body(JourneyStepActionInput)))


@api_bp.post("/journeys/<uuid:journey_id>/invite")
@authenticated(csrf=True)
def journeys_invite(journey_id):
    return jsonify(service.invite(g.auth_identity["user_id"], str(journey_id), parse_json_body(JourneyInviteInput)))


@api_bp.post("/journeys/<uuid:journey_id>/close-invite")
@authenticated(csrf=True)
def journeys_close_invite(journey_id):
    return jsonify(service.close_invite(g.auth_identity["user_id"], str(journey_id), parse_json_body(JourneyVersionInput).expected_version))


@api_bp.post("/journeys/join")
@authenticated(csrf=True)
def journeys_join():
    return jsonify(service.join(g.auth_identity["user_id"], parse_json_body(JourneyJoinInput)))


@api_bp.post("/journeys/<uuid:journey_id>/members/<uuid:member_id>/revoke")
@authenticated(csrf=True)
def journeys_revoke(journey_id, member_id):
    return jsonify(service.revoke(g.auth_identity["user_id"], str(journey_id), str(member_id), parse_json_body(JourneyVersionInput).expected_version))


@api_bp.post("/journeys/<uuid:journey_id>/plan")
@authenticated(csrf=True)
def journeys_plan(journey_id):
    return jsonify(service.plan_journey(g.auth_identity["user_id"], str(journey_id), parse_json_body(JourneyPlanInput)))


@api_bp.post("/journeys/<uuid:journey_id>/leave")
@authenticated(csrf=True)
def journeys_leave(journey_id):
    return jsonify(service.leave(g.auth_identity["user_id"], str(journey_id), parse_json_body(JourneyVersionInput).expected_version))


@api_bp.post("/journeys/<uuid:journey_id>/schedule")
@authenticated(csrf=True)
def journeys_schedule(journey_id):
    return jsonify(service.plan_journey(g.auth_identity["user_id"], str(journey_id), parse_json_body(JourneyPlanInput), commit=True))


@api_bp.get("/student-intent")
@authenticated()
def student_intent_get():
    return jsonify(service.get_intent(g.auth_identity["user_id"]))


@api_bp.put("/student-intent")
@authenticated(csrf=True)
def student_intent_save():
    return jsonify(service.save_intent(g.auth_identity["user_id"], parse_json_body(StudentIntentInput)))


@api_bp.get("/opportunities")
@authenticated()
def opportunities_list():
    return jsonify(service.opportunities(g.auth_identity["user_id"]))


@api_bp.post("/opportunities/<uuid:event_id>/adopt")
@authenticated(csrf=True)
def opportunities_adopt(event_id):
    return jsonify(service.adopt_opportunity(g.auth_identity["user_id"], str(event_id), parse_json_body(JourneyVersionInput).expected_version))
