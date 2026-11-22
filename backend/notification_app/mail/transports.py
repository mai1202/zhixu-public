from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
import json
import base64
import smtplib
import ssl
import socket
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import time


class MailTransport(Protocol):
    def send_verification_code(
        self,
        recipient: str,
        challenge_id: str,
        code: str,
        expires_in_seconds: int,
    ) -> None: ...

    def send_message(self, recipient: str, subject: str, body_text: str, *, attachments=()) -> None: ...


class MailDeliveryError(RuntimeError):
    """投递错误只暴露受控分类，区分可安全重试与结果未知。"""

    def __init__(self, code: str, *, state: str = "failed", retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.state = state
        self.retryable = retryable


class MailNotConfigured(RuntimeError):
    """部署尚未配置可用的运营发信通道。"""


class DisabledMailTransport:
    def send_verification_code(
        self,
        recipient: str,
        challenge_id: str,
        code: str,
        expires_in_seconds: int,
    ) -> None:
        raise MailNotConfigured("没有配置运营发信通道。")

    def send_message(self, recipient: str, subject: str, body_text: str, *, attachments=()) -> None:
        raise MailNotConfigured("没有配置运营发信通道。")


@dataclass(frozen=True)
class CapturedCode:
    recipient: str
    challenge_id: str
    code: str
    expires_in_seconds: int


class FakeMailTransport:
    """仅测试和显式本机开发使用；不把验证码暴露给 HTTP 响应。"""

    def __init__(self) -> None:
        self.sent_codes: list[CapturedCode] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.sent_attachments: list[tuple] = []

    def send_verification_code(
        self,
        recipient: str,
        challenge_id: str,
        code: str,
        expires_in_seconds: int,
    ) -> None:
        self.sent_codes.append(
            CapturedCode(recipient, challenge_id, code, expires_in_seconds)
        )
        self.send_message(
            recipient,
            "通知管理邮箱验证码",
            f"你的邮箱验证码是：{code}。验证码将在 {max(1, expires_in_seconds // 60)} 分钟后失效。",
        )

    def send_message(self, recipient: str, subject: str, body_text: str, *, attachments=()) -> None:
        self.sent_messages.append((recipient, subject, body_text))
        self.sent_attachments.append(tuple(attachments))


def _mailbox(value: str) -> str:
    """拒绝邮件头注入，并要求配置项是单一邮箱地址。"""
    if not value or any(char in value for char in "\r\n"):
        raise MailNotConfigured("邮件地址配置无效。")
    _display, parsed = parseaddr(value)
    if not parsed or "@" not in parsed or parsed.count("@") != 1:
        raise MailNotConfigured("邮件地址配置无效。")
    return parsed


class SmtpMailTransport:
    """管理员配置的 SMTP 适配器；TLS 校验开启且不预设学校邮箱主机。"""

    def __init__(self, config) -> None:
        self.host = str(config.get("SMTP_HOST", "")).strip()
        self.port = int(config.get("SMTP_PORT", 587))
        self.username = str(config.get("SMTP_AUTH_USER", "")).strip()
        self.password = str(config.get("SMTP_AUTH_SECRET", ""))
        self.sender = _mailbox(str(config.get("SMTP_FROM", "")).strip())
        self.timeout = int(config.get("SMTP_TIMEOUT_SECONDS", 20))
        self.use_ssl = bool(config.get("SMTP_USE_SSL", False))
        self.starttls = bool(config.get("SMTP_STARTTLS", True))
        if not self.host or any(char in self.host for char in "\r\n/@"):
            raise MailNotConfigured("请配置 SMTP_HOST。")
        if not 1 <= self.port <= 65535 or not 1 <= self.timeout <= 120:
            raise MailNotConfigured("SMTP 端口或超时配置无效。")
        if self.use_ssl == self.starttls:
            raise MailNotConfigured("SMTP 必须只选择隐式 TLS 或 STARTTLS。")
        if not self.username or not self.password:
            raise MailNotConfigured("请配置 SMTP_AUTH_USER 和 SMTP_AUTH_SECRET。")

    def send_verification_code(self, recipient: str, challenge_id: str, code: str, expires_in_seconds: int) -> None:
        self.send_message(
            recipient,
            "通知管理邮箱验证码",
            f"你的邮箱验证码是：{code}。验证码将在 {max(1, expires_in_seconds // 60)} 分钟后失效。",
        )

    def send_message(self, recipient: str, subject: str, body_text: str, *, attachments=()) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = _mailbox(recipient)
        if not subject or any(char in subject for char in "\r\n"):
            raise MailDeliveryError("invalid_message")
        message["Subject"] = subject
        message.set_content(body_text)
        # 二进制附件按 MIME 编码发送，文件名仅取已校验的用户私有材料元数据。
        for filename, media_type, content in attachments:
            major, minor = media_type.split('/', 1) if '/' in media_type else ('application', 'octet-stream')
            message.add_attachment(content, maintype=major, subtype=minor, filename=filename)
        context = ssl.create_default_context()
        client = None
        try:
            if self.use_ssl:
                client = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(self.username, self.password)
            client.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise MailDeliveryError("smtp_auth_rejected") from None
        except smtplib.SMTPResponseException as error:
            transient = 400 <= int(error.smtp_code) < 500
            raise MailDeliveryError("smtp_rejected", retryable=transient) from None
        except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError, OSError):
            # 连接在发送期间中断时无法知道对端是否已接收，避免自动重发造成重复。
            raise MailDeliveryError("smtp_delivery_unknown", state="unknown") from None
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    pass


class GraphMailTransport:
    """Microsoft Graph delegated Mail.Send 适配器，轮换 refresh token 加密留存。"""

    def __init__(
        self,
        config,
        *,
        load_refresh_token: Callable[[], str | None],
        save_refresh_token: Callable[[str], None],
        opener=urlopen,
    ) -> None:
        self.client_id = str(config.get("GRAPH_CLIENT_ID", "")).strip()
        self.client_secret = str(config.get("GRAPH_CLIENT_SECRET", ""))
        self.tenant = str(config.get("GRAPH_TENANT_ID", "consumers")).strip()
        self.bootstrap_token = str(config.get("GRAPH_REFRESH_TOKEN", ""))
        self.timeout = int(config.get("GRAPH_TIMEOUT_SECONDS", 20))
        self._load_refresh_token = load_refresh_token
        self._save_refresh_token = save_refresh_token
        self._opener = opener
        self._refresh_token = ""
        self._access_token = ""
        self._expires_at = 0.0

    def _request_json(self, request: Request) -> tuple[int, dict]:
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise MailDeliveryError("graph_response_too_large")
                return int(response.status), json.loads(raw.decode("utf-8") or "{}")
        except HTTPError as error:
            status = int(error.code)
            raise MailDeliveryError(
                "graph_http_rejected",
                retryable=status == 429 or 500 <= status <= 599,
            ) from None
        except (TimeoutError, socket.timeout, URLError, OSError):
            raise MailDeliveryError("graph_delivery_unknown", state="unknown") from None
        except (UnicodeError, json.JSONDecodeError):
            raise MailDeliveryError("graph_invalid_response") from None

    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        if not self.client_id:
            raise MailNotConfigured("请配置 GRAPH_CLIENT_ID。")
        if not self._refresh_token:
            self._refresh_token = self._load_refresh_token() or self.bootstrap_token
        if not self._refresh_token:
            raise MailNotConfigured("请先完成 Microsoft Graph delegated OAuth 授权。")
        fields = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "scope": "offline_access Mail.Send",
        }
        if self.client_secret:
            fields["client_secret"] = self.client_secret
        endpoint = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"
        request = Request(
            endpoint,
            data=urlencode(fields).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            _status, response = self._request_json(request)
        except MailDeliveryError as error:
            if error.state == "unknown":
                raise MailDeliveryError("graph_oauth_unknown", state="unknown") from None
            raise MailDeliveryError("graph_oauth_refresh_failed", retryable=error.retryable) from None
        access_token = response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise MailDeliveryError("graph_oauth_refresh_failed")
        rotated = response.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            self._refresh_token = rotated
            self._save_refresh_token(rotated)
        self._access_token = access_token
        self._expires_at = time.time() + max(60, int(response.get("expires_in", 3600)))
        return access_token

    def send_verification_code(self, recipient: str, challenge_id: str, code: str, expires_in_seconds: int) -> None:
        self.send_message(
            recipient,
            "通知管理邮箱验证码",
            f"你的邮箱验证码是：{code}。验证码将在 {max(1, expires_in_seconds // 60)} 分钟后失效。",
        )

    def send_message(self, recipient: str, subject: str, body_text: str, *, attachments=()) -> None:
        if not subject or any(char in subject for char in "\r\n"):
            raise MailDeliveryError("invalid_message")
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body_text},
                "toRecipients": [{"emailAddress": {"address": _mailbox(recipient)}}],
            },
            "saveToSentItems": True,
        }
        if attachments:
            # Graph 的直接发信接口使用 fileAttachment，不把本机文件路径提交给远端。
            if sum(len(item[2]) for item in attachments) > 3 * 1024 * 1024:
                raise MailDeliveryError('graph_attachment_too_large')
            payload['message']['attachments'] = [dict(
                **{'@odata.type': '#microsoft.graph.fileAttachment'}, name=filename,
                contentType=media_type, contentBytes=base64.b64encode(content).decode('ascii'))
                for filename, media_type, content in attachments]
        request = Request(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                if int(response.status) != 202:
                    raise MailDeliveryError("graph_send_rejected", retryable=500 <= int(response.status) < 600)
                response.read(1024)
        except HTTPError as error:
            status = int(error.code)
            raise MailDeliveryError("graph_send_rejected", retryable=status == 429 or 500 <= status < 600) from None
        except MailDeliveryError:
            raise
        except (TimeoutError, socket.timeout, URLError, OSError):
            raise MailDeliveryError("graph_delivery_unknown", state="unknown") from None
