"""运行时隐私告知与模型资料处理范围，不把部署配置写成已核验的事实。"""
from urllib.parse import urlsplit
from flask import current_app
from ..errors import ApiProblem

POLICY_VERSION = "2026-11-22"


def notice():
    return {"version": POLICY_VERSION, "password_login_enabled": bool(current_app.config.get('TEST_PASSWORD_LOGIN_ENABLED', False)),
        "contact": current_app.config.get("PRIVACY_CONTACT") or "请联系邀请你参加内测的项目负责人。",
        "paragraphs": [
            "知序使用邮箱进行身份验证与本人通知投递，保存你提交的通知、课表、日程、事务、对话和附件，以提供整理、检索、提醒与协作服务。",
            "新采集设备默认不上传通知。请勾选允许采集的群聊，按需开启附件。客户端仅在你授权后上传所选范围；停止采集或撤销设备后不再接收该设备的新内容。",
            "启用模型功能后，完成当前任务所需的文本、图片及相关历史会发送给所选模型服务商。你可以在模型连接页查看实际服务地址；境外或位置未确认的服务需要对该配置单独同意。",
            "共享事务只向当前协作成员展示你明确分享的字段；文件可选择仅发起人可见。共同空闲时间需要每位成员单独授权，仅返回交集，可随时撤回。",
            "个人资料默认仅本人可访问，账户会话、附件下载和工具执行均校验身份。API密钥加密存储。服务器仍需由运营者维护访问权限、备份和存储期限。",
            "你可以联系项目负责人申请导出、删除个人数据，并在设置中撤销设备或来源信任。模型识别结果可能出错，请核对重要截止日期；未授权前不会使用你的私人通知作公开广告资料。",
        ]}


def needs_separate_consent(profile):
    if profile.execution_target == "device" or profile.provider == "lm_studio": return False
    host = (urlsplit(profile.base_url).hostname or "").lower()
    domestic = {"api.deepseek.com", "api.xiaomimimo.com", "dashscope.aliyuncs.com", "dashscope.aliyun.com"}
    return host not in domestic


def require_model_consent(profile):
    if (current_app.config.get("PRIVACY_CONSENT_REQUIRED", True) and needs_separate_consent(profile)
            and not (profile.capabilities_json or {}).get("data_processing_consent_at")):
        raise ApiProblem("model_data_consent_required", "该模型服务位于境外或位置尚未确认。请查看服务地址，并在模型连接页单独同意资料处理后使用。", 409,
                         {"profile_id": profile.id, "href": "/settings/models"})


def set_model_consent(user_id, profile_id, payload):
    """按配置记录明确同意；默认模型也可撤回该账户自己的授权。"""
    import time
    from sqlalchemy import select, update
    from sqlalchemy.orm import Session
    from ..models import ProviderProfile
    from .provider_service import profile_public
    with Session(current_app.extensions["notification_db_engine"]) as db, db.begin():
        item = db.scalar(select(ProviderProfile).where(ProviderProfile.id == profile_id, ProviderProfile.user_id == user_id))
        if item is None: raise ApiProblem("not_found", "未找到该模型配置。", 404)
        capabilities = dict(item.capabilities_json or {})
        if payload.enabled: capabilities["data_processing_consent_at"] = int(time.time())
        else: capabilities.pop("data_processing_consent_at", None)
        changed = db.execute(update(ProviderProfile).where(ProviderProfile.id == item.id, ProviderProfile.version == payload.expected_version)
            .values(capabilities_json=capabilities, version=payload.expected_version + 1, updated_at_epoch=int(time.time())))
        if changed.rowcount != 1: raise ApiProblem("version_conflict", "模型配置已变化，请刷新后重试。", 409)
        db.refresh(item)
        return profile_public(item)
