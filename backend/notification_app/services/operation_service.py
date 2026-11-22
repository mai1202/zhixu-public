"""统一操作审计与活动流；日志只保存业务快照，不保存模型密钥或完整私密原文。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..models import AgentRunEvent, Job, OperationLog, SourceDocument


def _engine():
    return current_app.extensions["notification_db_engine"]


def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def record_operation(
    db: DbSession,
    *,
    user_id: str,
    actor_type: str,
    actor_id: str | None,
    action: str,
    object_type: str,
    object_id: str,
    summary: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    change_set_id: str | None = None,
    source_document_id: str | None = None,
    status: str = "applied",
    undoable: bool = False,
    created_at_epoch: int | None = None,
) -> OperationLog:
    """在业务事务内追加审计，调用方提交失败时日志会随事务一起回滚。"""
    item = OperationLog(
        id=str(uuid4()),
        user_id=user_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action[:64],
        object_type=object_type[:32],
        object_id=object_id,
        change_set_id=change_set_id,
        source_document_id=source_document_id,
        before_json=before or {},
        after_json=after or {},
        summary=summary[:1000],
        status=status,
        undoable=undoable,
        created_at_epoch=created_at_epoch or now_epoch(),
    )
    db.add(item)
    return item


def operation_public(item: OperationLog) -> dict[str, Any]:
    return {
        "id": item.id,
        "actor_type": item.actor_type,
        "actor_id": item.actor_id,
        "action": item.action,
        "object_type": item.object_type,
        "object_id": item.object_id,
        "change_set_id": item.change_set_id,
        "source_document_id": item.source_document_id,
        "before": item.before_json,
        "after": item.after_json,
        "summary": item.summary,
        "status": item.status,
        "undoable": item.undoable,
        "created_at_epoch": item.created_at_epoch,
    }


def list_operations(
    user_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
    object_type: str | None = None,
    object_id: str | None = None,
) -> dict[str, Any]:
    """按账户返回最近操作，可按对象筛选用于通知、任务或课程详情。"""
    with DbSession(_engine()) as db:
        query = select(OperationLog).where(OperationLog.user_id == user_id)
        count_query = select(func.count()).select_from(OperationLog).where(OperationLog.user_id == user_id)
        if object_type:
            query = query.where(OperationLog.object_type == object_type)
            count_query = count_query.where(OperationLog.object_type == object_type)
        if object_id:
            query = query.where(OperationLog.object_id == object_id)
            count_query = count_query.where(OperationLog.object_id == object_id)
        rows = db.scalars(
            query.order_by(OperationLog.created_at_epoch.desc(), OperationLog.id)
            .limit(max(1, min(limit, 100)))
            .offset(max(0, offset))
        ).all()
        total = int(db.scalar(count_query) or 0)
    return {
        "items": [operation_public(item) for item in rows],
        "total": total,
        "limit": max(1, min(limit, 100)),
        "offset": max(0, offset),
    }


_TOOL_LABELS = {
    "get_event_requirements": "读取当前有效要求",
    "update_event_requirements": "直接更新通知要求",
    "preview_multi_planning": "统一试算多个事务",
    # 新的学生产品工具在进度条与操作记录中同样显示中文名称。
    'get_student_journey': '查看办理路线',
    'preview_student_journey': '试算是否来得及',
    'find_student_opportunities': '发现相关机会',
    'organize_notification': '整理通知标题摘要',
    'group_notifications': '按证据归组通知',
    'read_notification_context': '查证通知前后文',
    'manage_notification_mailbox': '整理通知分组',
    'list_attachments': '查看本人附件',
    'create_attachment': '生成文件附件',
    'run_file_sandbox': '在隔离容器处理附件',
    'schedule_agent_task': '安排助手定时任务',
    'list_agent_tasks': '查看助手定时任务',
    'cancel_agent_task': '取消助手定时任务',
    'get_user_settings': '查看设置和微信群列表',
    'update_agent_settings': '修改助手设置',
    'update_tracked_chats': '更改追踪群聊',
    'request_device_sync': '请求客户端立即上传',
    'search_notifications': '查找通知邮箱',
    "search_items": "查找已有事项",
    "read_attachment": "分页读取附件",
    "search_history": "回查历史对话",
    "search_knowledge": "检索知识与资料",
    "read_knowledge": "读取资料原文",
    "search_web": "联网检索公开资料",
    "read_web_page": "读取并保存网页",
    "save_knowledge": "保存知识资料",
    "get_current_context": "读取本轮动态上下文",
    "start_plan": "建立执行计划",
    "update_plan_step": "更新计划步骤",
    "get_plan": "读取执行计划",
    "verify_citation": "校验引用定位",
    "set_agent_task_status": "暂停或恢复关注任务",
    "remember_memory": "保存长期记忆",
    "forget_memory": "删除长期记忆",
    "read_notification": "读取通知与证据",
    "search_notices": "检索已有通知",
    "get_notice": "读取通知详情",
    "update_notice": "修正通知字段",
    "archive_notice": "归档通知",
    "create_notification_source": "登记通知原文",
    "search_notification_candidates": "检索通知候选",
    "reopen_notification_candidate": "重新核对通知",
    "resolve_notification_candidate": "确认或忽略通知",
    "search_tasks": "检索相关任务",
    "get_task": "读取任务详情",
    "complete_task": "完成或重开任务",
    "merge_tasks": "合并重复任务",
    "search_courses": "检索课程实例",
    "get_course": "读取课程详情",
    "search_course_series": "检索课程系列",
    "preview_timetable_attachment": "预览课表附件",
    "create_course_series": "新增常规课程",
    "update_course_series": "修改课程系列",
    "set_course_series_status": "归档或恢复课程",
    "get_agenda": "核对日程冲突",
    "propose_schedule": "准备日程变更预览",
    "manage_personal_schedule": "维护个人日程",
    "change_course_occurrence": "调整单次课程",
    "create_course_makeup": "创建补课实例",
    "create_task": "创建任务",
    "update_task": "更新任务",
    "link_entities": "建立三端关联",
    "unlink_entity": "解除业务关联",
    "get_recent_changes": "查看最近变更",
    "get_agent_insights": "读取值班建议",
    "dismiss_insight": "关闭主动建议",
    "get_upcoming_deadlines": "检查临近截止",
    "schedule_reminder": "安排任务提醒",
    "generate_report": "生成事务报告",
    "list_reports": "查看历史报告",
    "get_report": "读取报告正文",
    "send_user_email": "发送本人提醒",
    "get_mail_preferences": "读取主动服务设置",
    "update_mail_preferences": "修改主动服务设置",
    "undo_agent_action": "撤销业务变更",
    "apply_notification_changes": "执行跨模块维护",
    "decide_notification": "保存通知处理决定",
}


def source_agent_progress(user_id: str, source_id: str) -> dict[str, Any]:
    """将后台通知运行的 Pi 事件压缩为可公开的进度，不返回提示词或模型正文。"""
    with DbSession(_engine()) as db:
        source = db.scalar(
            select(SourceDocument).where(SourceDocument.id == source_id, SourceDocument.user_id == user_id)
        )
        if source is None:
            from ..errors import ApiProblem

            raise ApiProblem("not_found", "未找到该通知来源。", 404)
        jobs = db.scalars(
            select(Job)
            .where(Job.user_id == user_id, Job.kind == "agent.notification")
            .order_by(Job.created_at_epoch.desc())
            .limit(200)
        ).all()
        job = next(
            (item for item in jobs if str((item.payload_json or {}).get("source_id", "")) == source_id),
            None,
        )
        if job is None:
            return {"status": "not_queued", "events": []}
        events = db.scalars(
            select(AgentRunEvent)
            .where(AgentRunEvent.run_id == job.id)
            .order_by(AgentRunEvent.sequence)
            .limit(100)
        ).all()
    public_events: list[dict[str, Any]] = []
    for item in events:
        payload = item.payload_json or {}
        event_type = payload.get("type")
        if event_type == "started":
            public_events.append({"sequence": item.sequence, "type": "started", "label": "开始理解通知", "status": "running"})
        elif event_type == "tool_execution_start":
            name = str(payload.get("tool_name", ""))
            public_events.append(
                {
                    "sequence": item.sequence,
                    "type": "tool_start",
                    "label": _TOOL_LABELS.get(name, name or "调用业务工具"),
                    "status": "running",
                }
            )
        elif event_type == "tool_execution_end":
            name = str(payload.get("tool_name", ""))
            failed = bool(payload.get("is_error"))
            public_events.append(
                {
                    "sequence": item.sequence,
                    "type": "tool_end",
                    "label": _TOOL_LABELS.get(name, name or "完成业务工具"),
                    "status": "failed" if failed else "succeeded",
                }
            )
        elif event_type == "error":
            public_events.append(
                {
                    "sequence": item.sequence,
                    "type": "error",
                    "label": str(payload.get("message") or "处理失败")[:500],
                    "status": "failed",
                }
            )
        elif event_type == "finished":
            public_events.append(
                {"sequence": item.sequence, "type": "finished", "label": "本轮处理结束", "status": "succeeded"}
            )
    return {
        "status": job.status,
        "error_code": job.error_code,
        "events": public_events[-60:],
    }
