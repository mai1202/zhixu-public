from uuid import UUID, uuid4

from flask import g, jsonify, request, send_file

from ..errors import ApiProblem
from ..schemas.devices import (
    DeviceChatsInput,
    DeviceClaimInput,
    DeviceHeartbeatInput,
    DeviceLocalJobFailureInput,
    DeviceLocalJobResultInput,
    DeviceRefreshInput,
    DeviceSettingsInput,
    DeviceAttachmentInput,
    SyncBatchInput,
)
from ..security import (
    authenticated,
    device_authenticated,
    parse_json_body,
    validate_device_contract,
    validate_request_origin,
)
from ..services import device_service as service
from ..services import device_inference_service
from ..services import device_attachment_service
from . import api_bp


def _no_store(payload: dict[str, object], status: int = 200):
    """设备凭证与同步状态不进入浏览器或代理缓存。"""
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _bearer_token() -> str:
    """从 Authorization 头取配对状态查询凭证，不接受 URL 查询参数。"""
    scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or len(token) > 256:
        raise ApiProblem("device_token_invalid", "需要有效的设备访问凭证。", 401)
    return token


@api_bp.post('/devices/<uuid:device_id>/sync')
@authenticated(csrf=True)
def request_client_sync(device_id):
    """显式排队本人设备的提前同步，回执前不声称已经上传。"""
    return _no_store(service.request_device_sync(g.auth_identity['user_id'], str(device_id), str(uuid4())), 202)


@api_bp.post("/devices/pairing-codes")
@authenticated(csrf=True)
def create_pairing_code():
    payload = service.create_pairing_code(g.auth_identity["user_id"])
    return _no_store(payload, 201)


@api_bp.get("/devices")
@authenticated()
def list_devices():
    return _no_store(service.list_devices(g.auth_identity["user_id"]))


@api_bp.post("/devices/pair/claim")
def claim_device_pairing():
    validate_request_origin()
    validate_device_contract()
    payload = parse_json_body(DeviceClaimInput)
    result = service.claim_pairing(
        code=payload.code,
        name=payload.name,
        fingerprint=payload.fingerprint,
    )
    return _no_store(result, 202)


@api_bp.get("/devices/pair/status")
def get_device_pairing_status():
    validate_request_origin()
    validate_device_contract()
    return _no_store(service.get_claim_status(_bearer_token()))


@api_bp.post("/devices/refresh")
def refresh_device_credential():
    validate_request_origin()
    validate_device_contract()
    payload = parse_json_body(DeviceRefreshInput)
    return _no_store(service.refresh_device_tokens(payload.refresh_token))


@api_bp.post("/devices/<uuid:device_id>/confirm")
@authenticated(csrf=True)
def confirm_device(device_id: UUID):
    result = service.confirm_device(g.auth_identity["user_id"], str(device_id))
    return _no_store(result)


@api_bp.patch("/devices/<uuid:device_id>/settings")
@authenticated(csrf=True)
def update_device_settings(device_id: UUID):
    payload = parse_json_body(DeviceSettingsInput)
    result = service.update_device_settings(g.auth_identity["user_id"], str(device_id), payload)
    return _no_store(result)


@api_bp.delete("/devices/<uuid:device_id>")
@authenticated(csrf=True)
def revoke_device(device_id: UUID):
    result = service.revoke_device(g.auth_identity["user_id"], str(device_id))
    return _no_store(result)


@api_bp.get("/device/config")
@device_authenticated(scope="heartbeat")
def get_device_config():
    identity = g.device_identity
    result = service.get_device_config(identity["device_id"], identity["user_id"])
    return _no_store(result)


@api_bp.post("/device/unpair")
def unpair_current_device():
    """本机凭证只能撤销其自身；待网页确认的设备也能取消配对。"""
    validate_request_origin()
    validate_device_contract()
    return _no_store(service.unpair_current_device(_bearer_token()))


@api_bp.post("/device/chats")
@device_authenticated(scope="heartbeat")
def update_available_chats():
    payload = parse_json_body(DeviceChatsInput)
    identity = g.device_identity
    result = service.update_available_chats(
        identity["device_id"],
        identity["user_id"],
        payload,
    )
    return _no_store(result)


@api_bp.post("/device/heartbeat")
@device_authenticated(scope="heartbeat")
def heartbeat_device():
    payload = parse_json_body(DeviceHeartbeatInput)
    identity = g.device_identity
    result = service.device_heartbeat(
        identity["device_id"],
        identity["user_id"],
        payload.collected_at_epoch,
        sync_request_id=str(payload.sync_request_id) if payload.sync_request_id else None,
        sync_status=payload.sync_status,
    )
    return _no_store(result)


@api_bp.post("/device-sync/batches")
@device_authenticated(scope="ingest")
def ingest_device_sync_batch():
    payload = parse_json_body(SyncBatchInput)
    identity = g.device_identity
    result = service.ingest_sync_batch(
        identity["device_id"],
        identity["user_id"],
        payload,
    )
    return _no_store(result, 202)


@api_bp.post('/device-sync/attachments')
@device_authenticated(scope='ingest')
def ingest_attachment():
    identity = g.device_identity
    return _no_store(device_attachment_service.ingest(identity['device_id'], identity['user_id'],
        parse_json_body(DeviceAttachmentInput)), 202)


@api_bp.get('/devices/attachments')
@authenticated()
def list_device_attachments():
    source = request.args.get('source')
    if source:
        try:
            source = str(UUID(source))
        except ValueError:
            raise ApiProblem('validation_error', '来源标识无效。', 422) from None
    return _no_store(device_attachment_service.list_attachments(g.auth_identity['user_id'], parent_source_id=source))


@api_bp.post('/devices/attachments/<uuid:attachment_id>/retry')
@authenticated(csrf=True)
def retry_device_attachment(attachment_id):
    return _no_store(device_attachment_service.retry(g.auth_identity['user_id'], str(attachment_id)), 202)


@api_bp.get('/devices/attachments/<uuid:attachment_id>/download')
@authenticated()
def download_device_attachment(attachment_id):
    path, filename = device_attachment_service.attachment_file(g.auth_identity['user_id'], str(attachment_id))
    response = send_file(path, as_attachment=True, download_name=filename)
    response.headers['Cache-Control'] = 'no-store'
    return response


@api_bp.post("/device/local-jobs/claim")
@device_authenticated(scope="own-local-jobs")
def claim_local_inference_job():
    """设备仅领取绑定给自己的推理工作，不影响小时采集批次。"""
    identity = g.device_identity
    result = device_inference_service.claim_next_local_job(
        identity["device_id"], identity["user_id"]
    )
    return _no_store({"job": result})


@api_bp.post("/device/local-jobs/complete")
@device_authenticated(scope="own-local-jobs")
def complete_local_inference_job():
    """验证设备租约及 JSON 输出后，只保存规范结果和调用指标。"""
    payload = parse_json_body(DeviceLocalJobResultInput)
    identity = g.device_identity
    result = device_inference_service.complete_local_job(
        identity["device_id"],
        identity["user_id"],
        job_id=str(payload.job_id),
        content=payload.content,
        model_id=payload.model_id,
        prompt_tokens=payload.prompt_tokens,
        completion_tokens=payload.completion_tokens,
    )
    return _no_store(result)


@api_bp.post("/device/local-jobs/fail")
@device_authenticated(scope="own-local-jobs")
def fail_local_inference_job():
    """只接受服务端登记的设备任务 ID 和受限错误代码。"""
    payload = parse_json_body(DeviceLocalJobFailureInput)
    identity = g.device_identity
    result = device_inference_service.fail_local_job(
        identity["device_id"],
        identity["user_id"],
        job_id=str(payload.job_id),
        error_code=payload.error_code,
    )
    return _no_store(result)
