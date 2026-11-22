"""首页待确认建议只由本人读取、确认或忽略。"""
from flask import g, jsonify
from . import api_bp
from ..security import authenticated, parse_json_body
from ..schemas.notification_suggestions import SuggestionConfirmInput, SuggestionDismissInput
from ..services import notification_suggestion_service as service


@api_bp.get("/notification-suggestions")
@authenticated()
def suggestion_list(): return jsonify(service.list_pending(g.auth_identity["user_id"]))


@api_bp.post("/notification-suggestions/<uuid:suggestion_id>/confirm")
@authenticated(csrf=True)
def suggestion_confirm(suggestion_id):
    return jsonify(service.confirm(g.auth_identity["user_id"], str(suggestion_id), parse_json_body(SuggestionConfirmInput)))


@api_bp.post("/notification-suggestions/<uuid:suggestion_id>/dismiss")
@authenticated(csrf=True)
def suggestion_dismiss(suggestion_id):
    return jsonify(service.dismiss(g.auth_identity["user_id"], str(suggestion_id), parse_json_body(SuggestionDismissInput).expected_version))
