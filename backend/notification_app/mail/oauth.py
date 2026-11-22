import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GraphOAuthError(RuntimeError):
    """只携带 Microsoft OAuth 的受控错误分类。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _json_request(request: Request, timeout: int, opener=urlopen) -> dict:
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise GraphOAuthError("oauth_response_too_large")
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise GraphOAuthError("oauth_invalid_response")
            return body
    except HTTPError as error:
        try:
            body = json.loads(error.read(100_000).decode("utf-8") or "{}")
        except Exception:
            body = {}
        raise GraphOAuthError(str(body.get("error", "oauth_http_error"))[:80]) from None
    except (URLError, TimeoutError, OSError):
        raise GraphOAuthError("oauth_network_error") from None
    except (UnicodeError, json.JSONDecodeError):
        raise GraphOAuthError("oauth_invalid_response") from None


def authorize_with_device_code(
    *,
    client_id: str,
    tenant: str,
    client_secret: str = "",
    timeout: int = 20,
    opener=urlopen,
    sleep=__import__("time").sleep,
) -> tuple[str, str]:
    """由管理员在浏览器完成 delegated Mail.Send 授权，令牌不打印也不落明文文件。"""
    if not client_id or not tenant or any(char in tenant for char in "/\\?#"):
        raise GraphOAuthError("oauth_configuration_invalid")
    root = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    device_request = Request(
        f"{root}/devicecode",
        data=urlencode({"client_id": client_id, "scope": "offline_access Mail.Send"}).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    device = _json_request(device_request, timeout, opener)
    device_code = device.get("device_code")
    interval = max(1, int(device.get("interval", 5)))
    expires_in = max(1, min(1800, int(device.get("expires_in", 900))))
    if not isinstance(device_code, str) or not device_code:
        raise GraphOAuthError("oauth_device_code_missing")
    print_text = device.get("message")
    if not isinstance(print_text, str) or not print_text:
        raise GraphOAuthError("oauth_device_message_missing")
    print(print_text)

    import time

    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        sleep(interval)
        fields = {
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        }
        if client_secret:
            fields["client_secret"] = client_secret
        token_request = Request(
            f"{root}/token",
            data=urlencode(fields).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            token = _json_request(token_request, timeout, opener)
        except GraphOAuthError as error:
            if error.code == "authorization_pending":
                continue
            if error.code == "slow_down":
                interval += 5
                continue
            raise
        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise GraphOAuthError("oauth_refresh_token_missing")
        return refresh_token, str(token.get("scope", ""))
    raise GraphOAuthError("oauth_device_code_expired")
