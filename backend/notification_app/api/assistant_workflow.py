"""通知整理邮箱与助手定时计划接口，统一使用已有认证与 CSRF。"""

from uuid import UUID
from flask import g, jsonify, request
from ..errors import ApiProblem
from ..security import authenticated, parse_json_body
from ..schemas.assistant_workflow import AgentScheduleInput, AgentScheduleStatusInput, MailboxInput
from ..schemas.agent import AgentConversationActionInput
from ..services import notification_inbox_service as inbox, agent_schedule_service as schedules
from ..services import main_assistant_service
from . import api_bp


@api_bp.get('/notifications')
@authenticated()
def notification_mailbox():
    """只列出当前账户的通知原文索引。"""
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        raise ApiProblem('validation_error', '分页位置无效。', 422) from None
    return jsonify(inbox.list_messages(g.auth_identity['user_id'], request.args.get('mailbox', 'inbox'),
        request.args.get('q', ''), offset=offset))


@api_bp.get('/notifications/counts')
@authenticated()
def notification_counts():
    """一次返回三个邮箱和未读数，页面进入时即可显示全部分类数字。"""
    return jsonify(inbox.mailbox_counts(g.auth_identity['user_id']))


@api_bp.get('/notifications/<uuid:source_id>')
@authenticated()
def notification_detail(source_id):
    return jsonify(inbox.get_message(g.auth_identity['user_id'], str(source_id)))


@api_bp.get('/notifications/notice/<uuid:notice_id>')
@authenticated()
def legacy_notification_detail(notice_id):
    owner = g.auth_identity['user_id']
    return jsonify(inbox.get_message(owner, inbox.source_for_notice(owner, str(notice_id))))


@api_bp.patch('/notifications/<uuid:source_id>')
@authenticated(csrf=True)
def notification_state(source_id):
    return jsonify(inbox.update_mailbox(g.auth_identity['user_id'], str(source_id), parse_json_body(MailboxInput)))


@api_bp.post('/notifications/<uuid:source_id>/organize')
@authenticated(csrf=True)
def organize_notification(source_id):
    """显式重新整理仍复用通知 Job，不要求用户填写日期或确认结构化字段。"""
    from ..jobs.queue import enqueue_job
    from ..models import ChangeSet
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    owner = g.auth_identity['user_id']
    inbox.get_message(owner, str(source_id))
    main_assistant_service.main_conversation(owner)
    if not main_assistant_service.get_preferences(owner)['profile_id']:
        raise ApiProblem('planning_profile_required', '请先为主助手配置模型。', 409)
    with Session(inbox._engine()) as db:
        change = db.scalar(select(ChangeSet).where(ChangeSet.user_id == owner,
            ChangeSet.source_document_id == str(source_id)).order_by(ChangeSet.created_at_epoch.desc()).limit(1))
    from uuid import uuid4
    job = enqueue_job(inbox._engine(), user_id=owner, kind='agent.notification',
        payload={'source_id': str(source_id), 'change_id': change.id if change else None},
        idempotency_key=f'organize:{source_id}:{uuid4()}', priority=60)
    return jsonify({'run_id': job.id, 'status': job.status}), 202


@api_bp.get('/agent/schedules')
@authenticated()
def list_agent_schedules():
    return jsonify(schedules.list_tasks(g.auth_identity['user_id']))


@api_bp.post('/agent/schedules')
@authenticated(csrf=True)
def create_agent_schedule():
    return jsonify(schedules.create_task(g.auth_identity['user_id'], parse_json_body(AgentScheduleInput))), 201


@api_bp.post('/agent/schedules/<uuid:task_id>/cancel')
@authenticated(csrf=True)
def cancel_agent_schedule(task_id):
    payload = parse_json_body(AgentConversationActionInput)
    return jsonify(schedules.cancel_task(g.auth_identity['user_id'], str(task_id), payload.expected_version))


@api_bp.patch('/agent/schedules/<uuid:task_id>/status')
@authenticated(csrf=True)
def update_agent_schedule_status(task_id):
    """设置页可暂停、恢复或取消关注任务；失败和上次成功时间保持不变。"""
    payload = parse_json_body(AgentScheduleStatusInput)
    return jsonify(schedules.set_task_status(g.auth_identity['user_id'], str(task_id),
        payload.expected_version, payload.action))


@api_bp.post('/notifications/<uuid:root_id>/detach/<uuid:source_id>')
@authenticated(csrf=True)
def detach_notification(root_id, source_id):
    """人工拆分有版本保护，后台不会立即把该来源合回去。"""
    payload = parse_json_body(AgentConversationActionInput)
    return jsonify(inbox.detach_message(g.auth_identity['user_id'], str(root_id), str(source_id), payload.expected_version))
