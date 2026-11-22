"""服务商余额告警与最近七天测试用量；收件人只能由部署配置指定。"""
from datetime import datetime, timedelta
import time
import json
from types import SimpleNamespace
from urllib.request import Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo
from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..errors import ApiProblem
from ..jobs.queue import enqueue_job
from ..mail.transports import MailDeliveryError, MailNotConfigured
from ..models import OperationsAlert, PlatformTokenDay, PlatformTokenReservation, User
from . import auth_service, token_quota_service

ZONE = ZoneInfo('Asia/Shanghai')


def _engine():
    return current_app.extensions['notification_db_engine']


def weekly_usage_text():
    """按北京时间自然日统计含今天的七天，不读取通知、对话、附件或任何凭证。"""
    today = datetime.now(ZONE).date()
    days = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    email = current_app.config.get('OPERATIONS_TEST_EMAIL', '').strip().lower()
    lines = [f'测试账号：{email or "尚未配置"}', f'统计范围：{days[0]} 至 {days[-1]}（北京时间，含今天）',
        '日期 | 已结算金额 | 调用中预留 | 调用次数 | 估算次数']
    with Session(_engine()) as db:
        user = db.scalar(select(User).where(User.email_key == email)) if email else None
        rows = {row.day: row for row in db.scalars(select(PlatformTokenDay).where(
            PlatformTokenDay.user_id == user.id, PlatformTokenDay.day.in_(days)))} if user else {}
        calls = db.scalars(select(PlatformTokenReservation).where(PlatformTokenReservation.user_id == user.id,
            PlatformTokenReservation.day.in_(days))).all() if user else []
        total = 0
        for day in days:
            row = rows.get(day)
            spent, held = (row.spent_micros, row.held_micros) if row else (0, 0)
            day_calls = [call for call in calls if call.day == day]
            estimated = sum(call.status == 'estimated' for call in day_calls)
            total += spent
            lines.append(f'{day} | {spent / 1e6:.6f}元 | {held / 1e6:.6f}元 | {len(day_calls)} | {estimated}')
    lines.extend([f'七天已结算合计：{total / 1e6:.6f}元。',
        '金额来自系统账本及配置单价，部分用量可能为保守估算；预留额尚未结算，不与已结算金额重复相加。',
        '此统计不是DeepSeek账户剩余余额或供应商最终账单。'])
    return '\n'.join(lines)


def provider_balance_exhausted(profile):
    """只有平台DeepSeek余额不足才建告警，同一密钥同一天跨账户去重。"""
    recipient = current_app.config.get('OPERATIONS_ALERT_EMAIL', '').strip()
    if not recipient or profile.provider != 'deepseek' or not token_quota_service.is_platform_profile(profile):
        return None
    day = datetime.now(ZONE).date().isoformat()
    key_hash = auth_service._keyed_digest('deepseek-balance-alert', str(current_app.config.get('DEFAULT_LLM_API_KEY') or ''))
    incident = f'deepseek-balance:{day}:{key_hash}'
    try:
        with Session(_engine()) as db, db.begin():
            existing = db.scalar(select(OperationsAlert).where(OperationsAlert.incident_key == incident))
            if existing:
                return existing.id
            identifier = str(uuid4())
            db.add(OperationsAlert(id=identifier, user_id=profile.user_id, incident_key=incident,
                status='queued', created_at_epoch=int(time.time())))
    except IntegrityError:
        with Session(_engine()) as db:
            return db.scalar(select(OperationsAlert.id).where(OperationsAlert.incident_key == incident))
    dispatch_pending_alerts()
    return identifier


def notify_balance_safely(profile):
    """告警入队故障不能覆盖供应商的原始错误，后台调度可恢复已落库的待发告警。"""
    try:
        return provider_balance_exhausted(profile)
    except Exception as error:
        current_app.logger.error('余额告警入队失败 error_type=%s', type(error).__name__)


def dispatch_pending_alerts():
    with Session(_engine()) as db:
        rows = db.scalars(select(OperationsAlert).where(OperationsAlert.status == 'queued').limit(20)).all()
        pending = [(row.id, row.user_id) for row in rows]
    for identifier, user_id in pending:
        enqueue_job(_engine(), user_id=user_id, kind='operations.alert', payload={'alert_id': identifier},
            idempotency_key='operations-alert:' + identifier, max_attempts=5, priority=100)


def schedule_balance_check():
    """每十五分钟产生一个持久检查，不调用生成模型，也不阻塞其他提醒扫描。"""
    if not current_app.config.get('OPERATIONS_ALERT_EMAIL') or not current_app.config.get('DEFAULT_LLM_API_KEY'):
        return
    if current_app.config.get('DEFAULT_LLM_PROVIDER') != 'deepseek': return
    with Session(_engine()) as db:
        user = db.scalar(select(User).where(User.email_key == current_app.config.get('OPERATIONS_TEST_EMAIL', '').lower()))
        if not user: return
        user_id = user.id
    enqueue_job(_engine(), user_id=user_id, kind='operations.balance_check', payload={},
        idempotency_key='deepseek-balance-check:' + str(int(time.time()) // 900), max_attempts=1, priority=80)


def handle_balance_check(claim):
    """使用DeepSeek官方余额接口；网络失败或格式不符不冒充余额耗尽。"""
    from .provider_service import _RejectRedirectHandler
    key = current_app.config.get('DEFAULT_LLM_API_KEY')
    if not key or current_app.config.get('DEFAULT_LLM_PROVIDER') != 'deepseek':
        return {'status': 'not_configured'}
    request = Request('https://api.deepseek.com/user/balance', headers={'Authorization': 'Bearer ' + key})
    try:
        with build_opener(_RejectRedirectHandler()).open(request, timeout=15) as response:
            raw = response.read(16385)
        if len(raw) > 16384: raise ValueError()
        value = json.loads(raw)
        if type(value.get('is_available')) is not bool: raise ValueError()
    except Exception:
        raise ApiProblem('balance_check_failed', '余额查询失败，未判断为余额不足。', 503) from None
    if not value['is_available']:
        provider_balance_exhausted(SimpleNamespace(user_id=claim.user_id, provider='deepseek', is_default=True))
    return {'available': value['is_available']}


def handle_alert_job(claim):
    """复用现有邮件传输；只有确定未投递的临时故障重试，未知结果不重复发送。"""
    recipient = current_app.config.get('OPERATIONS_ALERT_EMAIL', '').strip()
    if not recipient:
        raise ApiProblem('operations_mail_unconfigured', '运营告警收件人未配置。', 503)
    identifier = str(claim.payload.get('alert_id', ''))
    with Session(_engine()) as db, db.begin():
        row = db.scalar(select(OperationsAlert).where(OperationsAlert.id == identifier,
            OperationsAlert.user_id == claim.user_id).with_for_update())
        if row is None:
            raise ApiProblem('not_found', '告警记录不存在。', 404)
        if row.status in {'sent', 'unknown', 'failed'}:
            return {'status': row.status}
        if row.status == 'sending':
            row.status, row.error_code = 'unknown', 'interrupted_delivery'
            return {'status': 'unknown'}
        row.status = 'sending'
    status, error_code, retry = 'sent', None, False
    try:
        body = 'DeepSeek返回余额或服务商配额不足，平台模型调用已失败。请检查或充值DeepSeek账户。\n'
        body += '这不是领导测试账号每日4元限额的提醒。\n\n' + weekly_usage_text()
        current_app.extensions['notification_mail_transport'].send_message(recipient,
            '知序：DeepSeek余额不足及最近7天测试用量', body)
    except MailNotConfigured:
        status, error_code, retry = 'queued', 'mail_not_configured', True
    except MailDeliveryError as error:
        status = 'unknown' if error.state == 'unknown' else 'queued' if error.retryable else 'failed'
        error_code, retry = error.code, error.retryable and error.state != 'unknown'
    except Exception:
        status, error_code = 'unknown', 'operations_delivery_unknown'
    with Session(_engine()) as db, db.begin():
        row = db.get(OperationsAlert, identifier)
        row.status, row.error_code = status, error_code
        if status == 'sent': row.sent_at_epoch = int(time.time())
    if retry:
        raise ApiProblem(error_code, '告警邮件暂未投递，等待重试。', 503)
    return {'status': status, 'alert_id': identifier}
