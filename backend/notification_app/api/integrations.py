"""外部通知使用独立 Bearer 凭证，配置入口仍要求本人登录。"""
import time
from flask import g, jsonify, request
from sqlalchemy.orm import Session
from . import api_bp
from ..errors import ApiProblem
from ..schemas.integrations import FeedInput, WebhookNotificationInput
from ..security import authenticated, parse_json_body
from ..services import integration_service as service
from ..services.rate_limit import _increment_bucket


def _private(payload):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/integrations/webhook")
@authenticated()
def webhook_status():
    return _private(service.credential_status(g.auth_identity["user_id"]))


@api_bp.post("/integrations/webhook/token")
@authenticated(csrf=True)
def webhook_token():
    return _private(service.issue_credential(g.auth_identity["user_id"]))


@api_bp.delete("/integrations/webhook/token")
@authenticated(csrf=True)
def webhook_revoke():
    return _private(service.revoke_credential(g.auth_identity["user_id"]))


@api_bp.post("/integrations/webhook")
def webhook_deliver():
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    user_id = service.webhook_user(token if scheme.lower() == "bearer" else "")
    # 共用数据库原子桶，多进程下也不能绕过逐账户投递频率限制。
    with Session(service._engine()) as db, db.begin():
        hits, _ = _increment_bucket(db, "webhook:" + user_id, int(time.time()), 60)
    if hits > 60:
        raise ApiProblem("webhook_rate_limited", "每分钟最多投递60条通知。", 429, retry_after=60)
    result = service.deliver(user_id, parse_json_body(WebhookNotificationInput))
    # 投递端只需要收据，不能借凭证读回用户原有通知或候选内容。
    return _private({"accepted": True, "source_id": result["source"]["id"]}), 202


@api_bp.get("/integrations/feeds")
@authenticated()
def feeds_list():
    return _private(service.list_feeds(g.auth_identity["user_id"]))


@api_bp.post("/integrations/feeds")
@authenticated(csrf=True)
def feeds_create():
    return _private(service.add_feed(g.auth_identity["user_id"], parse_json_body(FeedInput))), 201


@api_bp.delete("/integrations/feeds/<uuid:feed_id>")
@authenticated(csrf=True)
def feeds_delete(feed_id):
    return _private(service.remove_feed(g.auth_identity["user_id"], str(feed_id)))
