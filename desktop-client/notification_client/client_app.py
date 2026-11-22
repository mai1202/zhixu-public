"""编排设备配对、会话核验、加密队列、采集和幂等上报。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import time
from typing import Any, Callable
from uuid import UUID, getnode, uuid4
from zoneinfo import ZoneInfo

from .api_client import ApiClient, ClientApiError
from .local_queue import LocalQueue, QueueFullError
from .secure_store import SecretProtector, WindowsDpapiProtector
from .wechat_cli import WechatCliError, WechatHistoryReader, import_fingerprint, message_timestamp_epoch
from .attachments import prepare_attachment

SHANGHAI = ZoneInfo("Asia/Shanghai")


class SingleInstanceLock(AbstractContextManager["SingleInstanceLock"]):
    """使用操作系统文件锁确保同一数据目录只运行一个采集轮次。"""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.handle is not None:
            if self.acquired:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        return False


def default_data_dir() -> Path:
    """生产数据放在当前 Windows 用户的 LocalAppData 下。"""
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "NotificationManagement"
    return Path.home() / ".local" / "share" / "NotificationManagement"


def machine_fingerprint() -> str:
    """用本机稳定信息生成配对指纹摘要，不向服务器发送原始机器标识。"""
    pieces = [socket.gethostname(), f"node:{getnode():012x}"]
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
            machine_guid, _kind = winreg.QueryValueEx(key, "MachineGuid")
            winreg.CloseKey(key)
            pieces.append(str(machine_guid))
        except OSError:
            pass
    return hashlib.sha256("|".join(pieces).encode("utf-8")).hexdigest()


class ClientApplication:
    """把设备身份、DPAPI 密钥、群目录和待上传队列限定在一个用户数据目录。"""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        protector: SecretProtector | None = None,
        api_factory: Callable[[str], ApiClient] = ApiClient,
    ):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.protector = protector or WindowsDpapiProtector()
        self.api_factory = api_factory
        self.config_path = self.data_dir / "client.json"
        self.credentials_path = self.data_dir / "credentials.dpapi"
        self.catalog_path = self.data_dir / "chat-catalog.dpapi"
        # 每次配对使用独立队列，避免换账号后上传上一次配对留下的消息。
        queue_name = "queue.sqlite3"
        if self.config_path.is_file():
            queue_id = self.config().get("queue_id")
            if queue_id:
                queue_name = f"queue-{UUID(str(queue_id))}.sqlite3"
        self.queue = LocalQueue(self.data_dir / queue_name, self.protector)

    @staticmethod
    def _write_atomic(path: Path, value: bytes) -> None:
        """先写临时文件再替换，避免掉电留下半截配置。"""
        # 独立临时名避免并发写入相互覆盖；同步盘或杀毒软件短时占用时有限重试。
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(value)
            for attempt in range(6):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _cli_path(config):
        """内置组件随当前版本运行，用户自行指定的外部组件路径保持原样。"""
        from .updater import installation_root
        configured = str(config['cli_path'])
        path = Path(configured).expanduser().resolve()
        bundled = Path(__file__).resolve().parents[2] / 'scripts/wechat_component.py'
        if path.name == 'wechat_component.py' and path.is_relative_to(installation_root()) and bundled.is_file():
            return str(bundled)
        return configured

    def config(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            raise RuntimeError("尚未配对设备；请双击采集客户端.cmd，输入网页配对码。")
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RuntimeError("本地客户端配置损坏，请先导出诊断信息并重新配对。") from None
        if not isinstance(payload, dict):
            raise RuntimeError("本地客户端配置格式无效。")
        return payload

    def save_config(self, payload: dict[str, Any]) -> None:
        self._write_atomic(
            self.config_path,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def _save_credentials(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._write_atomic(self.credentials_path, self.protector.protect(encoded))

    def _credentials(self) -> dict[str, Any]:
        if not self.credentials_path.is_file():
            raise RuntimeError("本机凭证缺失，请在网页撤销旧设备后重新配对。")
        try:
            value = self.protector.unprotect(self.credentials_path.read_bytes())
            result = json.loads(value.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("本机凭证无法解密，请在网页撤销旧设备后重新配对。") from None
        if not isinstance(result, dict):
            raise RuntimeError("本机凭证格式无效。")
        return result

    def pair(self, *, server_url: str, code: str, device_name: str, cli_path: str | None = None) -> dict[str, Any]:
        """领取短时配对码并保存待网页确认的设备凭证。"""
        client = self.api_factory(server_url)
        fingerprint = machine_fingerprint()
        result = client.request(
            "POST",
            "/api/v1/devices/pair/claim",
            payload={"code": code.strip().upper(), "name": device_name.strip(), "fingerprint": fingerprint},
        )
        config = {
            "server_url": client.server_url,
            "device_id": result["device_id"],
            "device_name": device_name.strip(),
            "fingerprint": fingerprint,
            "cli_path": str(Path(cli_path).expanduser()) if cli_path else shutil.which("wechat-cli"),
            "paused": False,
            "autostart_enabled": True,
            "queue_id": result["device_id"],
            "created_at_epoch": int(time.time()),
        }
        self.save_config(config)
        self._save_credentials(
            {
                "access_token": result["access_token"],
                "refresh_token": result["refresh_token"],
                "access_expires_at_epoch": result["access_expires_at_epoch"],
                "refresh_expires_at_epoch": result["refresh_expires_at_epoch"],
            }
        )
        self._write_atomic(self.catalog_path, self.protector.protect(b"[]"))
        self.queue = LocalQueue(self.data_dir / f"queue-{UUID(result['device_id'])}.sqlite3", self.protector)
        return {"device_id": result["device_id"], "status": result["status"]}

    def _request_device(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        config = self.config()
        credentials = self._credentials()
        client = self.api_factory(config["server_url"])
        if timeout_seconds is not None:
            # 解除配对使用较短超时，普通同步仍保留客户端默认超时。
            client.timeout_seconds = timeout_seconds
        try:
            return client.request(method, path, token=credentials["access_token"], payload=payload)
        except ClientApiError as error:
            if error.status != 401:
                raise
        # 访问令牌过期后原子轮换；旧刷新令牌不会再次被接受。
        rotated = client.request(
            "POST",
            "/api/v1/devices/refresh",
            payload={"refresh_token": credentials["refresh_token"]},
        )
        credentials.update(
            {
                "access_token": rotated["access_token"],
                "refresh_token": rotated["refresh_token"],
                "access_expires_at_epoch": rotated["access_expires_at_epoch"],
                "refresh_expires_at_epoch": rotated["refresh_expires_at_epoch"],
            }
        )
        self._save_credentials(credentials)
        return client.request(method, path, token=credentials["access_token"], payload=payload)

    def pairing_status(self) -> str:
        """只返回 pending、active 或 revoked，不暴露凭证。"""
        result = self._request_device("GET", "/api/v1/devices/pair/status")
        return str(result.get("status", "unknown"))

    def discover_groups(self) -> int:
        """自动上报群目录元数据；上传正文的名单仍只由网页勾选决定。"""
        config = self.config()
        if not config.get("cli_path"):
            raise RuntimeError("未找到微信读取组件，请在采集窗口选择已有组件。")
        groups = WechatHistoryReader(self._cli_path(config)).discover_groups()
        # 成功解析完整目录后才更新服务端，失败时保留原目录和上传范围。
        if groups != self._catalog():
            self._request_device("POST", "/api/v1/device/chats", payload={"chats": groups})
            self._write_atomic(self.catalog_path, self.protector.protect(json.dumps(groups, ensure_ascii=False).encode("utf-8")))
        return len(groups)

    def unpair(self) -> bool:
        """暂停并尝试撤销；服务不可达时完成本机解绑，返回服务端是否确认。"""
        config = self.config()
        config.update(paused=True, unpair_pending=True)
        self.save_config(config)
        server_confirmed = True
        try:
            self._request_device("POST", "/api/v1/device/unpair", payload={}, timeout_seconds=5)
        except ClientApiError as error:
            if error.code != 'network_unavailable':
                raise
            # 断网也立即销毁本机凭证，不再保留待联网撤销的身份。
            server_confirmed = False
        self.finish_unpair()
        return server_confirmed

    def finish_unpair(self) -> None:
        """撤销后使本机凭证不可再使用，保留加密队列供诊断但禁止重新上传。"""
        config = self.config()
        config.update(paused=True, unpaired=True, unpair_pending=False)
        self.save_config(config)
        self.credentials_path.unlink(missing_ok=True)
        self.catalog_path.unlink(missing_ok=True)

    def _catalog(self) -> list[dict[str, str]]:
        if not self.catalog_path.is_file():
            return []
        value = self.protector.unprotect(self.catalog_path.read_bytes())
        result = json.loads(value.decode("utf-8"))
        return result if isinstance(result, list) else []

    def add_chat(self, chat_name: str) -> dict[str, Any]:
        """只探测用户刚输入的群名，并向服务端提交会话目录元数据。"""
        config = self.config()
        if not config.get("cli_path"):
            raise RuntimeError("未配置 wechat-cli.exe 路径；请运行 notification-client configure-cli。")
        reader = WechatHistoryReader(self._cli_path(config))
        page = reader.probe_chat(chat_name, now_epoch=int(time.time()))
        catalog = self._catalog()
        entry = {"chat_id": page.username, "chat_name": page.chat}
        catalog = [item for item in catalog if item.get("chat_id") != entry["chat_id"]]
        catalog.append(entry)
        self._write_atomic(
            self.catalog_path,
            self.protector.protect(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        )
        result = self._request_device("POST", "/api/v1/device/chats", payload={"chats": catalog})
        return {"chat_id": entry["chat_id"], "chat_name": entry["chat_name"], "version": result["version"]}

    def configure_cli(self, cli_path: str) -> None:
        """将用户指定的 CLI 可执行文件路径保存到客户端配置。"""
        config = self.config()
        executable = Path(cli_path).expanduser()
        if not executable.is_file():
            raise ValueError("指定的 wechat-cli 可执行文件不存在。")
        config["cli_path"] = str(executable.resolve())
        self.save_config(config)

    def pause(self, paused: bool) -> None:
        """本地暂停/恢复小时采集，不撤销已保存队列。"""
        config = self.config()
        config["paused"] = paused
        self.save_config(config)

    def status(self) -> dict[str, Any]:
        """返回设备连接、队列和游标摘要，不输出群正文或设备令牌。"""
        config = self.config()
        return {
            "device_id": config["device_id"],
            "device_name": config["device_name"],
            "paused": bool(config.get("paused", False)),
            "cli_configured": bool(config.get("cli_path")),
            "queue": self.queue.queue_stats(),
        }

    def _collect_chat(
        self, chat: dict[str, Any], *, end_epoch: int, settings: dict | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, int]:
        """按 ACK 游标回看有限窗口，失败页会保留且不会推进完成游标。"""
        config = self.config()
        if not config.get("cli_path"):
            raise RuntimeError("未配置 wechat-cli.exe 路径。")
        _collected, acknowledged = self.queue.cursor(str(chat["chat_id"]))
        start_epoch = max(end_epoch - 4 * 60 * 60, acknowledged - 10 * 60) if acknowledged else end_epoch - 60 * 60
        start_dt = datetime.fromtimestamp(start_epoch, SHANGHAI)
        end_dt = datetime.fromtimestamp(end_epoch, SHANGHAI)
        reader = WechatHistoryReader(self._cli_path(config))
        messages: list[str] = []
        attachment_records: list[dict | None] = []
        sender_metadata: list[dict] = []
        failed_pages: list[dict[str, Any]] = []
        attachments_enabled = bool((settings or {}).get('attachments_enabled'))
        offset = 0
        for _page_number in range(100):
            try:
                page = reader.page(
                    str(chat["chat_name"]),
                    start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    offset=offset,
                    limit=100,
                    attachments_enabled=attachments_enabled,
                )
            except WechatCliError as error:
                failed_pages.append({"offset": offset, "error_code": error.code})
                if error.code == 'attachments_component_required' and attachments_enabled:
                    # 旧组件暂不支持附件时仍保存文字，不因附件能力缺失丢失整页通知。
                    attachments_enabled = False
                    continue
                break
            if page.username != str(chat["chat_id"]):
                failed_pages.append({"offset": offset, "error_code": "cli_chat_id_mismatch"})
                break
            messages.extend(page.messages)
            sender_metadata.extend(page.message_metadata or [{} for _ in page.messages])
            attachment_records.extend(page.attachment_records or [None] * len(page.messages))
            failed_pages.extend(page.failures)
            if len(page.messages) < page.limit:
                break
            offset += page.limit
        else:
            failed_pages.append({"offset": offset, "error_code": "page_limit_reached"})

        seen: dict[str, int] = {}
        converted: list[dict[str, Any]] = []
        for index, formatted_text in enumerate(messages):
            occurrence_key = f"{message_timestamp_epoch(formatted_text)}:{formatted_text}"
            occurrence = seen.get(occurrence_key, 0)
            seen[occurrence_key] = occurrence + 1
            # 本人发送与转发均使用本人 sender ID；过滤不改变原生分页与完成游标。
            if sender_metadata[index].get('is_self') is True:
                continue
            converted.append(
                {
                    "source_key": import_fingerprint(str(chat["chat_id"]), formatted_text, occurrence),
                    "chat_id": str(chat["chat_id"]),
                    "chat_name": str(chat["chat_name"]),
                    "sent_at_epoch": message_timestamp_epoch(formatted_text),
                    "formatted_text": formatted_text,
                    "attachment_metadata": [],
                    # 本地索引只在本进程中使用，构建上传批次时明确移除。
                    "_local_attachment": attachment_records[index],
                }
            )
        complete = not failed_pages
        return converted, failed_pages, complete, start_epoch

    def _upload_pending(self, settings: dict | None = None) -> dict[str, int]:
        """逐项验证服务器回执后确认本地队列，网络错误继续保留待重试。"""
        accepted = duplicate = rejected = failed = 0
        for batch in self.queue.pending(limit=20):
            payload = {
                "batch_id": batch.batch_id,
                "window_start_epoch": batch.window_start_epoch,
                "window_end_epoch": batch.window_end_epoch,
                "complete": batch.complete,
                "failed_pages": batch.failed_pages,
                "messages": batch.messages,
            }
            try:
                result = self._request_device("POST", "/api/v1/device-sync/batches", payload=payload)
                acknowledgements = result.get("acks", [])
                self.queue.acknowledge(batch.batch_id, acknowledgements)
                accepted += int(result.get("accepted_count", 0))
                duplicate += int(result.get("duplicate_count", 0))
                rejected += int(result.get("rejected_count", 0))
                if batch.complete:
                    by_chat: dict[str, int] = {}
                    for item in batch.messages:
                        by_chat[item["chat_id"]] = batch.window_end_epoch
                    for chat_id, cursor_epoch in by_chat.items():
                        self.queue.save_acknowledged_cursor(chat_id, cursor_epoch)
            except ClientApiError as error:
                self.queue.mark_attempt(batch.batch_id, error.code)
                failed += 1
            except (OSError, ValueError, KeyError):
                self.queue.mark_attempt(batch.batch_id, "ack_or_storage_error")
                failed += 1
        attachment_uploaded = attachment_failed = 0
        if settings and settings.get('attachments_enabled'):
            allowed = {chat['chat_id'] for chat in settings.get('selected_chats', [])}
            kinds = settings.get('attachment_kinds', ['image', 'file'])
            for source in self.queue.deferred_attachment_sources(allowed, kinds, limit=5):
                payload = prepare_attachment(source['record'], source_key=source['source_key'], chat_id=source['chat_id'])
                if payload.get('data_base64'):
                    # 先持久化内容再清除本机路径；失败仍可在下一轮精确恢复。
                    self.queue.enqueue_attachment(payload)
                    self.queue.forget_attachment_source(payload['attachment_key'])
            for payload in self.queue.pending_attachments(limit=5, allowed_chats=allowed, kinds=kinds):
                if payload['chat_id'] not in allowed or payload['kind'] not in kinds:
                    continue
                try:
                    receipt = self._request_device('POST', '/api/v1/device-sync/attachments', payload=payload)
                    self.queue.acknowledge_attachment(payload['attachment_key'], receipt)
                    attachment_uploaded += 1
                except ClientApiError as error:
                    self.queue.mark_attachment_attempt(payload['attachment_key'], error.code)
                    attachment_failed += 1
                except (OSError, ValueError, KeyError):
                    self.queue.mark_attachment_attempt(payload['attachment_key'], 'attachment_receipt_error')
                    attachment_failed += 1
        return {"accepted": accepted, "duplicate": duplicate, "rejected": rejected, "failed_batches": failed,
                'attachments_uploaded': attachment_uploaded, 'failed_attachments': attachment_failed}

    def pending_upload_request(self, settings: dict | None = None) -> str | None:
        """只读取已配对设备自己的同步请求，暂停和解绑状态不被远端请求绕过。"""
        config = self.config()
        if config.get('paused') or config.get('unpaired') or not self.credentials_path.exists():
            return None
        if settings is None:
            settings = self._request_device('GET', '/api/v1/device/config').get('settings', {})
        identifier = settings.get('sync_request_id')
        return str(identifier) if identifier and identifier != config.get('last_sync_request_id') else None

    @staticmethod
    def sync_interval(settings: dict, default: int = 300) -> int:
        """只接受整分钟的有效服务端间隔，旧服务或异常配置沿用默认值。"""
        value = settings.get("sync_interval_seconds", default)
        return value if type(value) is int and 60 <= value <= 3600 and value % 60 == 0 else default

    def _sync_progress_request(self, settings: dict) -> str | None:
        """首轮部分失败后的定时重试继续更新进度，但不因此触发密集采集。"""
        identifier = self.pending_upload_request(settings)
        if identifier:
            return identifier
        initial = settings.get("initial_sync_request_id")
        if initial and initial == settings.get("sync_request_id") and settings.get("initial_sync_status") != "completed":
            return str(initial)
        return None

    def _ack_upload_request(self, settings: dict, result: dict) -> None:
        """执行后回执；网络失败保留请求供恢复，已上传内容按来源键去重。"""
        identifier = self._sync_progress_request(settings)
        if not identifier or result.get('status') not in {'completed', 'partial', 'waiting_for_chat_selection'}:
            return
        self._request_device('POST', '/api/v1/device/heartbeat', payload={
            'collected_at_epoch': int(time.time()), 'sync_request_id': identifier, 'sync_status': result['status']})
        config = self.config()
        config['last_sync_request_id'] = identifier
        self.save_config(config)

    def sync_once(self) -> dict[str, Any]:
        """拉取服务端白名单后采集、持久化、上报；未经选择的群正文不上传。"""
        with SingleInstanceLock(self.data_dir / "collector.lock") as lock:
            if not lock.acquired:
                return {"status": "already_running"}
            config = self.config()
            if config.get("paused"):
                return {"status": "paused", "queue": self.queue.queue_stats()}
            pairing_state = self.pairing_status()
            if pairing_state != "active":
                return {"status": pairing_state, "queue": self.queue.queue_stats()}
            device = self._request_device("GET", "/api/v1/device/config")
            # 执行前上报进度，让网页能够展示首轮同步；请求仅在结束时确认。
            request_id = self._sync_progress_request(device.get("settings", {}))
            progress = {"collected_at_epoch": None}
            if request_id:
                progress.update(sync_request_id=request_id, sync_status="running")
            heartbeat = self._request_device("POST", "/api/v1/device/heartbeat", payload=progress)
            selected_chats = device.get("settings", {}).get("selected_chats", [])
            if not selected_chats:
                upload_result = self._upload_pending(device.get('settings', {}))
                result = {
                    **upload_result,
                    "status": "waiting_for_chat_selection",
                    "server_time_epoch": heartbeat["server_time_epoch"],
                    "queue": self.queue.queue_stats(),
                }
                self._ack_upload_request(device.get('settings', {}), result)
                return result
            end_epoch = int(time.time())
            collected_count = 0
            failed_pages = 0
            for chat in selected_chats:
                messages, errors, complete, start_epoch = self._collect_chat(chat, end_epoch=end_epoch, settings=device.get('settings', {}))
                failed_pages += len(errors)
                collected_count += len(messages)
                chunks = [messages[index:index + 100] for index in range(0, len(messages), 100)]
                if not chunks and errors:
                    chunks = [[]]
                enqueued = False
                for chunk in chunks:
                    batch = {
                        "batch_id": str(uuid4()),
                        "window_start_epoch": start_epoch,
                        "window_end_epoch": end_epoch,
                        "complete": complete,
                        "failed_pages": errors,
                        "messages": [{key: value for key, value in message.items() if key != '_local_attachment'} for message in chunk],
                    }
                    self.queue.enqueue(batch)
                    for message in chunk:
                        record = message.get('_local_attachment')
                        if record and record['kind'] in device.get('settings', {}).get('attachment_kinds', ['image', 'file']):
                            payload = prepare_attachment(record, source_key=message['source_key'], chat_id=message['chat_id'])
                            self.queue.remember_attachment_source(payload, record)
                            self.queue.enqueue_attachment(payload)
                    enqueued = True
                if complete and (enqueued or not messages):
                    self.queue.save_collection_cursor(str(chat["chat_id"]), end_epoch)
            result = self._upload_pending(device.get('settings', {}))
            result.update(
                {
                    "status": "completed" if not result["failed_batches"] and not result.get('failed_attachments') and not failed_pages else "partial",
                    "collected_messages": collected_count,
                    "failed_pages": failed_pages,
                    "queue": self.queue.queue_stats(),
                }
            )
            self._ack_upload_request(device.get('settings', {}), result)
            return result

    def process_local_jobs(self) -> dict[str, Any]:
        """独立轮询本机模型任务；不读取聊天记录，也不接收远程任意 URL。"""
        from .local_model import LocalModelError, request_local_completion

        try:
            self.config()
        except RuntimeError:
            return {"status": "unpaired"}
        pairing = self._request_device("GET", "/api/v1/devices/pair/status")
        if pairing.get("status") != "active":
            return {"status": str(pairing.get("status", "unavailable"))}
        claimed = self._request_device("POST", "/api/v1/device/local-jobs/claim", payload={})
        job = claimed.get("job")
        if not isinstance(job, dict):
            return {"status": "idle"}
        try:
            completion = request_local_completion(job)
            result = self._request_device(
                "POST",
                "/api/v1/device/local-jobs/complete",
                payload={"job_id": job["id"], **completion},
            )
            return {"status": result.get("status", "completed"), "job_id": job["id"]}
        except LocalModelError as error:
            try:
                self._request_device(
                    "POST",
                    "/api/v1/device/local-jobs/fail",
                    payload={"job_id": job["id"], "error_code": error.code},
                )
            except (ClientApiError, OSError, ValueError, KeyError):
                # 断网时由服务端租约过期后重新入队，不丢弃任务状态。
                return {"status": "offline", "job_id": job.get("id")}
            return {"status": "failed", "job_id": job["id"], "error_code": error.code}

    def run_loop(self, *, interval_seconds: int = 300) -> None:
        """按网页间隔采集，十五秒轮询同步指令；退出即停止。"""
        last_collection = None
        current_interval = self.sync_interval({}, interval_seconds)
        while True:
            collection_due = last_collection is None or time.monotonic() >= last_collection + current_interval
            try:
                settings = self._request_device("GET", "/api/v1/device/config").get("settings", {})
                current_interval = self.sync_interval(settings, interval_seconds)
                collection_due = (last_collection is None or time.monotonic() >= last_collection + current_interval
                    or bool(self.pending_upload_request(settings)))
            except (ClientApiError, RuntimeError, OSError):
                pass  # 离线时保留现有采集周期和本机加密队列。
            for is_collection, operation in (
                (False, self.process_local_jobs),
                (True, self.sync_once if collection_due else None),
            ):
                if operation is None:
                    continue
                if is_collection:
                    # 无论成功还是失败都等待下一轮窗口，断网时不进行短间隔重复采集。
                    last_collection = time.monotonic()
                try:
                    operation()
                except (ClientApiError, WechatCliError, QueueFullError, RuntimeError, OSError) as error:
                    # 运行日志只记录错误类别，不输出响应正文、令牌或聊天内容。
                    code = getattr(error, "code", "queue_full" if isinstance(error, QueueFullError) else "sync_failed")
                    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    with (self.data_dir / "client.log").open("a", encoding="utf-8") as log:
                        log.write(f"{timestamp} {code}\n")
            time.sleep(15)
