from flask import g, jsonify, request

from ..errors import ApiProblem
from ..security import authenticated, parse_json_body
from ..schemas.mail import DigestPreferenceInput
from ..services import mail_service
from . import api_bp


def _limit() -> int:
    """限制邮件状态列表大小，避免无界读取历史记录。"""
    try:
        value = int(request.args.get("limit", "50"))
    except ValueError:
        raise ApiProblem("validation_error", "limit 必须是整数。", 422) from None
    if not 1 <= value <= 100:
        raise ApiProblem("validation_error", "limit 必须在 1 到 100 之间。", 422)
    return value


@api_bp.get("/mail/status")
@authenticated()
def get_mail_status():
    return jsonify(mail_service.mail_transport_status())


@api_bp.get("/mail/preferences")
@authenticated()
def get_mail_preferences():
    return jsonify(mail_service.get_preferences(g.auth_identity["user_id"]))


@api_bp.patch("/mail/preferences")
@authenticated(csrf=True)
def update_mail_preferences():
    payload = parse_json_body(DigestPreferenceInput)
    return jsonify(mail_service.update_preferences(g.auth_identity["user_id"], payload))


@api_bp.get("/mail/preview")
@authenticated()
def preview_mail_digest():
    return jsonify(mail_service.preview_digest(g.auth_identity["user_id"]))


@api_bp.post("/mail/test")
@authenticated(csrf=True)
def queue_test_mail():
    result = mail_service.queue_test_mail(g.auth_identity["user_id"])
    response = jsonify(result)
    response.status_code = 202
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/mail/outbox")
@authenticated()
def list_mail_outbox():
    return jsonify(mail_service.list_outbox(g.auth_identity["user_id"], _limit()))


@api_bp.post("/mail/outbox/<uuid:outbox_id>/retry")
@authenticated(csrf=True)
def retry_mail_outbox(outbox_id):
    result = mail_service.retry_failed_mail(g.auth_identity["user_id"], str(outbox_id))
    response = jsonify(result)
    response.status_code = 202
    return response
