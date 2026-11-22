import hashlib
import hmac
import json
import secrets
import time
from uuid import uuid4

from flask import current_app
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..models import (
    Device,
    DeviceCredential,
    DevicePairing,
    SourceDocument,
    SyncBatch,
    SyncItem,
)
from ..schemas.business import CreateSourceInput
from ..schemas.devices import DeviceChatsInput, DeviceSettingsInput, SyncBatchInput
from .business_service import create_source

PAIR_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
DEVICE_SCOPES = ["heartbeat", "ingest", "own-local-jobs"]
ACCESS_TTL_SECONDS = 60 * 60
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60


def _initial_settings():
    """新设备默认不采集，用户勾选群聊后开始；不修改已有设备配置。"""
    return {'available_chats': [], 'selected_chats': [], 'chat_selection_mode': 'selected',
            'excluded_chat_ids': [], 'attachments_enabled': False,
            'attachment_kinds': ['image', 'file'], 'attachment_auto_notice': True}


def _engine():
    return current_app.extensions["notification_db_engine"]


def _now() -> int:
    return int(time.time())


def _keyed_digest(purpose: str, value: str) -> str:
    key = current_app.config.get("AUTH_HMAC_KEY")
    if not isinstance(key, str) or len(key.encode("utf-8")) < 32:
        raise ApiProblem("auth_not_configured", "服务尚未配置设备认证密钥。", 503)
    return hmac.new(key.encode("utf-8"), f"{purpose}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _new_tokens(device_id: str, now: int) -> tuple[dict[str, object], DeviceCredential]:
    access_token = secrets.token_urlsafe(48)
    refresh_token = secrets.token_urlsafe(64)
    credential = DeviceCredential(
        id=str(uuid4()),
        device_id=device_id,
        access_digest=_keyed_digest("device-access", access_token),
        refresh_digest=_keyed_digest("device-refresh", refresh_token),
        scopes_json=DEVICE_SCOPES,
        access_expires_at_epoch=now + ACCESS_TTL_SECONDS,
        refresh_expires_at_epoch=now + REFRESH_TTL_SECONDS,
        created_at_epoch=now,
        refreshed_at_epoch=None,
        revoked_at_epoch=None,
    )
    return (
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_expires_at_epoch": credential.access_expires_at_epoch,
            "refresh_expires_at_epoch": credential.refresh_expires_at_epoch,
            "token_type": "Bearer",
            "scopes": list(DEVICE_SCOPES),
        },
        credential,
    )


def create_pairing_code(user_id: str) -> dict[str, object]:
    """生成短时配对码，并使同一用户未使用的旧码失效。"""
    now = _now()
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(10))
    pairing_id = str(uuid4())
    expires_at_epoch = now + 10 * 60
    pairing = DevicePairing(
        id=pairing_id,
        user_id=user_id,
        code_digest=_keyed_digest("device-pairing", code),
        expires_at_epoch=expires_at_epoch,
        claimed_device_id=None,
        created_at_epoch=now,
    )
    with DbSession(_engine()) as db, db.begin():
        db.execute(
            update(DevicePairing)
            .where(DevicePairing.user_id == user_id, DevicePairing.claimed_device_id.is_(None))
            .values(expires_at_epoch=now)
        )
        db.add(pairing)
    return {"pairing_id": pairing_id, "code": code, "expires_at_epoch": expires_at_epoch}


def claim_pairing(*, code: str, name: str, fingerprint: str) -> dict[str, object]:
    """本机只凭短时配对码领取待确认设备和一次性返回的受限凭证。"""
    now = _now()
    code_digest = _keyed_digest("device-pairing", code.upper())
    device_id = str(uuid4())
    tokens, credential = _new_tokens(device_id, now)
    try:
        with DbSession(_engine()) as db:
            with db.begin():
                pairing = db.scalar(
                    select(DevicePairing).where(DevicePairing.code_digest == code_digest)
                )
                if pairing is None or pairing.expires_at_epoch <= now or pairing.claimed_device_id is not None:
                    raise ApiProblem("pairing_code_invalid", "配对码无效、过期或已使用。", 400)
                existing = db.scalar(
                    select(Device).where(Device.user_id == pairing.user_id, Device.fingerprint == fingerprint)
                )
                if existing is not None and existing.status != "revoked":
                    raise ApiProblem("device_already_paired", "该设备已经配对；请从网页撤销旧设备后重试。", 409)
                if existing is None:
                    device = Device(
                        id=device_id,
                        user_id=pairing.user_id,
                        name=name.strip(),
                        fingerprint=fingerprint,
                        status="pending",
                        settings_json=_initial_settings(),
                        version=1,
                        last_heartbeat_epoch=None,
                        last_collect_epoch=None,
                        last_upload_epoch=None,
                        created_at_epoch=now,
                        confirmed_at_epoch=None,
                        revoked_at_epoch=None,
                    )
                else:
                    # 重配撤销设备时保留历史批次，但旧凭证仍维持撤销状态。
                    device = existing
                    device_id = existing.id
                    credential.device_id = device_id
                    device.name = name.strip()
                    device.status = "pending"
                    device.settings_json = _initial_settings()
                    device.version += 1
                    device.confirmed_at_epoch = None
                    device.revoked_at_epoch = None
                    device.last_heartbeat_epoch = None
                    device.last_collect_epoch = None
                    device.last_upload_epoch = None
                db.add(device)
                db.flush()
                claimed = db.execute(
                    update(DevicePairing)
                    .where(
                        DevicePairing.id == pairing.id,
                        DevicePairing.claimed_device_id.is_(None),
                        DevicePairing.expires_at_epoch > now,
                    )
                    .values(claimed_device_id=device_id)
                )
                if claimed.rowcount != 1:
                    raise ApiProblem("pairing_code_invalid", "配对码无效、过期或已使用。", 400)
                db.add(credential)
        return {"device_id": device_id, "status": "pending", **tokens}
    except IntegrityError:
        raise ApiProblem("device_already_paired", "该设备正在配对或已有有效绑定。", 409) from None


def _owned_device(db: DbSession, user_id: str, device_id: str) -> Device:
    device = db.scalar(select(Device).where(Device.id == device_id, Device.user_id == user_id))
    if device is None:
        raise ApiProblem("not_found", "未找到该设备。", 404)
    return device


def _device_public(device: Device) -> dict[str, object]:
    return {
        "id": device.id,
        "name": device.name,
        "fingerprint_hint": device.fingerprint[-8:],
        "status": device.status,
        "settings": device.settings_json,
        "version": device.version,
        "last_heartbeat_epoch": device.last_heartbeat_epoch,
        "last_collect_epoch": device.last_collect_epoch,
        "last_upload_epoch": device.last_upload_epoch,
        "created_at_epoch": device.created_at_epoch,
        "confirmed_at_epoch": device.confirmed_at_epoch,
    }


def list_devices(user_id: str) -> dict[str, object]:
    with DbSession(_engine()) as db:
        devices = db.scalars(
            select(Device).where(Device.user_id == user_id).order_by(Device.created_at_epoch.desc())
        ).all()
    return {"items": [_device_public(device) for device in devices]}


def confirm_device(user_id: str, device_id: str) -> dict[str, object]:
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        device = _owned_device(db, user_id, device_id)
        if device.status != "pending":
            raise ApiProblem("device_state_conflict", "只有待确认设备可以配对确认。", 409)
        device.status = "active"
        device.confirmed_at_epoch = now
        # 确认绑定后持久化首轮同步请求，客户端重启或离线也不会丢失。
        request_id = str(uuid4())
        device.settings_json = {**(device.settings_json or {}),
            "sync_request_id": request_id, "sync_requested_at_epoch": now,
            "initial_sync_request_id": request_id, "initial_sync_status": "pending"}
        device.version += 1
    return _device_public(device)


def update_device_settings(user_id: str, device_id: str, payload: DeviceSettingsInput) -> dict[str, object]:
    with DbSession(_engine()) as db:
        device = _owned_device(db, user_id, device_id)
        if device.status == "revoked" or device.version != payload.expected_version:
            raise ApiProblem("version_conflict", "设备设置已变化或设备已撤销，请刷新后重试。", 409)
        available = {
            str(item.get("chat_id")): item
            for item in (device.settings_json or {}).get("available_chats", [])
            if isinstance(item, dict)
        }
        if any(chat.chat_id not in available for chat in payload.selected_chats):
            raise ApiProblem("unknown_chat", "只能选择本机已发现的会话，请刷新目录后重试。", 422)
        new_settings = {
            **(device.settings_json or {}),
            "available_chats": list(available.values()),
            "selected_chats": [
                {"chat_id": chat.chat_id, "chat_name": available[chat.chat_id]["chat_name"]}
                for chat in payload.selected_chats
            ],
            "attachments_enabled": payload.attachments_enabled,
        }
        if payload.sync_interval_seconds is not None:
            new_settings["sync_interval_seconds"] = payload.sync_interval_seconds
        # 旧客户端未提交新增字段时保留网页已保存的附件授权。
        if payload.attachment_kinds is not None:
            new_settings['attachment_kinds'] = list(dict.fromkeys(payload.attachment_kinds))
        if payload.attachment_auto_notice is not None:
            new_settings['attachment_auto_notice'] = payload.attachment_auto_notice
        if payload.chat_selection_mode is not None:
            new_settings['chat_selection_mode'] = payload.chat_selection_mode
        else:
            # 旧网页和 Agent 逐项提交群范围时保持原语义，不被新设备默认“全部群”覆盖。
            new_settings['chat_selection_mode'] = 'selected'
        if payload.excluded_chat_ids is not None:
            if any(chat_id not in available for chat_id in payload.excluded_chat_ids):
                raise ApiProblem('unknown_chat', '排除范围只能包含已发现的群。', 422)
            new_settings['excluded_chat_ids'] = list(dict.fromkeys(payload.excluded_chat_ids))
        if new_settings.get('chat_selection_mode') == 'all':
            excluded = set(new_settings.get('excluded_chat_ids', []))
            new_settings['selected_chats'] = [chat for chat in available.values() if chat['chat_id'] not in excluded]
        # 初次未选群或失败后，保存授权范围立即重新同步，不必等待五分钟。
        if payload.selected_chats and new_settings.get("initial_sync_status") in {"pending", "running", "waiting_for_chat_selection", "partial"}:
            request_id = str(uuid4())
            new_settings.update(sync_request_id=request_id, sync_requested_at_epoch=_now(),
                initial_sync_request_id=request_id, initial_sync_status="pending")
        next_version = payload.expected_version + 1
        result = db.execute(
            update(Device)
            .where(
                Device.id == device_id,
                Device.user_id == user_id,
                Device.version == payload.expected_version,
                Device.status != "revoked",
            )
            .values(settings_json=new_settings, version=next_version)
        )
        db.commit()
        if result.rowcount != 1:
            raise ApiProblem("version_conflict", "设备设置已变化或设备已撤销，请刷新后重试。", 409)
        device.settings_json = new_settings
        device.version = next_version
        return _device_public(device)


def update_available_chats(device_id: str, user_id: str, payload: DeviceChatsInput) -> dict[str, object]:
    """保存本机发现的会话名称供网页逐项授权，不上传未选会话正文。"""
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        device = db.scalar(select(Device).where(Device.id == device_id, Device.user_id == user_id))
        if device is None or device.status != "active":
            raise ApiProblem("device_inactive", "设备已撤销或未确认。", 403)
        current = device.settings_json or {}
        discovered = [chat.model_dump(mode="json") for chat in payload.chats]
        by_id = {chat["chat_id"]: chat for chat in discovered}
        selected = [
            {"chat_id": chat_id, "chat_name": by_id[chat_id]["chat_name"]}
            for chat_id in (item.get("chat_id") for item in current.get("selected_chats", []))
            if chat_id in by_id
        ]
        if current.get('chat_selection_mode') == 'all':
            excluded = set(current.get('excluded_chat_ids', []))
            selected = [chat for chat in discovered if chat['chat_id'] not in excluded]
        device.settings_json = {
            **current,
            "available_chats": discovered,
            "selected_chats": selected,
            "attachments_enabled": bool(current.get("attachments_enabled", False)),
        }
        device.last_heartbeat_epoch = now
        device.version += 1
    return {"device_id": device_id, "available_chats": discovered, "version": device.version}


def revoke_device(user_id: str, device_id: str) -> dict[str, object]:
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        device = _owned_device(db, user_id, device_id)
        if device.status != "revoked":
            device.status = "revoked"
            device.revoked_at_epoch = now
            device.version += 1
            db.execute(
                update(DeviceCredential)
                .where(DeviceCredential.device_id == device.id, DeviceCredential.revoked_at_epoch.is_(None))
                .values(revoked_at_epoch=now)
            )
    return _device_public(device)


def refresh_device_tokens(refresh_token: str) -> dict[str, object]:
    """原子消费刷新凭证并轮换出新的一对短时/长期凭证。"""
    now = _now()
    digest = _keyed_digest("device-refresh", refresh_token)
    with DbSession(_engine()) as db:
        with db.begin():
            row = db.execute(
                select(DeviceCredential, Device)
                .join(Device, Device.id == DeviceCredential.device_id)
                .where(
                    DeviceCredential.refresh_digest == digest,
                    DeviceCredential.refreshed_at_epoch.is_(None),
                    DeviceCredential.revoked_at_epoch.is_(None),
                    DeviceCredential.refresh_expires_at_epoch > now,
                    Device.status == "active",
                )
            ).one_or_none()
            if row is None:
                raise ApiProblem("refresh_token_invalid", "设备刷新凭证无效、过期或已撤销。", 401)
            old_credential, device = row
            old_credential.refreshed_at_epoch = now
            tokens, credential = _new_tokens(device.id, now)
            db.add(credential)
        return {"device_id": device.id, **tokens}


def get_claim_status(access_token: str) -> dict[str, object]:
    """让配对中的客户端轮询网页确认状态；不授予读取业务数据的权限。"""
    now = _now()
    digest = _keyed_digest("device-access", access_token)
    with DbSession(_engine()) as db:
        row = db.execute(
            select(DeviceCredential, Device)
            .join(Device, Device.id == DeviceCredential.device_id)
            .where(
                DeviceCredential.access_digest == digest,
            )
        ).one_or_none()
        if row is None:
            raise ApiProblem("device_token_invalid", "设备访问凭证无效或已过期。", 401)
        credential, device = row
        # 已撤销凭证只可获知撤销状态，以便本机及时取消自启；不再授予权限。
        revoked = credential.revoked_at_epoch is not None or device.status == "revoked"
        if not revoked and credential.access_expires_at_epoch <= now:
            raise ApiProblem("device_token_invalid", "设备访问凭证已过期。", 401)
        return {"device_id": device.id, "status": "revoked" if revoked else device.status,
                "scopes": [] if revoked else credential.scopes_json}


def unpair_current_device(access_token: str) -> dict[str, object]:
    """按访问凭证查出所属账户与设备，不接受外部传入的设备标识。"""
    identity = get_claim_status(access_token)
    with DbSession(_engine()) as db:
        device = db.get(Device, identity["device_id"])
        user_id = device.user_id
    return revoke_device(user_id, str(identity["device_id"]))


def get_device_config(device_id: str, user_id: str) -> dict[str, object]:
    with DbSession(_engine()) as db:
        device = db.scalar(select(Device).where(Device.id == device_id, Device.user_id == user_id))
        if device is None:
            raise ApiProblem("not_found", "未找到该设备。", 404)
        if device.status != "active":
            raise ApiProblem("device_inactive", "设备尚未确认或已撤销。", 403)
        return {"device_id": device.id, "settings": device.settings_json, "version": device.version}


def device_heartbeat(device_id: str, user_id: str, collected_at_epoch: int | None,
                     *, sync_request_id: str | None = None, sync_status: str | None = None) -> dict[str, object]:
    now = _now()
    if collected_at_epoch is not None and collected_at_epoch > now + 300:
        raise ApiProblem("invalid_timestamp", "采集时间不能晚于当前时间。", 422)
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        device = db.scalar(select(Device).where(Device.id == device_id, Device.user_id == user_id))
        if device is None or device.status != "active":
            raise ApiProblem("device_inactive", "设备已撤销或未确认。", 403)
        device.last_heartbeat_epoch = now
        if collected_at_epoch is not None:
            device.last_collect_epoch = collected_at_epoch
        if sync_request_id and sync_status and (device.settings_json or {}).get('sync_request_id') == sync_request_id:
            # 只确认当前请求；旧回执不能覆盖刚到达的新同步请求。
            settings = {**device.settings_json, 'sync_result': sync_status}
            # 开始执行只记录进度，完成后才消费请求，避免中途退出吞掉首轮同步。
            if sync_status != 'running':
                settings.update(sync_ack_id=sync_request_id, sync_ack_at_epoch=now)
            if settings.get('initial_sync_request_id') == sync_request_id:
                settings['initial_sync_status'] = sync_status
            device.settings_json = settings
            device.version += 1
    return {"device_id": device_id, "server_time_epoch": now, "status": "active"}


def request_device_sync(user_id: str, device_id: str, request_id: str):
    """持久化一次立即同步请求，在线客户端下次心跳读取，离线时保留。"""
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        device = _owned_device(db, user_id, device_id)
        if device.status != 'active':
            raise ApiProblem('device_inactive', '只能请求已经确认且未解除配对的客户端同步。', 409)
        if (device.settings_json or {}).get('sync_request_id') != request_id:
            current_version = device.version
            settings = {**(device.settings_json or {}), 'sync_request_id': request_id,
                'sync_requested_at_epoch': _now(), 'sync_result': 'pending'}
            # 首轮未完成时，助手发起的新请求也接续首轮进度，避免提示停留在旧回执。
            if settings.get('initial_sync_status') and settings['initial_sync_status'] != 'completed':
                settings.update(initial_sync_request_id=request_id, initial_sync_status='pending')
            changed = db.execute(update(Device).where(Device.id == device_id, Device.user_id == user_id,
                Device.version == current_version).values(settings_json=settings, version=current_version + 1))
            if changed.rowcount != 1:
                raise ApiProblem('version_conflict', '客户端设置已变化，请重试。', 409)
    return {'device_id': device_id, 'request_id': request_id, 'status': 'requested',
        'note': '在线客户端会在下一次约15秒轮询时执行；离线客户端恢复连接后执行，当前尚未确认上传完成。'}


def _batch_public(db: DbSession, batch: SyncBatch) -> dict[str, object]:
    items = db.scalars(
        select(SyncItem).where(SyncItem.batch_id == batch.id).order_by(SyncItem.ordinal)
    ).all()
    return {
        "batch_id": batch.batch_key,
        "complete": batch.complete,
        "window_start_epoch": batch.window_start_epoch,
        "window_end_epoch": batch.window_end_epoch,
        "failed_pages": batch.failed_pages_json,
        "accepted_count": batch.accepted_count,
        "duplicate_count": batch.duplicate_count,
        "rejected_count": batch.rejected_count,
        "acks": [
            {
                "source_key": item.source_key,
                "status": item.status,
                "source_document_id": item.source_document_id,
                "reason_code": item.reason_code,
            }
            for item in items
        ],
    }


def ingest_sync_batch(device_id: str, user_id: str, payload: SyncBatchInput) -> dict[str, object]:
    """先持久化批次，再按来源键逐项确认；部分失败不会越过缺口。"""
    batch_key = str(payload.batch_id)
    now = _now()
    serializable = payload.model_dump(mode="json")
    # 新增可选身份字段不改变旧客户端批次的幂等摘要。
    for item in serializable["messages"]:
        if item.get("is_self") is None:
            item.pop("is_self", None)
    payload_hash = _keyed_digest(
        "sync-batch-payload",
        json.dumps(serializable, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    with DbSession(_engine()) as db:
        device = db.scalar(select(Device).where(Device.id == device_id, Device.user_id == user_id))
        if device is None or device.status != "active":
            raise ApiProblem("device_inactive", "设备已撤销或未确认。", 403)
        settings = device.settings_json or {}
        selected_chats = settings.get("selected_chats", [])
        allowed_ids = {str(chat.get("chat_id", "")) for chat in selected_chats if isinstance(chat, dict)}
        if payload.messages and not allowed_ids:
            raise ApiProblem("device_not_configured", "请先在网页选择允许同步的群。", 409)
        if not settings.get("attachments_enabled", False) and any(
            message.attachment_metadata for message in payload.messages
        ):
            raise ApiProblem("attachments_disabled", "该设备的附件元数据采集尚未启用。", 403)

    batch_id = str(uuid4())
    with DbSession(_engine()) as db:
        with db.begin():
            batch = db.scalar(
                select(SyncBatch).where(SyncBatch.device_id == device_id, SyncBatch.batch_key == batch_key)
            )
            if batch is not None:
                if batch.payload_hash != payload_hash:
                    raise ApiProblem("idempotency_conflict", "同一批次标识对应了不同内容。", 409)
                batch_id = batch.id
            else:
                batch = SyncBatch(
                    id=batch_id,
                    user_id=user_id,
                    device_id=device_id,
                    batch_key=batch_key,
                    window_start_epoch=payload.window_start_epoch,
                    window_end_epoch=payload.window_end_epoch,
                    payload_hash=payload_hash,
                    complete=payload.complete,
                    failed_pages_json=[item.model_dump(mode="json") for item in payload.failed_pages],
                    accepted_count=0,
                    duplicate_count=0,
                    rejected_count=0,
                    created_at_epoch=now,
                )
                db.add(batch)

    for ordinal, message in enumerate(payload.messages):
        with DbSession(_engine()) as db:
            existing_ack = db.scalar(
                select(SyncItem).where(
                    SyncItem.batch_id == batch_id,
                    SyncItem.ordinal == ordinal,
                )
            )
            if existing_ack is not None:
                continue
            existing_owner_item = db.scalar(
                select(SyncItem).where(
                    SyncItem.device_id == device_id,
                    SyncItem.batch_id == batch_id,
                    SyncItem.source_key == message.source_key,
                )
            )
            existing_source = db.scalar(
                select(SourceDocument).where(
                    SourceDocument.user_id == user_id,
                    SourceDocument.source_type == "wechat",
                    SourceDocument.external_key == message.source_key,
                )
            )

        content_hash = hashlib.sha256(message.formatted_text.encode("utf-8")).hexdigest()
        status = "accepted"
        reason_code = None
        source_id: str | None = None
        allowed = message.chat_id in allowed_ids
        if not allowed:
            status = "rejected"
            reason_code = "chat_not_selected"
        elif message.is_self is True:
            # 在创建来源、附件与后台任务之前拒收本人发送或转发。
            status = "rejected"
            reason_code = "self_sent_message"
        elif existing_owner_item is not None:
            if existing_owner_item.content_hash == content_hash:
                status = "duplicate"
                source_id = existing_owner_item.source_document_id
            else:
                status = "rejected"
                reason_code = "source_key_content_conflict"
        elif existing_source is not None:
            if existing_source.content_hash == content_hash:
                status = "duplicate"
                source_id = existing_source.id
            else:
                status = "rejected"
                reason_code = "source_key_content_conflict"
        else:
            title = f"微信来源 · {message.chat_name}"
            if message.sent_at_epoch is not None:
                title += f" · {time.strftime('%Y-%m-%d %H:%M', time.localtime(message.sent_at_epoch))}"
            try:
                created = create_source(
                    user_id,
                    CreateSourceInput(
                        source_type="text",
                        title=title[:500],
                        text=message.formatted_text,
                        analysis_mode="rules",
                    ),
                    source_type_override="wechat",
                    external_key=message.source_key,
                )
                source_id = str(created["source"]["id"])
                # 唯一键并发冲突时 create_source 会回取已存在来源；正文不一致则明确拒绝。
                if created["source"].get("original_text") != message.formatted_text:
                    status = "rejected"
                    reason_code = "source_key_content_conflict"
                elif existing_source is not None:
                    status = "duplicate"
            except ApiProblem as error:
                status = "rejected"
                reason_code = error.code

        if status == "accepted" and source_id is None:
            status = "rejected"
            reason_code = "source_create_failed"
        with DbSession(_engine()) as db, db.begin():
            db.add(
                SyncItem(
                    id=str(uuid4()),
                    user_id=user_id,
                    device_id=device_id,
                    batch_id=batch_id,
                    ordinal=ordinal,
                    source_key=message.source_key,
                    chat_id=message.chat_id,
                    content_hash=content_hash,
                    attachments_json=[item.model_dump(mode="json") for item in message.attachment_metadata],
                    source_document_id=source_id,
                    status=status,
                    reason_code=reason_code,
                    created_at_epoch=_now(),
                )
            )

    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        batch = db.scalar(
            select(SyncBatch).where(
                SyncBatch.id == batch_id,
                SyncBatch.user_id == user_id,
                SyncBatch.device_id == device_id,
            )
        )
        if batch is None:
            raise ApiProblem("sync_batch_missing", "未找到同步批次。", 404)
        counts = db.execute(
            select(SyncItem.status, SyncItem.id).where(SyncItem.batch_id == batch_id)
        ).all()
        batch.accepted_count = sum(1 for status_value, _item_id in counts if status_value == "accepted")
        batch.duplicate_count = sum(1 for status_value, _item_id in counts if status_value == "duplicate")
        batch.rejected_count = sum(1 for status_value, _item_id in counts if status_value == "rejected")
        device = db.scalar(select(Device).where(Device.id == device_id, Device.user_id == user_id))
        if device is not None:
            device.last_upload_epoch = _now()
            if payload.complete:
                device.last_collect_epoch = payload.window_end_epoch
        response = _batch_public(db, batch)
    return response
