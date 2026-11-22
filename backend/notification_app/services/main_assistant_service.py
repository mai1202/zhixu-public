"""主助手托管新通知，所有自动决定保留原文、处理理由和可撤销变更。"""

import json
import re
import time
from uuid import uuid4

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..jobs.queue import enqueue_job
from ..models import AgentConversation, AgentPreference, AgentTurn, ChangeSet, Job, OperationLog, ProviderProfile, SourceDocument, SourceSegment
from ..schemas.business import NormalizedBundle
from . import business_service, provider_service


def _engine():
    return current_app.extensions["notification_db_engine"]


def _ensure_default_preference(user_id: str) -> None:
    """为新旧账户自动选中服务端默认规划模型；用户已有选择时不覆盖。"""
    try:
        provider_service.ensure_default_profiles(user_id)
    except Exception:
        current_app.logger.warning("默认规划模型准备失败 user_id=%s", user_id)
    with Session(_engine()) as db, db.begin():
        preference = db.get(AgentPreference, user_id)
        if preference is None:
            preference = AgentPreference(user_id=user_id)
            db.add(preference)
            db.flush()
        if preference.profile_id:
            return
        default_planning = db.scalar(
            select(ProviderProfile).where(
                ProviderProfile.user_id == user_id,
                ProviderProfile.purpose == "planning",
                ProviderProfile.is_default.is_(True),
                ProviderProfile.enabled.is_(True),
            ).limit(1)
        )
        if default_planning is not None:
            preference.profile_id = default_planning.id


def get_preferences(user_id: str) -> dict:
    """新账户默认启用通知维护；已保存的开关由账户本人控制，不强制改写。"""
    _ensure_default_preference(user_id)
    with Session(_engine()) as db:
        preference = db.get(AgentPreference, user_id)
        return {"profile_id": preference.profile_id if preference else None,
                "automatic_notifications": bool(preference and preference.automatic_notifications),
                "context_budget": preference.context_budget if preference else 64000,
                "thinking_level": preference.thinking_level if preference else "medium",
                "main_conversation_id": preference.main_conversation_id if preference else None}


def save_preferences(user_id: str, payload) -> dict:
    """只允许当前账户启用过的规划模型作为主助手。"""
    if payload.profile_id:
        profile = provider_service._load_profile(user_id, str(payload.profile_id), require_enabled=True, allow_device=True)
        if profile.purpose != "planning":
            raise ApiProblem("wrong_profile_purpose", "主助手需要已启用的规划模型。", 422)
    # 可以先保存默认自动维护意愿；缺少模型时保持“等待配置”，不启动虚构运行。
    # 有明确 profile_id 时仍校验归属、启用状态和规划用途。
    with Session(_engine()) as db, db.begin():
        preference = db.get(AgentPreference, user_id)
        if preference is None:
            preference = AgentPreference(user_id=user_id)
            db.add(preference)
        preference.profile_id = str(payload.profile_id) if payload.profile_id else None
        preference.automatic_notifications = int(payload.automatic_notifications)
        preference.context_budget = payload.context_budget
        preference.thinking_level = payload.thinking_level
    return get_preferences(user_id)


def main_conversation(user_id: str) -> str:
    """每个账户固定一个主助手；普通新聊天永不替换这个指针。"""
    _ensure_default_preference(user_id)
    with Session(_engine()) as db, db.begin():
        preference = db.get(AgentPreference, user_id)
        if preference is None:
            preference = AgentPreference(user_id=user_id)
            db.add(preference)
        if not preference.profile_id:
            planning = db.scalar(select(ProviderProfile).where(ProviderProfile.user_id == user_id, ProviderProfile.purpose == "planning", ProviderProfile.enabled.is_(True)).limit(1))
            if planning is None:
                source = db.scalar(select(ProviderProfile).where(ProviderProfile.user_id == user_id, ProviderProfile.provider == "deepseek", ProviderProfile.enabled.is_(True), ProviderProfile.execution_target == "server").limit(1))
                if source:
                    # 沿用已验证的账户密文，不回显密钥；单独规划配置不改变提取设置。
                    values = {column.name: getattr(source, column.name) for column in ProviderProfile.__table__.columns}
                    values.update(id=str(uuid4()), purpose="planning", label="主助手 · DeepSeek", max_output_tokens=8192, version=1)
                    planning = ProviderProfile(**values)
                    db.add(planning)
                    db.flush()
            if planning:
                preference.profile_id = planning.id
        if preference.main_conversation_id:
            current = db.get(AgentConversation, preference.main_conversation_id)
            if current is not None and current.user_id == user_id:
                # 升级前若归档过主助手，恢复同一身份及历史，而不是随机挑选聊天。
                if current.status != 'active':
                    current.status = 'active'
                    current.archived_at_epoch = None
                    current.version += 1
                return current.id
            # 归档或失效的旧指针不能把用户带回只读对话。
            preference.main_conversation_id = None
        conversation = None
        if conversation is None:
            now = int(time.time())
            conversation = AgentConversation(id=str(uuid4()), user_id=user_id, title="主助手", status="active", version=1, created_at_epoch=now, updated_at_epoch=now)
            db.add(conversation)
            db.flush()
        preference.main_conversation_id = conversation.id
        return conversation.id


def queue_notification(user_id: str, source_id: str, change_id: str) -> None:
    """所有新通知都可整理；自动维护事务的开关独立控制业务写入。"""
    main_conversation(user_id)
    preference = get_preferences(user_id)
    if preference["profile_id"]:
        enqueue_job(_engine(), user_id=user_id, kind="agent.notification", payload={"source_id": source_id, "change_id": change_id}, idempotency_key=f"main-source:{source_id}", max_attempts=3, priority=10)


def handle_notification_job(claim) -> dict:
    """模型定夺是否登记；内容不确定时保留候选，失败时不丢弃通知。"""
    # 通知与聊天使用同一个 Pi SDK 运行机制和主会话。
    from .pi_bridge_service import run_claim
    return run_claim(claim)


def apply_notification_decision(job, arguments: dict, deterministic_change_id: str) -> dict:
    """Pi 仅给出建议参数，Flask 按持久事件授权、来源与版本执行可撤销业务操作。"""
    from ..schemas.business import ChangeSetUpdateInput, EventPatchInput
    if job.kind != "agent.notification" or not get_preferences(job.user_id)["automatic_notifications"]:
        raise ApiProblem("autopilot_disabled", "自动通知处理已关闭。", 409)
    user_id = job.user_id
    source_id, change_id = job.payload_json["source_id"], job.payload_json["change_id"]
    decision, reason = arguments.get("decision"), arguments.get("reason")
    if decision not in {"add", "update", "ignore", "skip", "handled", "review"} or not isinstance(reason, str) or not reason.strip():
        raise ApiProblem("invalid_notification_decision", "必须给出有效决定和中文理由。", 422)
    reason = reason[:900]
    confidence = arguments.get("confidence", 1.0)
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        raise ApiProblem("invalid_confidence", "置信度必须在0与1之间。", 422)
    if decision == "review" or (decision in {"add", "update"} and confidence < 0.85):
        from .notification_suggestion_service import defer
        return defer(user_id, source_id, change_id, reason, arguments.get("bundle"))
    with Session(_engine()) as db:
        source = business_service._owned(db, SourceDocument, source_id, user_id)
        change = business_service._owned(db, ChangeSet, change_id, user_id)
        previous = db.get(ChangeSet, deterministic_change_id) if decision == "update" else change
        if previous and business_service.change_applied(previous):
            return {"decision": decision, "reason": reason, "source_id": source_id, "change_set": business_service.change_set_public(previous), "already_applied": True}
        # 用户修改候选后，本轮托管不再覆盖它。
        if not business_service.change_unapplied(change) or change.version != 1:
            return {"decision": "skip", "reason": "用户已处理或修改候选，自动操作停止。", "source_id": source_id, "change_set": business_service.change_set_public(change)}
        segments = db.scalars(select(SourceSegment).where(SourceSegment.source_document_id == source_id, SourceSegment.user_id == user_id)).all()
        segment_data = [{"id": part.id, "text": part.text} for part in segments]
    applied_change = change_id
    if decision == "add":
        bundle = NormalizedBundle.model_validate(arguments.get("bundle"))
        provider_service._check_bundle_references(bundle, source_id, segment_data)
        if not bundle.events:
            decision, reason = "skip", "没有可登记的事项，已保留通知原文。" + reason
        else:
            # 自动写入只接受原文明确给出的完整日期，防止根据当前年份补猜旧消息。
            for event in bundle.events:
                if event.deadline and event.deadline.local_date is None:
                    from .notification_suggestion_service import defer
                    return defer(user_id, source_id, change_id, "截止日期尚未明确，请先核对原文。" + reason, bundle.model_dump(mode="json"))
                dates = ([event.deadline.local_date] if event.deadline and event.deadline.local_date else [])
                if event.time_range:
                    dates.extend([event.time_range.start.date(), event.time_range.end.date()])
                if any(not _explicit_date(value, source.original_text) for value in dates):
                    from .notification_suggestion_service import defer
                    return defer(user_id, source_id, change_id, "日期缺少完整依据，请先核对年份和日期。" + reason, bundle.model_dump(mode="json"))
        if decision == "add":
            # 结构化证据与最终写入同样采用既有校验；来源版本保护防止覆盖用户修正。
            business_service.apply_change_set(user_id, user_id, change_id, 1, actor_type="agent", bundle_override=bundle)
    elif decision == "update":
        patch_input = EventPatchInput.model_validate({"expected_version": arguments.get("expected_version"), **(arguments.get("patch") or {})})
        patch_data = patch_input.model_dump(mode="json", exclude_unset=True, exclude={"expected_version"})
        with Session(_engine()) as db:
            manual_change = db.scalar(
                select(OperationLog).where(
                    OperationLog.user_id == user_id,
                    OperationLog.object_type == "event",
                    OperationLog.object_id == str(arguments.get("event_id", "")),
                    OperationLog.actor_type == "user",
                    OperationLog.action.like("event.%"),
                    OperationLog.created_at_epoch >= source.created_at_epoch,
                ).order_by(OperationLog.created_at_epoch.desc()).limit(1)
            )
        if manual_change is not None:
            raise ApiProblem(
                "manual_change_conflict",
                "用户在本通知到达后已手工修改该事项，本次更新停止，已保留原字段。",
                409,
            )
        if patch_input.deadline and patch_input.deadline.local_date and not _explicit_date(patch_input.deadline.local_date, source.original_text):
            from .notification_suggestion_service import defer
            return defer(user_id, source_id, change_id, "新通知没有明确完整年月日，请核对后再决定是否更新。")
        if not patch_data.get("evidence"):
            raise ApiProblem("update_evidence_required", "自动更新必须引用新通知中的精确证据。", 422)
        # 常用要求变更统一走字段证据链；整组材料列表只产生明确的增补/撤回提案。
        if set(patch_data) <= {"deadline", "time_range", "materials", "submission", "evidence"}:
            from ..models import Event, RequirementClaim
            from ..schemas.innovation import RequirementUpdateInput
            from . import requirement_service
            with Session(_engine()) as db:
                event = business_service._owned(db, Event, str(arguments.get("event_id", "")), user_id)
                current_claims = list(db.scalars(select(RequirementClaim).where(
                    RequirementClaim.user_id == user_id, RequirementClaim.event_id == event.id, RequirementClaim.status == "active")))
                identity = {"activity": event.title, "actor": event.actor, "stage": event.stage, "scope": event.scope}
                previous_materials = list(event.materials_json)
            claims = []
            evidence = patch_data["evidence"]
            for field in ("deadline", "time_range", "submission"):
                if field in patch_data:
                    previous_claim = next((item for item in current_claims if item.field_key == field), None)
                    claims.append({"field": field, "value": patch_data[field], "relation": "replace",
                        "supersedes_id": previous_claim.id if previous_claim else None,
                        "evidence": evidence.get(field) or []})
            for material in patch_data.get("materials", []):
                if material not in previous_materials:
                    claims.append({"field": "material", "value": material, "relation": "supplement", "evidence": evidence.get("materials") or []})
            # 省略不等于撤回；没有明确撤回文字时保留原材料。
            if any(word in source.original_text for word in ("撤回", "不再需要", "取消材料", "无需提交")):
                for material in previous_materials:
                    if "materials" in patch_data and material not in patch_data["materials"]:
                        claims.append({"field": "material", "value": material, "relation": "retract", "evidence": evidence.get("materials") or []})
            if not claims:
                # 无字段变化时结束来源处理，避免留下永远等待的旧候选。
                business_service.reject_change_set(user_id, change_id, change.version)
                return {"decision": "skip", "reason": "没有明确的字段变化，保留原要求。", "source_id": source_id}
            updated = requirement_service.update_requirements(user_id, RequirementUpdateInput.model_validate({
                "source_id": source_id, "event_id": event.id, "expected_version": patch_input.expected_version,
                "identity": identity, "claims": claims, "idempotency_key": deterministic_change_id, "reason": reason}), automatic=True)
        else:
            # 身份/资格等超出首版字段范围的变更继续经过既有严格候选校验。
            updated = business_service.create_event_patch_change_set(user_id, str(arguments.get("event_id", "")), patch_input.expected_version, patch_data,
                        change_set_id=deterministic_change_id, source_document_id=source_id, actor_type="agent")
        applied_change = updated["id"]
        if not updated.get("applied", updated.get("status") == "confirmed"):
            business_service.apply_change_set(user_id, user_id, applied_change, updated["version"], actor_type="agent")
        business_service.reject_change_set(user_id, change_id, 1)
    elif decision == "ignore":
        business_service.reject_change_set(user_id, change_id, 1)
        from .notification_inbox_service import update_mailbox
        from ..schemas.assistant_workflow import MailboxInput
        update_mailbox(user_id, source_id, MailboxInput(mailbox='ignored'))
    elif decision == "handled":
        # 跨模块工具已经直接维护了任务或课程；原候选只标记处理完成，避免重复建待办。
        with Session(_engine()) as db, db.begin():
            current = business_service._owned(db, ChangeSet, change_id, user_id)
            if not business_service.change_unapplied(current) or current.version != 1:
                return {
                    "decision": "skip",
                    "reason": "候选已被用户处理或修改，自动维护停止。",
                    "source_id": source_id,
                    "change_set": business_service.change_set_public(current),
                }
            current.change_seq = business_service._next_change_seq(db, user_id)
            current.status = None
            current.version += 1
            current.confirmed_at_epoch = int(time.time())
            current.updated_at_epoch = current.confirmed_at_epoch
            current.summary = f"主助手跨模块处理：{reason}"[:1000]
            current_source = business_service._owned(db, SourceDocument, source_id, user_id)
            current_source.analysis_state = "processed"
            current_source.updated_at_epoch = current.confirmed_at_epoch
    if decision == "skip":
        # 没有可执行事项也结束本次处理，不创建待核对状态或人工确认任务。
        business_service.reject_change_set(user_id, change_id, change.version)
    with Session(_engine()) as db, db.begin():
        current = business_service._owned(db, ChangeSet, applied_change, user_id)
        if decision != "handled":
            current.summary = f"主助手{'自动登记' if decision == 'add' else '自动更新' if decision == 'update' else '忽略' if decision == 'ignore' else '已保留原文'}：{reason}"[:1000]
        result = {"decision": decision, "reason": reason, "source_id": source_id, "change_set": business_service.change_set_public(current)}
    if decision in {"add", "update", "handled"}:
        important_words = ("停课", "调课", "补课", "延期", "取消", "紧急", "重要", "截止", "地点变更")
        if any(word in source.original_text for word in important_words):
            try:
                from . import mail_service

                mail_service.queue_change_alert(
                    user_id,
                    subject=f"通知管理 · {source.title}"[:300],
                    body_text=f"Agent 已自动处理一条通知。\n\n处理结果：{result['change_set']['summary']}\n\n请登录查看详情与撤销入口。",
                    source_id=source_id,
                    change_set_id=applied_change,
                    flag_name="important_notice",
                )
            except Exception:
                pass
    return result


def _explicit_date(value, text: str) -> bool:
    """仅自动路径使用严格日期证据门槛，用户人工核对仍可填写合理年份。"""
    pattern = rf"(?<!\d){value.year}\s*(?:年|[-/.])\s*0?{value.month}\s*(?:月|[-/.])\s*0?{value.day}(?:日|号)?(?!\d)"
    return re.search(pattern, text) is not None
