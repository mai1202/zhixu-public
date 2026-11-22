from flask import g, jsonify

from ..errors import ApiProblem
from ..schemas.llm import ProviderProfileCreateInput, ProviderProfileUpdateInput
from ..security import authenticated, parse_json_body
from ..services import provider_service as service
from . import api_bp


@api_bp.post("/models/profiles/<uuid:profile_id>/data-consent")
@authenticated(csrf=True)
def model_data_consent(profile_id):
    """此入口只给网页登录本人使用，不暴露给助手工具。"""
    from ..schemas.privacy import ModelDataConsentInput
    from ..services.privacy_service import set_model_consent
    return jsonify(set_model_consent(g.auth_identity["user_id"], str(profile_id), parse_json_body(ModelDataConsentInput)))


@api_bp.get("/models/catalog")
@authenticated()
def list_model_catalog():
    return jsonify({"items": service.provider_catalog()})


@api_bp.get("/models/profiles")
@authenticated()
def list_model_profiles():
    return jsonify(service.list_profiles(g.auth_identity["user_id"]))


@api_bp.post("/models/profiles")
@authenticated(csrf=True)
def create_model_profile():
    payload = parse_json_body(ProviderProfileCreateInput)
    response = jsonify(service.create_profile(g.auth_identity["user_id"], payload))
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.patch("/models/profiles/<uuid:profile_id>")
@authenticated(csrf=True)
def update_model_profile(profile_id):
    payload = parse_json_body(ProviderProfileUpdateInput)
    response = jsonify(service.update_profile(g.auth_identity["user_id"], str(profile_id), payload))
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.delete("/models/profiles/<uuid:profile_id>")
@authenticated(csrf=True)
def delete_model_profile(profile_id):
    service.delete_profile(g.auth_identity["user_id"], str(profile_id))
    return jsonify({"status": "deleted"})


@api_bp.post("/models/profiles/<uuid:profile_id>/test")
@authenticated(csrf=True)
def test_model_profile(profile_id):
    result = service.test_profile(g.auth_identity["user_id"], str(profile_id))
    response = jsonify(result)
    if result.get("status") == "queued":
        response.status_code = 202
        response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/models/runs")
@authenticated()
def list_model_runs():
    return jsonify(service.list_runs(g.auth_identity["user_id"]))


@api_bp.get("/models/platform-quota")
@authenticated()
def platform_token_quota():
    """只展示本人平台额度，自配密钥不受此额度影响。"""
    from ..services.token_quota_service import status
    response = jsonify(status(g.auth_identity["user_id"]))
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/models/billing")
@authenticated()
def model_billing():
    """工作台只读取当前登录账户的模型账本，禁止浏览器或代理缓存。"""
    from ..services.token_quota_service import billing
    response = jsonify(billing(g.auth_identity["user_id"]))
    response.headers["Cache-Control"] = "no-store"
    return response
