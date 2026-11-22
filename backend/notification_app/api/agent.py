from uuid import UUID, uuid4

from flask import g, jsonify, request, Response, stream_with_context
from flask import send_file
import json
import time
from pydantic import ValidationError

from ..errors import ApiProblem
from ..jobs.queue import cancel_owned_job
from ..jobs.queue import retry_owned_job
from ..security import authenticated, parse_json_body
from ..schemas.agent import AgentConversationActionInput, AgentMemoryInput, AgentPreferenceInput, AgentRetryInput, AgentTurnInput
from ..services import agent_service
from ..services import agent_material_service, agent_memory_service, main_assistant_service, pi_bridge_service, provider_service
from . import api_bp


def _limit() -> int:
    """对话列表提供有限分页，避免一次读取过多历史记录。"""
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        raise ApiProblem("validation_error", "limit 必须是整数。", 422) from None
    if not 1 <= limit <= 100:
        raise ApiProblem("validation_error", "limit 必须在 1 到 100 之间。", 422)
    return limit


@api_bp.post("/agent/turns")
@authenticated(csrf=True)
def submit_agent_turn():
    """将用户输入写入持久队列；模型或规则执行都在后台完成。"""
    payload = parse_json_body(AgentTurnInput)
    result = agent_service.submit_turn(g.auth_identity["user_id"], payload)
    response = jsonify(result)
    response.status_code = 202
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/agent/conversations")
@authenticated()
def list_agent_conversations():
    status = request.args.get("status", "active")
    return jsonify(agent_service.list_conversations(g.auth_identity["user_id"], limit=_limit(), status=status))


@api_bp.post("/agent/conversations/<uuid:conversation_id>/archive")
@authenticated(csrf=True)
def archive_agent_conversation(conversation_id: UUID):
    """软归档当前账户对话；原文保留，主对话指针由服务层清理。"""
    payload = parse_json_body(AgentConversationActionInput)
    return jsonify(agent_service.archive_conversation(
        g.auth_identity["user_id"],
        str(conversation_id),
        payload.expected_version,
    ))


@api_bp.post("/agent/conversations/<uuid:conversation_id>/restore")
@authenticated(csrf=True)
def restore_agent_conversation(conversation_id: UUID):
    payload = parse_json_body(AgentConversationActionInput)
    return jsonify(agent_service.restore_conversation(
        g.auth_identity["user_id"],
        str(conversation_id),
        payload.expected_version,
    ))


@api_bp.post("/agent/main")
@authenticated(csrf=True)
def get_main_conversation():
    """显式恢复主会话，不因刷新或新通知清空历史。"""
    return jsonify({"conversation_id": main_assistant_service.main_conversation(g.auth_identity["user_id"])})


@api_bp.get("/settings/catalog")
@authenticated()
def get_settings_catalog():
    """仅列出真实设置入口；与助手读取的目录保持一致。"""
    from ..services.agent_settings_service import get_catalog
    return jsonify(get_catalog())


@api_bp.get("/agent/preferences")
@authenticated()
def get_agent_preferences():
    return jsonify(main_assistant_service.get_preferences(g.auth_identity["user_id"]))


@api_bp.get("/agent/runtime-health")
@authenticated()
def agent_runtime_health():
    """首页据此提示额度、密钥或提供方故障；只返回错误类别，不含密钥或正文。"""
    from ..services.agent_runtime_service import runtime_health

    response = jsonify(runtime_health(g.auth_identity["user_id"]))
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.patch("/agent/preferences")
@authenticated(csrf=True)
def patch_agent_preferences():
    """独立设置页面只提交自己负责的字段，其他设置按最新值合并。"""
    payload = request.get_json(silent=True)
    allowed = set(AgentPreferenceInput.model_fields)
    if not isinstance(payload, dict) or not payload or not set(payload) <= allowed:
        raise ApiProblem('invalid_settings', '助手设置字段无效。', 422)
    current = main_assistant_service.get_preferences(g.auth_identity['user_id'])
    merged = {key: value for key, value in current.items() if key in allowed} | payload
    try:
        validated = AgentPreferenceInput.model_validate(merged)
    except ValidationError:
        raise ApiProblem('invalid_settings', '助手设置值无效。', 422) from None
    return jsonify(main_assistant_service.save_preferences(g.auth_identity['user_id'], validated))


@api_bp.put("/agent/preferences")
@authenticated(csrf=True)
def save_agent_preferences():
    return jsonify(main_assistant_service.save_preferences(g.auth_identity["user_id"], parse_json_body(AgentPreferenceInput)))


@api_bp.get("/agent/memories")
@authenticated()
def list_agent_memories():
    """长期记忆只返回当前账户内容，供设置页查看和删除。"""
    return jsonify(agent_memory_service.list_memories(g.auth_identity["user_id"], limit=100))


@api_bp.post("/agent/memories")
@authenticated(csrf=True)
def create_agent_memory():
    """用户可直接补充长期事实，不依赖模型是否主动调用记忆工具。"""
    payload = parse_json_body(AgentMemoryInput)
    response = jsonify({
        "memory": agent_memory_service.remember_memory(
            g.auth_identity["user_id"],
            conversation_id=None,
            content=payload.content,
            category=payload.category,
        )
    })
    response.status_code = 201
    return response


@api_bp.delete("/agent/memories/<uuid:memory_id>")
@authenticated(csrf=True)
def delete_agent_memory(memory_id: UUID):
    return jsonify(agent_memory_service.forget_memory(g.auth_identity["user_id"], str(memory_id)))


@api_bp.post("/agent/attachments")
@authenticated(csrf=True)
def upload_agent_attachment():
    """附件和对话共享账户权限，浏览器可拖入文件而无需先建通知候选。"""
    upload = request.files.get("file")
    if upload is None:
        raise ApiProblem("file_required", "请选择附件。", 422)
    return jsonify({"attachment": agent_material_service.upload_material(g.auth_identity["user_id"], upload)}), 201


@api_bp.get("/agent/attachments/<uuid:attachment_id>")
@authenticated()
def get_agent_attachment(attachment_id):
    response = jsonify({"attachment": agent_material_service.material_info(g.auth_identity["user_id"], str(attachment_id))})
    response.headers["Cache-Control"] = "no-store"
    return response


def _after_sequence() -> int | None:
    """增量恢复只接受非负整数；不带参数时保持全量返回。"""
    raw = request.args.get("after_sequence")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ApiProblem("validation_error", "after_sequence 必须是整数。", 422) from None
    if value < 0:
        raise ApiProblem("validation_error", "after_sequence 不能为负数。", 422)
    return value


@api_bp.get("/agent/conversations/<uuid:conversation_id>")
@authenticated()
def get_agent_conversation(conversation_id: UUID):
    return jsonify(agent_service.get_conversation(
        g.auth_identity["user_id"],
        str(conversation_id),
        after_sequence=_after_sequence(),
    ))


@api_bp.get('/agent/attachments/<uuid:attachment_id>/download')
@authenticated()
def download_agent_attachment(attachment_id):
    """生成文件与用户上传的原文件都只能由本人下载。"""
    path, material = agent_material_service.material_file(g.auth_identity['user_id'], str(attachment_id))
    response = send_file(path, as_attachment=True, download_name=material.filename, mimetype=material.media_type)
    response.headers['Cache-Control'] = 'no-store'
    return response


@api_bp.get("/agent/runs/<uuid:run_id>")
@authenticated()
def get_agent_run(run_id: UUID):
    return jsonify(agent_service.get_run(g.auth_identity["user_id"], str(run_id)))


@api_bp.post("/agent/runs/<uuid:run_id>/cancel")
@authenticated(csrf=True)
def cancel_agent_run(run_id: UUID):
    result = cancel_owned_job(
        agent_service._engine(),
        user_id=g.auth_identity["user_id"],
        job_id=run_id,
    )
    if result is None:
        raise ApiProblem("not_found", "未找到该 Agent 运行。", 404)
    if not result:
        return jsonify(pi_bridge_service.request_cancel(g.auth_identity["user_id"], str(run_id)))
    return jsonify({"status": "cancelled"})


@api_bp.post("/agent/runs/<uuid:run_id>/retry")
@authenticated(csrf=True)
def retry_agent_run(run_id: UUID):
    """失败任务复用同一条用户消息；可按需换成另一个规划模型。"""
    raw = request.get_json(silent=True) or {}
    try:
        payload = AgentRetryInput.model_validate(raw)
    except ValidationError:
        raise ApiProblem("invalid_retry", "重试参数无效。", 422) from None
    updates: dict[str, str] = {}
    if payload.profile_id is not None:
        profile = provider_service._load_profile(
            g.auth_identity["user_id"], str(payload.profile_id), require_enabled=True, allow_device=True,
        )
        if profile.purpose != "planning":
            raise ApiProblem("wrong_profile_purpose", "重试需要规划用途的模型。", 409)
        updates["profile_id"] = str(payload.profile_id)
    if payload.thinking_level is not None:
        updates["thinking_level"] = payload.thinking_level
    result = retry_owned_job(
        agent_service._engine(),
        user_id=g.auth_identity["user_id"],
        job_id=run_id,
        payload_updates=updates or None,
    )
    if result is None:
        raise ApiProblem("not_found", "未找到本次助手运行。", 404)
    if not result:
        raise ApiProblem("run_not_retryable", "只有失败的助手运行可以重试。", 409)
    return jsonify({"run_id": str(run_id), "status": "queued"})


@api_bp.get("/agent/runs/<uuid:run_id>/events")
@authenticated()
def get_agent_run_events(run_id):
    """按序号追赶流，不重复拉取完整对话。"""
    try:
        after = max(0, int(request.args.get("after", "0")))
    except ValueError:
        raise ApiProblem("validation_error", "事件序号必须是整数。", 422) from None
    return jsonify(pi_bridge_service.list_events(g.auth_identity["user_id"], str(run_id), after))


@api_bp.post("/internal/pi/tools/<name>")
def execute_pi_business_tool(name):
    """内部工具不用浏览器会话，必须携带绑定到实际运行的能力凭证。"""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme != "Bearer":
        raise ApiProblem("invalid_pi_capability", "需要内部工作凭证。", 401)
    run_id = request.headers.get("X-Pi-Run-Id", "")
    try:
        run_id = str(UUID(run_id))
    except ValueError:
        raise ApiProblem("invalid_pi_capability", "无效的内部运行标识。", 401) from None
    job = pi_bridge_service.owned_run(run_id, token)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"tool_call_id", "arguments"} or not isinstance(payload["arguments"], dict) or not isinstance(payload["tool_call_id"], str) or not 1 <= len(payload["tool_call_id"]) <= 200:
        raise ApiProblem("validation_error", "工具参数无效。", 422)
    try:
        return jsonify(pi_bridge_service.execute_tool(job, name, payload["arguments"], payload["tool_call_id"]))
    except (ValidationError, ValueError, KeyError):
        raise ApiProblem("invalid_tool_arguments", "业务工具参数未通过校验，请补充明确的字段和版本。", 422) from None


@api_bp.post("/internal/pi/token-budget/<action>")
def pi_token_budget(action):
    """逐请求预算回调只接受运行能力凭证，不接受正文指定账户或豁免身份。"""
    from ..services import provider_service, token_quota_service as quota
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme != "Bearer" or action not in {"reserve", "settle"}:
        raise ApiProblem("invalid_pi_capability", "无效的内部预算请求。", 401)
    payload = request.get_json(silent=True)
    expected = {"reservation_id", "profile_id", "prompt_bytes", "max_output_tokens"} if action == "reserve" else {"reservation_id", "usage"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ApiProblem("validation_error", "预算参数无效。", 422)
    try:
        run_id = str(UUID(request.headers.get("X-Pi-Run-Id", "")))
        identifier = str(UUID(payload["reservation_id"]))
    except (ValueError, TypeError, AttributeError):
        raise ApiProblem("validation_error", "预算标识无效。", 422) from None
    job = pi_bridge_service.owned_run(run_id, token, allow_settlement=action == "settle")
    if action == "reserve":
        profile = provider_service._load_profile(job.user_id, str(payload["profile_id"]), require_enabled=True)
        identifier = quota.reserve(profile, payload["prompt_bytes"], payload["max_output_tokens"], identifier=identifier, run_id=run_id)
        return jsonify({"reservation_id": identifier})
    quota.settle(identifier, job.user_id, payload["usage"], run_id=run_id)
    return jsonify({"status": "settled"})


@api_bp.post('/internal/pi/local/<uuid:run_id>/<uuid:profile_id>/v1/chat/completions')
def pi_local_completion(run_id, profile_id):
    """运行专属模型网关：只排到本人绑定设备，以兼容 SSE 回传给同一个 Pi 执行器。"""
    from ..services.local_completion_service import request_completion
    scheme, _, token = request.headers.get('Authorization', '').partition(' ')
    if scheme != 'Bearer':
        raise ApiProblem('invalid_pi_capability', '需要内部工作凭证。', 401)
    job = pi_bridge_service.owned_run(str(run_id), token)
    profile = provider_service._load_profile(job.user_id, str(profile_id), require_enabled=True, allow_device=True)
    if profile.execution_target != 'device' or profile.purpose != 'planning':
        raise ApiProblem('local_profile_required', '此网关仅支持设备规划模型。', 422)
    value = request_completion(profile, request.get_json(silent=True), parent_run_id=job.id)
    choice = value['choices'][0]
    # 设备返回完整结果后适配 SDK 流协议；不声称本地模型支持逐 token 实时传输。
    delta = dict(choice['message'])
    if 'tool_calls' in delta:
        delta['tool_calls'] = [dict(call, index=index) for index, call in enumerate(delta['tool_calls'])]
    chunk = {'id': 'local-' + str(uuid4()), 'object': 'chat.completion.chunk', 'created': int(time.time()),
        'model': profile.model_id, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
    end = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': choice['finish_reason']}], 'usage': value['usage']}
    return Response('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\ndata: ' + json.dumps(end, ensure_ascii=False)
        + '\n\ndata: [DONE]\n\n', mimetype='text/event-stream', headers={'Cache-Control': 'no-store'})


@api_bp.get("/agent/runs/<uuid:run_id>/stream")
@authenticated()
def stream_agent_run(run_id):
    """流式事件来自持久表，断线重连按序号恢复；不因网页断开取消后台工作。"""
    user_id = g.auth_identity["user_id"]
    try:
        after = max(0, int(request.args.get("after", "0")))
    except ValueError:
        raise ApiProblem("validation_error", "事件序号必须是整数。", 422) from None
    pi_bridge_service.list_events(user_id, str(run_id), after)
    @stream_with_context
    def events():
        cursor = after
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            batch = pi_bridge_service.list_events(user_id, str(run_id), cursor)
            for event in batch["events"]:
                cursor = event["sequence"]
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            if not batch["events"] and batch["status"] in {"succeeded", "failed", "cancelled", "expired"}:
                return
            if not batch["events"]:
                yield ": keepalive\n\n"
                time.sleep(0.25)
    return Response(events(), mimetype="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
