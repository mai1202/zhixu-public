"""以真实配置驱动首次引导，超过约定时间只提醒一次且可关闭。"""
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..models import AcademicTerm, CourseSeries, Device, DigestPreference, ProviderProfile, Task, User, UserOnboarding
from ..schemas.agent import AgentTurnInput
from . import main_assistant_service

SECTIONS = [
    ('model', '主助手模型', '/settings/models'),
    ('collection', '群范围与采集频率', '/settings/devices'),
    ('attachments', '附件采集与通知转化', '/settings/notifications'),
    ('tasks', '第一批事务', '/matters'),
    ('courses', '课表与课程', '/schedule/import'),
    ('reminders', '提醒、时区与收件邮箱', '/settings/mail'),
]


def _engine():
    return current_app.extensions['notification_db_engine']


def _ensure(user_id, now):
    with Session(_engine()) as db:
        if db.get(UserOnboarding, user_id):
            return
        db.add(UserOnboarding(user_id=user_id, status='active', skipped_sections_json=[], reminder_enabled=True,
            reminder_due_at_epoch=now + int(current_app.config.get('ONBOARDING_REMINDER_DELAY_SECONDS', 86400)),
            version=1, created_at_epoch=now, updated_at_epoch=now))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()


def get_state(user_id, *, create=True, now_epoch=None):
    """未打开过引导的历史账户不登记提醒，不把默认选项当作用户已完成配置。"""
    now = int(time.time()) if now_epoch is None else now_epoch
    if create:
        _ensure(user_id, now)
    else:
        with Session(_engine()) as db:
            if db.get(UserOnboarding, user_id) is None:
                return None
    # 复用默认模型选择；这里不会开启外部网络请求或执行模型。
    preferences = main_assistant_service.get_preferences(user_id)
    with Session(_engine()) as db:
        state = db.get(UserOnboarding, user_id)
        if state is None:
            return None
        profile = db.get(ProviderProfile, preferences['profile_id']) if preferences['profile_id'] else None
        model_ready = bool(profile and profile.user_id == user_id and profile.enabled and profile.purpose == 'planning' and profile.execution_target == 'server')
        devices = db.scalars(select(Device).where(Device.user_id == user_id, Device.status == 'active')).all()
        selected_groups = sum(len((device.settings_json or {}).get('selected_chats', [])) for device in devices)
        collection_ready = bool(devices and selected_groups)
        attachments_ready = any((device.settings_json or {}).get('attachments_enabled') for device in devices)
        task_count = int(db.scalar(select(func.count()).select_from(Task).where(Task.user_id == user_id, Task.archived_at_epoch.is_(None))) or 0)
        term_count = int(db.scalar(select(func.count()).select_from(AcademicTerm).where(AcademicTerm.user_id == user_id)) or 0)
        # 仅有学期锚点并不代表已有课程，不能让空课表在首次引导中被标记完成。
        course_count = int(db.scalar(select(func.count()).select_from(CourseSeries).where(CourseSeries.user_id == user_id, CourseSeries.status == 'active')) or 0)
        mail = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        actual = {'model': model_ready, 'collection': collection_ready, 'attachments': attachments_ready,
                  'tasks': task_count > 0, 'courses': course_count > 0, 'reminders': bool(mail and mail.recipient_address_id)}
        details = {'model': '已选择启用的规划模型' if model_ready else '需要连接规划模型，Agent 才能开始引导',
            'collection': f'{len(devices)} 台已确认设备 · {selected_groups} 个接管群',
            'attachments': '已启用附件采集' if attachments_ready else '可启用附件采集，或选择暂不使用',
            'tasks': f'已登记 {task_count} 项事务', 'courses': f'已设置 {term_count} 个学期 · {course_count} 门有效课程',
            'reminders': f'时区 {mail.timezone}，已选收件邮箱' if mail and mail.recipient_address_id else '可设置提醒和邮箱，也可选择暂不需要'}
        skipped = set(state.skipped_sections_json or [])
        steps = [{'key': key, 'title': title, 'href': href, 'detail': details[key],
                  'status': 'done' if actual[key] else 'skipped' if key in skipped else 'pending'} for key, title, href in SECTIONS]
        return {'status': state.status, 'steps': steps, 'completed_count': sum(step['status'] != 'pending' for step in steps),
            'step_count': len(steps), 'ready_to_complete': all(step['status'] != 'pending' for step in steps),
            'model_ready': model_ready, 'automatic_notifications': preferences['automatic_notifications'],
            'reminder_enabled': state.reminder_enabled, 'reminder_due_at_epoch': state.reminder_due_at_epoch,
            'reminder_delay_seconds': int(current_app.config.get('ONBOARDING_REMINDER_DELAY_SECONDS', 86400)),
            'reminder_queued': bool(state.reminder_outbox_id), 'snoozed_until_epoch': state.snoozed_until_epoch,
            'show_prompt': state.status != 'completed' and (state.snoozed_until_epoch is None or state.snoozed_until_epoch <= now),
            'skipped_sections': list(state.skipped_sections_json or []), 'version': state.version,
            'created_at_epoch': state.created_at_epoch}


def update_state(user_id, payload, *, now_epoch=None):
    now = int(time.time()) if now_epoch is None else now_epoch
    current = get_state(user_id, now_epoch=now)
    skipped = payload.skipped_sections if payload.skipped_sections is not None else current['skipped_sections']
    if payload.action == 'complete' and any(step['status'] == 'pending' and step['key'] not in skipped for step in current['steps']):
        raise ApiProblem('onboarding_incomplete', '请完成待配置项目，或明确选择暂不需要。', 409)
    values = {'skipped_sections_json': list(dict.fromkeys(skipped)), 'version': payload.expected_version + 1, 'updated_at_epoch': now}
    if payload.reminder_enabled is not None:
        values['reminder_enabled'] = payload.reminder_enabled
    if payload.action == 'complete':
        values.update(status='completed', snoozed_until_epoch=None)
    elif payload.action == 'snooze':
        values.update(status='snoozed', snoozed_until_epoch=now + 7 * 86400, reminder_due_at_epoch=now + 7 * 86400)
    elif payload.action == 'resume':
        values.update(status='active', snoozed_until_epoch=None)
    with Session(_engine()) as db, db.begin():
        result = db.execute(update(UserOnboarding).where(UserOnboarding.user_id == user_id,
            UserOnboarding.version == payload.expected_version).values(**values))
        if result.rowcount != 1:
            raise ApiProblem('version_conflict', '引导进度已变化，请刷新后重试。', 409)
    return get_state(user_id, now_epoch=now)


def start_agent(user_id, payload):
    """用户点击引导按钮后才创建真实主助手运行；不虚构目标、截止或设备授权。"""
    state = get_state(user_id)
    if not state['model_ready']:
        raise ApiProblem('planning_profile_required', '请先连接主助手规划模型，再开始 Agent 引导。', 422)
    from .agent_service import submit_turn
    conversation = main_assistant_service.main_conversation(user_id)
    message = (
        '请作为我的首次使用引导 Agent，协助我配置本程序，并根据我给出的实际目标添加事务。\n'
        '先调用 get_user_settings 核对现有模型、设备群目录、附件与邮件设置及 onboarding 进度，已配置的不要重复询问。\n'
        '按模型与处理方式、群范围与附件、第一批事务、课表、提醒与时区逐项推进。先给简短计划，每轮只问最关键的缺失信息。\n'
        '新账户默认接管已发现群并开启通知转化，解释实际范围与如何排除、暂停和撤销；不要根据群名猜测不存在的群 ID。\n'
        '我已经明确描述的目标可以登记为事务；缺少日期时保留未知，不编造截止时间或课程。涉及我尚未选择的偏好时请询问，不擅自重置现有配置。\n'
        '设备安装、微信登录、文件下载等需要我操作的步骤给出设置直达链接；不能声称已完成外部操作。\n'
        f'用户补充的目标与偏好：{payload.goals.strip() or "尚未填写，请先帮助我明确第一件要管理的事务。"}\n'
        '完成后重新读取设置与事务核对结果，并引导我在 /onboarding 查看实际进度。'
    )
    return submit_turn(user_id, AgentTurnInput(conversation_id=conversation, idempotency_key=payload.idempotency_key,
        message=message, page_context='首次使用引导：用户明确点击开始，按真实设置继续，进度页 /onboarding'))


def _due_owners(now):
    """按主键分批扫描，前一批已完成或处于安静时段不会挡住后续账户。"""
    cursor = ''
    while True:
        with Session(_engine()) as db:
            owners = db.scalars(select(UserOnboarding.user_id).join(User, User.id == UserOnboarding.user_id).where(
                User.is_active.is_(True), UserOnboarding.user_id > cursor,
                UserOnboarding.status != 'completed', UserOnboarding.reminder_enabled.is_(True),
                UserOnboarding.reminder_outbox_id.is_(None), UserOnboarding.reminder_due_at_epoch <= now
            ).order_by(UserOnboarding.user_id).limit(100)).all()
        if not owners:
            return
        yield from owners
        cursor = owners[-1]


def dispatch_reminders(*, now_epoch=None):
    """只扫描已进入引导的账户，尊重关闭、稍后设置和安静时段，最多排队一次。"""
    now = int(time.time()) if now_epoch is None else now_epoch
    if current_app.config.get('MAIL_TRANSPORT') not in {'fake', 'smtp', 'graph'}:
        return {'queued': 0}
    queued = 0
    from . import mail_service, reminder_service
    for owner in _due_owners(now):
        state = get_state(owner, create=False, now_epoch=now)
        if not state or not state['show_prompt'] or state['ready_to_complete']:
            continue
        with Session(_engine()) as db:
            preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == owner))
            if preference and reminder_service._in_quiet_hours(now, preference.timezone, preference.quiet_hours_json or {}):
                continue
        pending = '、'.join(step['title'] for step in state['steps'] if step['status'] == 'pending')
        outbox = mail_service.queue_onboarding_reminder(owner, pending=pending, started_at_epoch=state['created_at_epoch'], now_epoch=now)
        if outbox:
            with Session(_engine()) as db, db.begin():
                record = db.get(UserOnboarding, owner)
                record.reminder_outbox_id = outbox['id']
            queued += 1
    return {'queued': queued}
