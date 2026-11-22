"""复用设备任务队列转接主助手与审查模型，不让服务器访问用户电脑端口。"""
import json
import time
from uuid import uuid4

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..jobs.queue import enqueue_job
from ..models import AgentRunControl, Device, Job, ProviderProfile


def validate_body(body, profile):
    """仅接收有限的 Chat Completions 数据，模型、端点和预算由已授权配置决定。"""
    if not isinstance(body, dict) or len(json.dumps(body, ensure_ascii=False).encode()) > 1_000_000:
        raise ApiProblem("local_request_invalid", "本机模型请求过大或格式无效。", 422)
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 250:
        raise ApiProblem("local_request_invalid", "本机模型消息数量超出限制。", 422)
    normalized = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ApiProblem("local_request_invalid", "本机模型消息角色无效。", 422)
        content = message.get("content")
        # Pi SDK 将纯文本用户消息也编码为 text 分段，统一还原为本机协议的文本。
        if isinstance(content, list) and all(isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str) for part in content):
            content = '\n'.join(part['text'] for part in content)
        # 当前设备协议只支持文本与工具；不把图像、外链或任意多模态对象发送给本机服务。
        if content is not None and (not isinstance(content, str) or len(content) > 500_000):
            raise ApiProblem("local_request_invalid", "本机模型目前仅支持文本与工具调用。", 422)
        if set(message) - {"role", "content", "name", "tool_call_id", "tool_calls"}:
            raise ApiProblem("local_request_invalid", "本机消息包含不支持的字段。", 422)
        normalized.append({**message, "content": content})
    tools = body.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 150:
        raise ApiProblem("local_request_invalid", "本机模型工具目录无效。", 422)
    # 只转发模型推理参数，不接受路径、URL、凭证、服务器身份或输出存储位置。
    result = {key: body[key] for key in ("messages", "tools", "tool_choice", "response_format", "temperature", "top_p", "parallel_tool_calls") if key in body}
    requested = body.get("max_completion_tokens", body.get("max_tokens", profile.max_output_tokens))
    if type(requested) is not int or requested < 1:
        raise ApiProblem("local_request_invalid", "本机模型输出预算无效。", 422)
    result.update(model=profile.model_id, messages=normalized, max_tokens=min(requested, profile.max_output_tokens), stream=False)
    return result


def validate_response(value, model_id):
    """只保留 Chat Completions 必要字段；工具结果仍由服务端原有权限和审查执行。"""
    try:
        if not isinstance(value, dict): raise ValueError()
        choice = value["choices"][0]
        message = choice["message"]
        content, calls = message.get("content"), message.get("tool_calls", [])
        if content is not None and (not isinstance(content, str) or len(content) > 32000): raise ValueError()
        if not isinstance(calls, list) or len(calls) > 20: raise ValueError()
        clean_calls = []
        for call in calls:
            function = call["function"]
            if call.get("type") != "function" or not isinstance(call.get("id"), str) or not 1 <= len(call["id"]) <= 200: raise ValueError()
            if not isinstance(function.get("name"), str) or not 1 <= len(function["name"]) <= 120: raise ValueError()
            arguments = function["arguments"]
            if not isinstance(arguments, str) or len(arguments) > 32000 or not isinstance(json.loads(arguments), dict): raise ValueError()
            clean_calls.append({"id": call["id"], "type": "function", "function": {"name": function["name"], "arguments": arguments}})
        if not (content or clean_calls): raise ValueError()
        reason = choice.get("finish_reason", "tool_calls" if clean_calls else "stop")
        if reason not in {"stop", "length", "tool_calls"}: raise ValueError()
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        clean_usage = {key: count for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                       if type(count := usage.get(key)) is int and 0 <= count <= 10_000_000}
        return {"model": model_id, "choices": [{"index": 0, "message": {"role": "assistant", "content": content,
                **({"tool_calls": clean_calls} if clean_calls else {})}, "finish_reason": reason}], "usage": clean_usage}
    except (ValueError, KeyError, IndexError, TypeError):
        raise ApiProblem("local_response_invalid", "本机模型返回的文字或工具调用无效。", 422) from None


def _active(db, profile, parent_run_id):
    """每次等待都重新检查设备、配置和父运行，撤销或取消立即停止转接。"""
    device = db.get(Device, profile.device_id)
    current = db.get(ProviderProfile, profile.id)
    if (not device or device.user_id != profile.user_id or device.status != "active" or not current or not current.enabled
            or current.device_id != profile.device_id or current.model_id != profile.model_id or current.base_url != profile.base_url):
        raise ApiProblem("local_device_unavailable", "本机模型的设备或配置已停用。", 409)
    if parent_run_id:
        parent = db.get(Job, parent_run_id)
        control = db.get(AgentRunControl, parent_run_id)
        if not parent or parent.user_id != profile.user_id or parent.status not in {"running", "leased"} or (control and control.cancelled):
            raise ApiProblem("pi_run_cancelled", "本次运行已停止，本机推理不会继续执行。", 409)


def request_completion(profile, body, *, parent_run_id=None):
    """等待绑定设备领取一个有限任务；离线失败明确上报，不回退到云端。"""
    engine = current_app.extensions["notification_db_engine"]
    body = validate_body(body, profile)
    with Session(engine) as db:
        _active(db, profile, parent_run_id)
    deadline = int(time.time()) + profile.timeout_seconds + 25
    task = enqueue_job(engine, user_id=profile.user_id, kind="local_inference", max_attempts=1, priority=80,
        idempotency_key="completion:" + str(uuid4()), payload={"device_id": profile.device_id, "profile_id": profile.id,
        "task_kind": "agent_completion", "model_id": profile.model_id, "base_url": profile.base_url,
        "timeout_seconds": profile.timeout_seconds, "max_tokens": body["max_tokens"], "completion_body": body,
        "parent_run_id": parent_run_id, "deadline_epoch": deadline})
    try:
        while time.time() < deadline:
            with Session(engine) as db:
                _active(db, profile, parent_run_id)
                row = db.get(Job, task.id)
                if row.status == "succeeded":
                    return validate_response(row.result_json, profile.model_id)
                if row.status in {"failed", "cancelled", "expired"}:
                    raise ApiProblem("local_model_failed", "本机模型执行失败，请确认电脑客户端及 LM Studio 正在运行。", 503)
            time.sleep(0.2)
        raise ApiProblem("local_model_timeout", "等待本机模型超时，请确认绑定电脑在线后重试。", 503)
    finally:
        # 保留任务状态审计，及时清除临时对话副本和模型原始返回，不长期复制私有上下文。
        from .device_inference_service import _finish_failed
        with Session(engine) as db, db.begin():
            row = db.scalar(select(Job).where(Job.id == task.id).with_for_update())
            if row:
                if row.status not in {"succeeded", "failed", "cancelled", "expired"}:
                    _finish_failed(db, row, int(time.time()), "local_completion_stopped")
                row.payload_json = {key: value for key, value in row.payload_json.items() if key != "completion_body"}
                row.result_json = None
