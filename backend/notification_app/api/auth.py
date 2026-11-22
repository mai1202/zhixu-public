from flask import current_app, g, jsonify, make_response, request
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session as DbSession
import time

from ..models import WebSession
from ..errors import ApiProblem
from ..schemas.auth import RequestCodeInput, VerifyCodeInput, PasswordLoginInput
from ..security import authenticated, parse_json_body, validate_request_origin
from ..services.auth_service import consume_login_code, issue_login_code, login_local_test_account, set_web_session_cookie
from . import api_bp


@api_bp.post("/auth/request-code")
def request_code():
    validate_request_origin()
    payload = parse_json_body(RequestCodeInput)
    from ..services.privacy_service import POLICY_VERSION
    if current_app.config.get("PRIVACY_CONSENT_REQUIRED", True) and payload.policy_version != POLICY_VERSION:
        raise ApiProblem("privacy_consent_required", "请先阅读并同意资料处理告知。", 422)
    # 本地测试邮箱直接签发同源会话，前端无需跳转独立端口。
    local_session = login_local_test_account(payload.email, policy_version=payload.policy_version)
    if local_session is not None:
        response = jsonify({"local_login": True})
        response.headers["Cache-Control"] = "no-store"
        set_web_session_cookie(response, str(local_session["session_token"]))
        return response
    challenge_id = issue_login_code(
        payload.email,
        request.remote_addr or "unknown",
        policy_version=payload.policy_version,
    )
    response = jsonify(
        {
            "challenge_id": challenge_id,
            "message": "如果地址符合规则，请查收验证码。",
        }
    )
    response.status_code = 202
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/privacy-notice")
def privacy_notice():
    from ..services.privacy_service import notice
    return jsonify(notice())


@api_bp.post("/auth/verify-code")
def verify_code():
    validate_request_origin()
    payload = parse_json_body(VerifyCodeInput)
    result = consume_login_code(payload.challenge_id, payload.code)
    response = make_response(
        jsonify(
            {
                "user": result["user"],
                "csrf_token": result["csrf_token"],
                "expires_in": result["expires_in"],
            }
        )
    )
    response.headers["Cache-Control"] = "no-store"
    set_web_session_cookie(response, str(result['session_token']))
    return response


@api_bp.post('/auth/password-login')
def password_login():
    """只有管理员预置的测试账号可用，复用正式Cookie和CSRF会话。"""
    from ..services.test_account_service import login
    validate_request_origin()
    result = login(parse_json_body(PasswordLoginInput), request.remote_addr or 'unknown')
    response = jsonify({key: result[key] for key in ('user', 'csrf_token', 'expires_in')})
    response.headers['Cache-Control'] = 'no-store'
    set_web_session_cookie(response, str(result['session_token']))
    return response


@api_bp.get("/auth/session")
@authenticated()
def get_session():
    auth_identity = g.auth_identity
    response = jsonify(
        {
            "user": {
                "id": auth_identity["user_id"],
                "email": auth_identity["email"],
                "is_admin": auth_identity["is_admin"],
            },
            "csrf_token": auth_identity["csrf_token"],
            # 前端按剩余时间提前心跳；会话只因主动退出、撤销或账户停用失效。
            "expires_in": max(1, auth_identity['session_expires_at_epoch'] - int(time.time())),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.post("/auth/logout")
@authenticated(csrf=True)
def logout():
    identity = g.auth_identity
    engine = current_app.extensions["notification_db_engine"]
    with DbSession(engine) as db:
        db.execute(
            update(WebSession)
            .where(
                WebSession.id == identity["session_id"],
                WebSession.user_id == identity["user_id"],
                WebSession.revoked_at_epoch.is_(None),
            )
            .values(revoked_at_epoch=int(time.time()))
        )
        db.commit()

    g.skip_session_renewal = True
    response = jsonify({"status": "logged_out"})
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        str(current_app.config["SESSION_COOKIE_NAME"]),
        path="/",
        httponly=True,
        secure=bool(current_app.config["SESSION_COOKIE_SECURE"]),
        samesite="Lax",
    )
    return response


def _device_name(user_agent: str) -> str:
    """仅根据浏览器提供的信息标注设备，不推断硬件身份或地理位置。"""
    browser = next((label for marker, label in (
        ('Edg/', 'Edge'), ('EdgiOS/', 'Edge'), ('EdgA/', 'Edge'),
        ('Firefox/', 'Firefox'), ('FxiOS/', 'Firefox'), ('Chrome/', 'Chrome'),
        ('CriOS/', 'Chrome'), ('Safari/', 'Safari'),
    ) if marker in user_agent), '浏览器')
    system = next((label for marker, label in (
        ('Android', 'Android'), ('iPhone', 'iPhone'), ('iPad', 'iPad'),
        ('Windows', 'Windows'), ('Macintosh', 'macOS'), ('Linux', 'Linux'),
    ) if marker in user_agent), '设备信息未知')
    return f'{browser} · {system}'


@api_bp.get('/auth/sessions')
@authenticated()
def list_sessions():
    """只返回本账户仍可使用的网页登录，不泄露令牌摘要或其他账户信息。"""
    identity = g.auth_identity
    with DbSession(current_app.extensions['notification_db_engine']) as db:
        sessions = db.scalars(select(WebSession).where(
            WebSession.user_id == identity['user_id'],
            WebSession.revoked_at_epoch.is_(None),
            or_(WebSession.persistent.is_(True), WebSession.expires_at_epoch > int(time.time())),
        ).order_by(WebSession.created_at_epoch.desc(), WebSession.id)).all()
        items = [{
            'id': item.id, 'name': _device_name(item.user_agent or ''),
            'created_at_epoch': item.created_at_epoch,
            'last_seen_at_epoch': item.last_seen_at_epoch or item.created_at_epoch,
            'is_current': item.id == identity['session_id'],
            'persistent': item.persistent,
            'expires_at_epoch': None if item.persistent else item.expires_at_epoch,
        } for item in sessions]
    response = jsonify({'items': items})
    response.headers['Cache-Control'] = 'no-store'
    return response


@api_bp.delete('/auth/sessions/<session_id>')
@authenticated(csrf=True)
def revoke_session(session_id: str):
    """按账户边界撤销指定登录；当前设备撤销后也立即删除浏览器 Cookie。"""
    identity = g.auth_identity
    with DbSession(current_app.extensions['notification_db_engine']) as db:
        changed = db.execute(update(WebSession).where(
            WebSession.id == session_id, WebSession.user_id == identity['user_id'],
        ).values(revoked_at_epoch=int(time.time())))
        if changed.rowcount != 1:
            raise ApiProblem('session_not_found', '找不到此登录设备。', 404)
        db.commit()
    current = session_id == identity['session_id']
    response = jsonify({'status': 'revoked', 'is_current': current})
    response.headers['Cache-Control'] = 'no-store'
    if current:
        g.skip_session_renewal = True
        response.delete_cookie(str(current_app.config['SESSION_COOKIE_NAME']), path='/',
            httponly=True, secure=bool(current_app.config['SESSION_COOKIE_SECURE']), samesite='Lax')
    return response
