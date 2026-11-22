"""调用本机 LM Studio 的受限 Chat Completions 接口。"""

from __future__ import annotations

import json
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class LocalModelError(RuntimeError):
    """不携带请求正文、模型回复或本机 URL 的受控错误。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    """本机模型不得通过重定向代理到其他主机。"""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def request_local_completion(task: dict[str, Any]) -> dict[str, Any]:
    """只访问 loopback LM Studio，限制任务尺寸/时长并提取 JSON 文本结果。"""
    base_url = str(task.get("base_url", ""))
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/v1")
    ):
        raise LocalModelError("local_model_url_rejected")
    agent_completion = task.get("task_kind") == "agent_completion"
    completion_body = task.get("completion_body") if agent_completion else None
    if agent_completion and (not isinstance(completion_body, dict) or set(completion_body) - {
        "model", "messages", "tools", "tool_choice", "response_format", "temperature", "top_p", "parallel_tool_calls", "max_tokens", "stream"
    }):
        raise LocalModelError("local_model_task_invalid")
    messages = completion_body.get("messages") if agent_completion else task.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= (250 if agent_completion else 8):
        raise LocalModelError("local_model_task_invalid")
    if any(
        not isinstance(message, dict)
        or message.get("role") not in ({"system", "user", "assistant", "tool"} if agent_completion else {"system", "user", "assistant"})
        or (not agent_completion and not isinstance(message.get("content"), str))
        or (message.get("content") is not None and not isinstance(message.get("content"), str))
        or len(message.get("content") or "") > (500_000 if agent_completion else 50_000)
        for message in messages
    ):
        raise LocalModelError("local_model_task_invalid")
    try:
        timeout = max(3, min(120, int(task.get("timeout_seconds", 30))))
        max_tokens = max(32, min(8192, int(task.get("max_tokens", 1200))))
    except (TypeError, ValueError):
        raise LocalModelError("local_model_task_invalid") from None
    try:
        # 工具调用只返回服务器，由原有权限/审查执行；客户端不执行模型给出的任何代码或命令。
        request_body = {**completion_body, "model": str(task.get("model_id", ""))[:200], "messages": messages,
                        "stream": False, "max_tokens": max_tokens} if agent_completion else {
                "model": str(task.get("model_id", ""))[:200],
                "messages": messages,
                "temperature": 0,
                "max_tokens": max_tokens,
                "stream": False,
                "response_format": {"type": "json_object"},
            }
        body = json.dumps(
            request_body,
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise LocalModelError("local_model_task_invalid") from None
    if len(body) > (1_000_000 if agent_completion else 250_000):
        raise LocalModelError("local_model_task_too_large")

    request = Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise LocalModelError("local_model_response_too_large")
        payload = json.loads(raw.decode("utf-8"))
    except HTTPError:
        raise LocalModelError("local_model_http_error") from None
    except (TimeoutError, socket.timeout):
        raise LocalModelError("local_model_timeout") from None
    except (URLError, OSError):
        raise LocalModelError("local_model_unavailable") from None
    except (UnicodeError, json.JSONDecodeError):
        raise LocalModelError("local_model_invalid_response") from None

    try:
        choice = payload["choices"][0]
        # 主助手需要完整工具调用包；既有连接测试/提取继续返回原文本，保持设备协议兼容。
        content = json.dumps({"model": payload.get("model"), "choices": [choice], "usage": payload.get("usage", {})}, ensure_ascii=False) if agent_completion else choice["message"]["content"]
        if not isinstance(content, str) or not content or len(content) > 40_000:
            raise ValueError
        usage = payload.get("usage") if isinstance(payload, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        return {
            "content": content,
            "model_id": str(payload.get("model") or task.get("model_id", ""))[:200],
            "prompt_tokens": usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None,
            "completion_tokens": usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None,
        }
    except (KeyError, IndexError, TypeError, ValueError):
        raise LocalModelError("local_model_invalid_response") from None
