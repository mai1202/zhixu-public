"""采集客户端 HTTP 传输；不会把请求正文、凭证或聊天文本写入日志。"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from typing import Any
from . import __version__

# 桌面端升级同步协议字段时必须推进版本号，与服务端 DEVICE_CONTRACT_VERSION 对齐。
API_CONTRACT_VERSION = "1.0"
# Cloudflare 会拦截默认的 Python-urllib 标识；使用稳定客户端名而不是伪装浏览器。
COLLECTOR_USER_AGENT = f"zhi-xu-collector/{__version__}"


class ClientApiError(RuntimeError):
    """只暴露 HTTP 状态和服务端错误代码，避免泄漏正文或令牌。"""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def validate_server_url(value: str) -> str:
    """远程服务必须使用 HTTPS；HTTP 仅允许本机回环地址。"""
    parsed = urlsplit(value.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("服务地址必须是有效的 HTTP(S) URL")
    if parsed.query or parsed.fragment:
        raise ValueError("服务地址不能包含查询参数或片段")
    host = parsed.hostname.lower()
    if parsed.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("远程服务地址必须使用 HTTPS")
    return parsed.geturl()


class ApiClient:
    """使用 urllib 调用固定服务路径，不提供任意 URL 代理能力。"""

    def __init__(self, server_url: str, *, timeout_seconds: int = 30):
        self.server_url = validate_server_url(server_url)
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/api/v1/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("API 路径无效")
        headers = {
            "Accept": "application/json",
            "X-Notification-Contract-Version": API_CONTRACT_VERSION,
            "User-Agent": COLLECTOR_USER_AGENT,
        }
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self.server_url + path, data=body, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(10 * 1024 * 1024 + 1)
                if len(raw) > 10 * 1024 * 1024:
                    raise ClientApiError("response_too_large", response.status)
        except HTTPError as error:
            try:
                error_payload = json.loads(error.read(64 * 1024).decode("utf-8", errors="replace"))
                code = error_payload.get("error", {}).get("code", "http_error")
            except (json.JSONDecodeError, AttributeError, TypeError):
                code = "http_error"
            raise ClientApiError(str(code)[:80], int(error.code)) from None
        except (URLError, TimeoutError, OSError):
            raise ClientApiError("network_unavailable", 0) from None
        try:
            result = json.loads(raw.decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ClientApiError("invalid_response", 502) from None
        if not isinstance(result, dict):
            raise ClientApiError("invalid_response", 502)
        return result
