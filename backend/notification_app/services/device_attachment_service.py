"""认证设备附件入库、独立收据与可恢复的通知转化。"""
from base64 import b64decode
from binascii import Error as Base64Error
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import time
from uuid import UUID, uuid4

from flask import current_app
from sqlalchemy import select, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.datastructures import FileStorage

from ..errors import ApiProblem
from ..jobs.queue import enqueue_job, retry_owned_job
from ..models import AgentAttachment, Device, DeviceAttachment, Job, OcrArtifact, SyncItem
from ..schemas.business import CreateSourceInput
from . import agent_material_service, business_service

SUPPORTED = {'.png', '.jpg', '.jpeg', '.txt', '.md', '.csv', '.json', '.pdf', '.docx', '.xls', '.xlsx'}
MAX_BYTES = 10 * 1024 * 1024


def _engine():
    return current_app.extensions['notification_db_engine']


def _raw_path(item_id):
    # 文件键仅来自服务端 UUID，解析失败时原文件仍留在私有目录供重试。
    identifier = str(UUID(item_id))
    root = Path(current_app.config['DATA_DIR']) / 'private' / 'device-attachments'
    root.mkdir(parents=True, exist_ok=True)
    return root / identifier


def _public(item, db):
    status, error = item.status, item.error_code
    job = db.get(Job, item.job_id) if item.job_id else None
    if job and job.status in {'failed', 'cancelled', 'expired'} and status not in {'converted', 'ready'}:
        status, error = 'failed', job.error_code
    elif job and job.status in {'leased', 'running'}:
        status = 'parsing'
    elif job and job.status == 'retry_wait':
        status, error = 'retry_wait', job.error_code
    return {'id': item.id, 'device_id': item.device_id, 'parent_source_id': item.parent_source_id,
            'attachment_key': item.attachment_key, 'filename': item.filename, 'kind': item.kind,
            'size_bytes': item.size_bytes, 'status': status, 'error_code': error,
            'auto_notice': item.auto_notice, 'source_document_id': item.source_document_id,
            'download_href': f'/api/v1/devices/attachments/{item.id}/download' if item.status != 'unavailable' else None,
            'created_at_epoch': item.created_at_epoch, 'updated_at_epoch': item.updated_at_epoch}


def _authorized_device(db, user_id, device_id, chat_id):
    device = db.scalar(select(Device).where(Device.id == device_id, Device.user_id == user_id))
    if device is None or device.status != 'active':
        raise ApiProblem('device_inactive', '设备尚未确认或已撤销。', 403)
    settings = device.settings_json or {}
    if not settings.get('attachments_enabled'):
        raise ApiProblem('attachments_disabled', '该设备的附件采集已关闭。', 403)
    if chat_id not in {chat.get('chat_id') for chat in settings.get('selected_chats', [])}:
        raise ApiProblem('chat_not_selected', '该会话不在当前采集范围。', 403)
    return settings


def _queue(item_id, user_id):
    job = enqueue_job(_engine(), user_id=user_id, kind='attachment.process', payload={'attachment_id': item_id},
                      idempotency_key=f'device-attachment:{item_id}', max_attempts=12, priority=40)
    if job.status in {'failed', 'cancelled', 'expired'}:
        retry_owned_job(_engine(), user_id=user_id, job_id=UUID(job.id))
    with Session(_engine()) as db, db.begin():
        item = db.get(DeviceAttachment, item_id)
        item.job_id, item.status, item.error_code = job.id, 'queued', None
        item.updated_at_epoch = int(time.time())


def ingest(device_id, user_id, payload):
    try:
        return _ingest(device_id, user_id, payload)
    except IntegrityError:
        raise ApiProblem('attachment_busy', '附件正在保存，请稍后重试。', 409) from None


def _ingest(device_id, user_id, payload):
    """先校验当前授权与父消息，再接收内容；同一附件键不产生第二份业务结果。"""
    now = int(time.time())
    data = None
    if payload.data_base64:
        try:
            data = b64decode(payload.data_base64, validate=True)
        except (Base64Error, ValueError):
            raise ApiProblem('attachment_encoding', '附件内容编码无效。', 422) from None
        if not data or len(data) > MAX_BYTES:
            raise ApiProblem('attachment_size', '单个附件必须在 1 字节到 10 MiB 之间。', 413)
        if sha256(data).hexdigest() != payload.content_hash:
            raise ApiProblem('attachment_hash', '附件摘要校验失败。', 422)
        suffix = Path(payload.filename).suffix.lower()
        if suffix not in SUPPORTED or (payload.kind == 'image') != (suffix in {'.png', '.jpg', '.jpeg'}):
            raise ApiProblem('unsupported_attachment', '该附件格式暂不支持。', 422)
    with Session(_engine()) as db, db.begin():
        settings = _authorized_device(db, user_id, device_id, payload.chat_id)
        if payload.kind not in settings.get('attachment_kinds', ['image', 'file']):
            raise ApiProblem('attachment_kind_disabled', '该设备未启用此类附件采集。', 403)
        parent = db.scalar(select(SyncItem).where(SyncItem.user_id == user_id, SyncItem.device_id == device_id,
            SyncItem.source_key == payload.source_key, SyncItem.chat_id == payload.chat_id,
            SyncItem.status.in_(('accepted', 'duplicate')), SyncItem.source_document_id.is_not(None)).limit(1))
        if parent is None:
            raise ApiProblem('attachment_parent_pending', '请先上传并确认该附件所属的消息。', 409)
        item = db.scalar(select(DeviceAttachment).where(DeviceAttachment.device_id == device_id,
            DeviceAttachment.attachment_key == payload.attachment_key).with_for_update())
        if item:
            if item.parent_source_id != parent.source_document_id or item.kind != payload.kind or (item.content_hash and data and item.content_hash != payload.content_hash):
                raise ApiProblem('idempotency_conflict', '附件标识已对应其他内容。', 409)
            if item.status in {'ready', 'converted', 'queued'} or (item.status == 'unavailable' and data is None):
                return {**_public(item, db), 'duplicate': True}
            if item.status == 'receiving' and item.updated_at_epoch > now - 300:
                raise ApiProblem('attachment_busy', '附件正在保存，请稍后重试。', 409)
        else:
            item = DeviceAttachment(id=str(uuid4()), user_id=user_id, device_id=device_id,
                parent_source_id=parent.source_document_id, attachment_key=payload.attachment_key,
                filename=payload.filename, kind=payload.kind, size_bytes=0, status='receiving',
                auto_notice=bool(settings.get('attachment_auto_notice', True)), created_at_epoch=now, updated_at_epoch=now)
            db.add(item)
        # 图片未下载时扩展名未知；收到已校验的实际 PNG/JPEG 后同步文件名，避免按旧后缀解析。
        if data is not None and item.status == 'unavailable':
            item.filename = payload.filename
        item.status = 'unavailable' if data is None else 'receiving'
        item.error_code = payload.unavailable_reason
        item.content_hash = payload.content_hash if data else item.content_hash
        item.size_bytes = len(data) if data else 0
        item.updated_at_epoch = now
        db.flush()
        item_id, material_id = item.id, item.material_id
    if data is not None:
        try:
            if not material_id:
                _raw_path(item_id).write_bytes(data)
            _queue(item_id, user_id)
        except Exception as error:
            with Session(_engine()) as db, db.begin():
                item = db.get(DeviceAttachment, item_id)
                item.status, item.error_code = 'failed', str(getattr(error, 'code', 'attachment_store_failed'))[:80]
            raise
    with Session(_engine()) as db:
        return _public(db.get(DeviceAttachment, item_id), db)


def list_attachments(user_id, *, parent_source_id=None):
    statement = select(DeviceAttachment).where(DeviceAttachment.user_id == user_id)
    if parent_source_id:
        statement = statement.where(or_(DeviceAttachment.parent_source_id == parent_source_id,
                                       DeviceAttachment.source_document_id == parent_source_id))
    with Session(_engine()) as db:
        items = db.scalars(statement.order_by(DeviceAttachment.created_at_epoch.desc()).limit(50)).all()
        return {'items': [_public(item, db) for item in items]}


def retry(user_id, item_id):
    with Session(_engine()) as db:
        item = db.scalar(select(DeviceAttachment).where(DeviceAttachment.id == item_id, DeviceAttachment.user_id == user_id))
        if item is None:
            raise ApiProblem('not_found', '未找到该附件。', 404)
        if not item.material_id and not _raw_path(item.id).is_file():
            raise ApiProblem('attachment_unavailable', '原文件尚未上传，请让采集端重新同步。', 409)
        if item.status in {'converted', 'ready'}:
            return _public(item, db)
        # 重新检查范围和开关，关闭附件后不会被旧的重试入口重新启用。
        parent = db.scalar(select(SyncItem).where(SyncItem.device_id == item.device_id,
            SyncItem.source_document_id == item.parent_source_id).limit(1))
        _authorized_device(db, user_id, item.device_id, parent.chat_id if parent else '')
        material = db.get(AgentAttachment, item.material_id)
        if material and material.ocr_artifact_id:
            artifact = db.get(OcrArtifact, material.ocr_artifact_id)
            if artifact and artifact.job_id:
                retry_owned_job(_engine(), user_id=user_id, job_id=UUID(artifact.job_id))
    _queue(item_id, user_id)
    return {'id': item_id, 'status': 'queued'}


def handle_process_job(claim):
    """确定的格式或权限错误立即结束；OCR 等待与临时存储异常保留有限重试。"""
    try:
        return _process_attachment_job(claim)
    except ApiProblem as error:
        if error.code in {'not_found', 'device_inactive', 'attachments_disabled', 'chat_not_selected',
            'attachment_missing', 'unsupported_attachment', 'attachment_size', 'attachment_parse_failed',
            'attachment_no_text', 'attachment_text_limit', 'attachment_ocr_failed',
            'image_size_invalid', 'image_type_unsupported', 'image_pixels_invalid', 'image_invalid'}:
            error.retryable = False
        raise


def _process_attachment_job(claim):
    """图片等待既有 OCR，文档复用现有解析；关闭转化时只保存附件。"""
    item_id = str(claim.payload['attachment_id'])
    # 先独立完成附件解析并保存素材，随后再用短事务读取结果，避免嵌套数据库写锁。
    with Session(_engine()) as db:
        receipt = db.scalar(select(DeviceAttachment).where(DeviceAttachment.id == item_id, DeviceAttachment.user_id == claim.user_id))
        if receipt is None:
            raise ApiProblem('not_found', '附件收据不存在。', 404)
        parent = db.scalar(select(SyncItem).where(SyncItem.device_id == receipt.device_id,
            SyncItem.user_id == claim.user_id, SyncItem.source_document_id == receipt.parent_source_id).limit(1))
        _authorized_device(db, claim.user_id, receipt.device_id, parent.chat_id if parent else '')
        material_id, filename = receipt.material_id, receipt.filename
    if material_id is None:
        path = _raw_path(item_id)
        if not path.is_file():
            raise ApiProblem('attachment_missing', '原附件文件不存在。', 404)
        with path.open('rb') as handle:
            material = agent_material_service.upload_material(claim.user_id, FileStorage(stream=handle, filename=filename))
        with Session(_engine()) as db, db.begin():
            db.get(DeviceAttachment, item_id).material_id = material['id']
        path.unlink(missing_ok=True)
    with Session(_engine()) as db:
        item = db.scalar(select(DeviceAttachment).where(DeviceAttachment.id == item_id, DeviceAttachment.user_id == claim.user_id))
        if item is None:
            raise ApiProblem('not_found', '附件收据不存在。', 404)
        if item.source_document_id or item.status == 'ready':
            return _public(item, db)
        parent = db.scalar(select(SyncItem).where(SyncItem.device_id == item.device_id,
            SyncItem.source_document_id == item.parent_source_id).limit(1))
        settings = _authorized_device(db, claim.user_id, item.device_id, parent.chat_id if parent else '')
        material = db.get(AgentAttachment, item.material_id)
        if not material or material.user_id != claim.user_id:
            raise ApiProblem('attachment_missing', '原附件记录不存在。', 404)
        artifact = db.get(OcrArtifact, material.ocr_artifact_id) if material.ocr_artifact_id else None
        text = material.extracted_text or ''
        if artifact:
            if not artifact.result_json:
                ocr_job = db.get(Job, artifact.job_id) if artifact.job_id else None
                if ocr_job and ocr_job.status in {'failed', 'cancelled', 'expired'}:
                    raise ApiProblem('attachment_ocr_failed', '图片识别未完成，原图已保留，请检查 OCR 后重试。', 422)
                raise ApiProblem('attachment_ocr_pending', '图片仍在识别，原文件已保留。', 409)
            text = '\n'.join(part['text'] for part in artifact.result_json.get('items', []))
        auto_notice = item.auto_notice and bool(settings.get('attachment_auto_notice', True))
        filename, parent_source_id = item.filename, item.parent_source_id
    source_id = None
    if auto_notice:
        if not text.strip():
            raise ApiProblem('attachment_no_text', '附件没有可转化的文本，原文件已保留。', 422)
        if len(text.encode('utf-8')) > 100_000:
            raise ApiProblem('attachment_text_limit', '附件文字超过通知长度限制，请拆分处理。', 413)
        created = business_service.create_source(claim.user_id,
            CreateSourceInput(source_type='text', title=f'附件通知 · {filename}'[:500], text=text, analysis_mode='rules'),
            source_type_override='wechat_attachment', external_key=f'attachment:{item_id}')
        source_id = created['source']['id']
    with Session(_engine()) as db, db.begin():
        item = db.get(DeviceAttachment, item_id)
        item.source_document_id, item.status, item.error_code = source_id, 'converted' if source_id else 'ready', None
        item.updated_at_epoch = int(time.time())
        if artifact and source_id:
            current_artifact = db.get(OcrArtifact, artifact.id)
            current_artifact.source_document_id = source_id
    return {'id': item_id, 'status': 'converted' if source_id else 'ready', 'source_document_id': source_id,
            'parent_source_id': parent_source_id}


def attachment_file(user_id, item_id):
    """解析失败也可下载本人的原件，不将磁盘路径返回到网页。"""
    with Session(_engine()) as db:
        item = db.scalar(select(DeviceAttachment).where(DeviceAttachment.id == item_id, DeviceAttachment.user_id == user_id))
        if item is None:
            raise ApiProblem('not_found', '未找到该附件。', 404)
        if item.material_id:
            path, material = agent_material_service.material_file(user_id, item.material_id)
            return path, material.filename
        path = _raw_path(item.id)
        if not path.is_file():
            raise ApiProblem('attachment_missing', '原附件尚未上传。', 404)
        return path, item.filename
