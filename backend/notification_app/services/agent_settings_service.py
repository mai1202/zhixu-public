"""向助手开放账户日常设置，复用设备目录、版本保护和既有配置验证。"""

import json
from pathlib import Path

from flask import current_app
from sqlalchemy.orm import Session
from ..errors import ApiProblem
from ..schemas.agent import AgentPreferenceInput
from ..schemas.devices import DeviceSettingsInput
from . import device_service, main_assistant_service, mail_service, operation_service, provider_service


def get_catalog():
    """设置目录与助手共用同一份真实路由表，避免凭空编造入口。"""
    return json.loads((Path(__file__).resolve().parents[1] / 'settings_catalog.json').read_text(encoding='utf-8'))


def get_settings(user_id):
    """模型只看到配置摘要与本人微信群目录，不返回密钥或登录凭证。"""
    from . import onboarding_service
    return {'catalog': get_catalog(), 'agent': main_assistant_service.get_preferences(user_id), 'onboarding': onboarding_service.get_state(user_id, create=False),
        'mail': mail_service.get_preferences(user_id), 'devices': device_service.list_devices(user_id)['items'],
        'models': provider_service.list_profiles(user_id)['items']}


def _record(user_id, run_id, action, before, after):
    """设置变更留下前后快照，便于用户核对或按原值恢复。"""
    with Session(current_app.extensions['notification_db_engine']) as db, db.begin():
        operation_service.record_operation(db, user_id=user_id, actor_type='agent', actor_id=run_id,
            action=action, object_type='settings', object_id=user_id,
            summary='主助手按对话要求修改账户设置', before=before, after=after)


def update_agent(user_id, run_id, patch):
    """仅合并实际提供的字段，避免开关操作重置模型或思考强度。"""
    allowed = set(AgentPreferenceInput.model_fields)
    if not patch or not set(patch) <= allowed:
        raise ApiProblem('invalid_settings', '助手设置字段无效。', 422)
    before = main_assistant_service.get_preferences(user_id)
    payload = {key: value for key, value in before.items() if key in allowed} | patch
    after = main_assistant_service.save_preferences(user_id, AgentPreferenceInput.model_validate(payload))
    _record(user_id, run_id, 'settings.agent', before, after)
    return after


def update_chats(user_id, run_id, device_id, expected_version, chat_ids, collection=None):
    """按稳定群 ID 维护追踪列表，不接受模型虚构的群名或未同步的群。"""
    devices = device_service.list_devices(user_id)['items']
    device = next((item for item in devices if item['id'] == device_id), None)
    if device is None:
        raise ApiProblem('not_found', '未找到该设备。', 404)
    available = {item['chat_id']: item for item in device['settings'].get('available_chats', [])}
    if len(chat_ids) != len(set(chat_ids)) or any(identifier not in available for identifier in chat_ids):
        raise ApiProblem('unknown_chat', '请选择设备已经同步的群 ID。', 422)
    allowed = {'attachments_enabled', 'attachment_kinds', 'attachment_auto_notice', 'chat_selection_mode', 'excluded_chat_ids', 'sync_interval_seconds'}
    if collection and not set(collection) <= allowed:
        raise ApiProblem('invalid_settings', '采集设置字段无效。', 422)
    payload = {'expected_version': expected_version, 'selected_chats': [available[value] for value in chat_ids],
               'attachments_enabled': device['settings'].get('attachments_enabled', False), **(collection or {})}
    result = device_service.update_device_settings(user_id, device_id, DeviceSettingsInput.model_validate(payload))
    _record(user_id, run_id, 'settings.tracked_chats', device, result)
    return result
