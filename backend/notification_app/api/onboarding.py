"""首次引导入口使用既有会话和 CSRF，不能替其他账户配置。"""
from flask import g, jsonify
from . import api_bp
from ..security import authenticated, parse_json_body
from ..schemas.onboarding import OnboardingStartInput, OnboardingUpdateInput
from ..services import onboarding_service


@api_bp.get('/onboarding')
@authenticated()
def get_onboarding():
    return jsonify(onboarding_service.get_state(g.auth_identity['user_id']))


@api_bp.patch('/onboarding')
@authenticated(csrf=True)
def update_onboarding():
    return jsonify(onboarding_service.update_state(g.auth_identity['user_id'], parse_json_body(OnboardingUpdateInput)))


@api_bp.post('/onboarding/start')
@authenticated(csrf=True)
def start_onboarding():
    return jsonify(onboarding_service.start_agent(g.auth_identity['user_id'], parse_json_body(OnboardingStartInput))), 202
