"""适配 wechat-cli 0.2.4 history JSON；不假定消息含稳定原始 ID。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import locale
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any
from zoneinfo import ZoneInfo


_TIMESTAMP = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?!\d)")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _child_environment() -> dict[str, str]:
    """让 Python 子组件在管道中也输出 UTF-8，避免中文 Windows 按本地代码页写出 GBK。"""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def decode_json_payload(raw: bytes) -> Any:
    """按子进程实际使用的编码解码 JSON，兼容 UTF-8、GB18030 和系统本地代码页。"""
    candidates = ["utf-8-sig", "gb18030", locale.getpreferredencoding(False)]
    seen: set[str] = set()
    for encoding in candidates:
        if not encoding:
            continue
        normalized = encoding.lower().replace("-", "").replace("_", "")
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            text = raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    raise WechatCliError("invalid_json")


class WechatCliError(RuntimeError):
    """命令失败时只保留不含聊天正文的分类错误。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HistoryPage:
    """经契约校验的一页格式化聊天记录。"""

    chat: str
    username: str
    is_group: bool
    messages: list[str]
    failures: list[dict[str, Any]]
    offset: int
    limit: int
    attachment_records: list[dict[str, Any] | None] = field(default_factory=list)
    # 身份未知时保留 None，兼容旧组件，禁止把昵称当作账号身份。
    message_metadata: list[dict[str, Any]] = field(default_factory=list)


def parse_history_json(raw: str, *, expected_offset: int, expected_limit: int) -> HistoryPage:
    """只读取已核实的 history 外层字段，任何不完整页都作为失败处理。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise WechatCliError("invalid_json") from None
    if not isinstance(payload, dict):
        raise WechatCliError("invalid_contract")
    messages = payload.get("messages")
    failures = payload.get("failures")
    if (
        not isinstance(payload.get("chat"), str)
        or not isinstance(payload.get("username"), str)
        or not isinstance(payload.get("is_group"), bool)
        or not isinstance(messages, list)
        or any(not isinstance(message, str) or not message.strip() for message in messages)
    ):
        raise WechatCliError("invalid_contract")
    if payload.get("offset") != expected_offset or payload.get("limit") != expected_limit:
        raise WechatCliError("pagination_mismatch")
    if failures is None:
        failures = []
    if not isinstance(failures, list):
        raise WechatCliError("invalid_failures")
    normalized_failures: list[dict[str, Any]] = []
    attachments = payload.get('attachment_records', [])
    if not isinstance(attachments, list) or (attachments and len(attachments) != len(messages)) or any(
        item is not None and not isinstance(item, dict) for item in attachments
    ):
        raise WechatCliError('invalid_attachment_contract')
    metadata = payload.get('message_metadata', [])
    if not isinstance(metadata, list) or (metadata and len(metadata) != len(messages)) or any(
        not isinstance(item, dict) or set(item) != {'is_self'} or (item['is_self'] is not None and type(item['is_self']) is not bool)
        for item in metadata
    ):
        raise WechatCliError('invalid_sender_contract')
    for failure in failures:
        if isinstance(failure, dict):
            normalized_failures.append({"offset": expected_offset, "error_code": "cli_page_failure"})
        else:
            normalized_failures.append({"offset": expected_offset, "error_code": "cli_page_failure"})
    return HistoryPage(
        chat=payload["chat"],
        username=payload["username"],
        is_group=payload["is_group"],
        messages=messages,
        failures=normalized_failures,
        offset=expected_offset,
        limit=expected_limit,
        attachment_records=attachments,
        message_metadata=metadata,
    )


def message_timestamp_epoch(formatted_text: str) -> int | None:
    """从原格式化文本提取明确秒级时间；无法解析时保留为未知。"""
    match = _TIMESTAMP.search(formatted_text)
    if match is None:
        return None
    try:
        local = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_SHANGHAI)
    except ValueError:
        return None
    return int(local.astimezone(timezone.utc).timestamp())


def import_fingerprint(chat_id: str, formatted_text: str, occurrence: int = 0) -> str:
    """标记无原始消息 ID 的导入指纹，不冒充微信消息 ID。"""
    timestamp = message_timestamp_epoch(formatted_text)
    canonical = json.dumps(
        ["wechat-cli-import-v1", chat_id, timestamp, formatted_text, occurrence],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "import-fingerprint:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WechatHistoryReader:
    """以参数数组、shell=False 和有限输出执行单个已授权群的只读 history。"""

    def __init__(self, executable: str | Path, *, timeout_seconds: int = 120, max_output_bytes: int = 8 * 1024 * 1024):
        self.executable = str(Path(executable).expanduser())
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def page(
        self,
        chat_name: str,
        *,
        start_time: str,
        end_time: str,
        offset: int,
        limit: int = 100,
        attachments_enabled: bool = False,
    ) -> HistoryPage:
        """只在 argument list 中传递群名、时间和分页参数。"""
        if not chat_name.strip() or len(chat_name) > 160:
            raise WechatCliError("invalid_chat_name")
        args = [
            self.executable,
            "history",
            chat_name,
            "--start-time",
            start_time,
            "--end-time",
            end_time,
            "--limit",
            str(limit),
            "--offset",
            str(offset),
            "--format",
            "json",
        ]
        if attachments_enabled:
            if Path(self.executable).suffix.lower() != '.py':
                raise WechatCliError('attachments_component_required')
            args.append('--collect-attachments')
        return parse_history_json(
            json.dumps(self._json_command(args), ensure_ascii=False),
            expected_offset=offset,
            expected_limit=limit,
        )

    def discover_groups(self) -> list[dict[str, str]]:
        """核实 contacts 契约后只取群名称与标识，不读取 sessions 的最近消息。"""
        contacts = self._json_command([
            self.executable, "contacts", "--query", "@chatroom", "--limit", "201", "--format", "json",
        ])
        if not isinstance(contacts, list) or len(contacts) > 200:
            raise WechatCliError("group_catalog_limit" if isinstance(contacts, list) else "invalid_contacts_contract")
        groups: dict[str, dict[str, str]] = {}
        for item in contacts:
            if not isinstance(item, dict) or not isinstance(item.get("username"), str):
                raise WechatCliError("invalid_contacts_contract")
            chat_id = item["username"]
            if not chat_id.endswith("@chatroom"):
                continue
            name = item.get("remark") or item.get("nick_name") or chat_id
            if not isinstance(name, str) or not name.strip() or len(name) > 160 or len(chat_id) > 256:
                raise WechatCliError("invalid_contacts_contract")
            groups[chat_id] = {"chat_id": chat_id, "chat_name": name}
        return list(groups.values())

    def _json_command(self, args: list[str]) -> Any:
        """目录和历史命令复用超时、大小限制与无 shell 的执行边界。"""
        if Path(args[0]).suffix.lower() == '.py':
            # 发布版组件以同一套自带 Python 执行，不依赖全局命令或脚本关联。
            python = Path(sys.executable).with_name('python.exe')
            args = [str(python if python.is_file() else sys.executable), *args]
        try:
            process = subprocess.Popen(
                args,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_child_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            raise WechatCliError("executable_unavailable") from None
        output: list[bytes] = []
        output_size = 0
        exceeded = threading.Event()

        def read_limited_output() -> None:
            nonlocal output_size
            assert process.stdout is not None
            while chunk := process.stdout.read(64 * 1024):
                output_size += len(chunk)
                if output_size > self.max_output_bytes:
                    exceeded.set()
                    process.kill()
                    continue
                output.append(chunk)

        reader_thread = threading.Thread(target=read_limited_output, daemon=True)
        reader_thread.start()
        try:
            return_code = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            reader_thread.join()
            raise WechatCliError("timeout") from None
        reader_thread.join()
        if exceeded.is_set():
            raise WechatCliError("output_too_large")
        if return_code != 0:
            raise WechatCliError({2: "component_unavailable", 3: "wechat_not_ready"}.get(return_code, "cli_failed"))
        return decode_json_payload(b"".join(output))

    def probe_chat(self, chat_name: str, *, now_epoch: int) -> HistoryPage:
        """读取用户刚指定会话最近至多一条记录以取到 CLI 返回的稳定会话标识。"""
        end = datetime.fromtimestamp(now_epoch, _SHANGHAI)
        start = end.replace(second=0, microsecond=0)
        page = self.page(
            chat_name,
            start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
            offset=0,
            limit=1,
        )
        if not page.username:
            raise WechatCliError("chat_id_unavailable")
        return page
