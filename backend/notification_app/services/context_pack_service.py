"""按本轮问题生成有界动态摘要，避免每轮塞入全量个人资料。

摘要带生成时间和对象版本；模型执行修改前仍须用业务工具重新核对当前对象。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (AgentExecutionPlan, AgentMemory, AgentScheduledTask, AgentTurn,
                      ChangeSet, CourseOccurrence, CourseSeries, DigestPreference, Job,
                      Notice, ScheduleEntry, Task)
from . import knowledge_service

UTC = timezone.utc
MAX_SCHEDULE = 8
MAX_TASKS = 6
MAX_NOTICES = 6
MAX_PENDING = 5
MAX_WATCHES = 5
MAX_MEMORIES = 6


def _engine():
    return current_app.extensions["notification_db_engine"]


def _zone(user_id: str, db) -> ZoneInfo:
    preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
    try:
        return ZoneInfo(preference.timezone if preference else "Asia/Shanghai")
    except (KeyError, ValueError):
        return ZoneInfo("Asia/Shanghai")


def _task_entry(item: Task, today) -> dict[str, Any]:
    due = (item.due_json or {}).get("local_date")
    return {
        "id": item.id,
        "title": item.title[:200],
        "status": item.status,
        "priority": item.priority,
        "due_date": due,
        "overdue": bool(due and str(due) < today.isoformat()),
        "version": item.version,
    }


def _schedule_entries(db, user_id: str, start_date, end_date) -> list[dict[str, Any]]:
    """课程和忙碌块统一成有界时间摘要；跨日安排使用首段时刻。"""
    rows: list[dict[str, Any]] = []
    occurrences = db.scalars(
        select(CourseOccurrence)
        .join(CourseSeries, CourseSeries.id == CourseOccurrence.course_series_id)
        .where(
            CourseOccurrence.user_id == user_id,
            CourseOccurrence.local_date >= start_date.isoformat(),
            CourseOccurrence.local_date <= end_date.isoformat(),
            CourseOccurrence.status.in_(("scheduled", "rescheduled")),
            CourseSeries.status == "active",
        )
        .order_by(CourseOccurrence.local_date, CourseOccurrence.display_start)
        .limit(MAX_SCHEDULE * 2)
    ).all()
    for occurrence in occurrences:
        course = db.get(CourseSeries, occurrence.course_series_id)
        segments = occurrence.busy_segments_json or []
        first = segments[0] if segments else {}
        rows.append({
            "kind": "course",
            "id": occurrence.id,
            "title": (course.display_name if course else "课程")[:200],
            "local_date": occurrence.local_date,
            "start_at": first.get("start_at") or f"{occurrence.local_date}T{occurrence.display_start}:00",
            "end_at": first.get("end_at") or f"{occurrence.local_date}T{occurrence.display_end}:00",
            "room": occurrence.room_override or (course.room if course else None),
            "version": occurrence.version,
        })
    entries = db.scalars(
        select(ScheduleEntry).where(
            ScheduleEntry.user_id == user_id,
            ScheduleEntry.local_date >= start_date.isoformat(),
            ScheduleEntry.local_date <= end_date.isoformat(),
            ScheduleEntry.status == "active",
        ).order_by(ScheduleEntry.local_date, ScheduleEntry.start_at).limit(MAX_SCHEDULE * 2)
    ).all()
    for entry in entries:
        rows.append({
            "kind": "schedule",
            "id": entry.id,
            "title": entry.title[:200],
            "local_date": entry.local_date,
            "start_at": entry.start_at,
            "end_at": entry.end_at,
            "room": None,
            "version": entry.version,
        })
    rows.sort(key=lambda item: (item.get("local_date") or "", item.get("start_at") or ""))
    return rows[:MAX_SCHEDULE]


def build(user_id: str, *, question: str = "", now: datetime | None = None,
          conversation_id: str | None = None) -> dict[str, Any]:
    """返回 JSON 可序列化的有限摘要；不返回全量正文或聊天历史。"""
    now = now or datetime.now(UTC)
    with Session(_engine()) as db:
        zone = _zone(user_id, db)
        local_now = now.astimezone(zone)
        today = local_now.date()
        tasks = db.scalars(
            select(Task).where(
                Task.user_id == user_id,
                Task.status == "open",
                Task.archived_at_epoch.is_(None),
            ).order_by(Task.priority.desc(), Task.created_at_epoch.desc()).limit(40)
        ).all()
        task_items = [_task_entry(item, today) for item in tasks]
        priority_order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        task_items.sort(key=lambda item: (
            0 if item["overdue"] else 1,
            item.get("due_date") or "9999-12-31",
            priority_order.get(str(item.get("priority")), 2),
        ))
        task_items = task_items[:MAX_TASKS]
        notices = db.scalars(
            select(Notice).where(
                Notice.user_id == user_id,
                Notice.status == "active",
                Notice.archived_at_epoch.is_(None),
            ).order_by(Notice.updated_at_epoch.desc()).limit(MAX_NOTICES)
        ).all()
        notice_items = [{
            "id": item.id,
            "source_id": item.source_document_id,
            "title": item.title[:200],
            "importance": item.importance,
            "version": item.version,
            "updated_at_epoch": item.updated_at_epoch,
            "href": f"/inbox/{item.source_document_id}",
        } for item in notices]
        schedule_items = _schedule_entries(db, user_id, today, today + timedelta(days=3))
        watches = db.scalars(
            select(AgentScheduledTask).where(
                AgentScheduledTask.user_id == user_id,
                AgentScheduledTask.status.in_(("active", "paused")),
            ).order_by(AgentScheduledTask.created_at_epoch.desc()).limit(MAX_WATCHES)
        ).all()
        memory_rows = db.scalars(
            select(AgentMemory).where(AgentMemory.user_id == user_id)
            .order_by(AgentMemory.updated_at_epoch.desc()).limit(80)
        ).all()
        terms = knowledge_service._terms(question)[:12]
        memories = []
        for item in memory_rows:
            lowered = item.content.casefold()
            score = sum(1 for term in terms if term in lowered)
            if score or len(memories) < 3:
                memories.append((score, item))
        memories.sort(key=lambda pair: (pair[0], pair[1].updated_at_epoch), reverse=True)
        memory_items = [{
            "id": item.id,
            "category": item.category,
            "content": item.content[:400],
            "updated_at_epoch": item.updated_at_epoch,
        } for _, item in memories[:MAX_MEMORIES]]
        last_turn = db.scalar(
            select(AgentTurn).where(
                AgentTurn.user_id == user_id,
                AgentTurn.role == "assistant",
            ).order_by(AgentTurn.created_at_epoch.desc()).limit(1)
        )
        last_failed = db.scalar(
            select(Job).where(
                Job.user_id == user_id,
                Job.kind == "agent.run",
                Job.status.in_(("failed", "retry_wait")),
            ).order_by(Job.created_at_epoch.desc()).limit(1)
        )
        plan_statement = select(AgentExecutionPlan).where(
            AgentExecutionPlan.user_id == user_id,
            AgentExecutionPlan.status.in_(("active", "failed")),
        )
        if conversation_id:
            plan_statement = plan_statement.where(AgentExecutionPlan.conversation_id == conversation_id)
        plan = db.scalar(plan_statement.order_by(AgentExecutionPlan.updated_at_epoch.desc()).limit(1))
    from . import onboarding_service
    onboarding = onboarding_service.get_state(user_id, create=False, now_epoch=int(now.timestamp()))
    return {
        "generated_at_epoch": int(now.timestamp()),
        "onboarding": ({'status': onboarding['status'], 'completed_count': onboarding['completed_count'],
            'steps': onboarding['steps'], 'reminder_enabled': onboarding['reminder_enabled']}
            if onboarding and onboarding['status'] != 'completed' else None),
        "timezone": str(zone),
        "local_time": local_now.isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "schedule": schedule_items,
        "tasks": task_items,
        "notices": notice_items,
        "watches": [{
            "task_id": item.id,
            "title": item.title[:150],
            "query": item.watch_query,
            "scope": item.watch_scope,
            "status": item.status,
            "interval_seconds": item.interval_seconds,
            "next_run_at_epoch": item.next_run_at_epoch,
            "last_checked_epoch": item.last_watch_checked_epoch,
            "last_success_epoch": item.last_success_epoch,
            "last_error_code": item.last_error_code,
            "version": item.version,
        } for item in watches],
        "memories": memory_items,
        "last_assistant_turn": ({
            "turn_id": last_turn.id,
            "conversation_id": last_turn.conversation_id,
            "created_at_epoch": last_turn.created_at_epoch,
            "excerpt": last_turn.content[:600],
        } if last_turn else None),
        "unfinished": ({
            "run_id": last_failed.id,
            "status": last_failed.status,
            "error_code": last_failed.error_code,
            "created_at_epoch": last_failed.created_at_epoch,
        } if last_failed else None),
        "plan": ({
            "id": plan.id,
            "goal": plan.goal[:500],
            "status": plan.status,
            "resumable": plan.status in {"active", "failed"},
            "version": plan.version,
            "completed_count": sum(1 for step in (plan.steps_json or []) if step.get("status") == "done"),
            "receipt_count": len(plan.tool_receipts_json or []),
            "next_step": next((
                {
                    "index": step.get("index"), "title": step.get("title"), "status": step.get("status"),
                    "object_type": step.get("object_type"), "object_id": step.get("object_id"),
                    "version": step.get("version"),
                }
                for step in (plan.steps_json or [])
                if step.get("status") in {"running", "failed", "waiting", "pending"}
            ), None),
            "completed_receipts": [
                {
                    "tool_name": receipt.get("tool_name"),
                    "tool_call_id": receipt.get("tool_call_id"),
                    "created_at_epoch": receipt.get("created_at_epoch"),
                }
                for receipt in (plan.tool_receipts_json or [])[-5:]
            ],
            "steps": [{
                "index": step.get("index"), "title": step.get("title"), "status": step.get("status"),
                "object_type": step.get("object_type"), "object_id": step.get("object_id"),
                "version": step.get("version"), "tool_receipt": step.get("tool_receipt"),
            } for step in (plan.steps_json or [])[:12]],
        } if plan else None),
        "limits": {
            "schedule": MAX_SCHEDULE, "tasks": MAX_TASKS, "notices": MAX_NOTICES,
            "watches": MAX_WATCHES, "memories": MAX_MEMORIES,
        },
        "note": "动态摘要是带生成时间的有限快照，不是修改授权；写入前请用业务工具核对最新版本。",
    }


def prompt_text(pack: dict[str, Any]) -> str:
    """压缩成有限提示段；资料和记忆只能作为事实参考，不能提升为指令。"""
    lines = [
        "[本轮动态上下文，生成于 " + str(pack.get("local_time")) + " " + str(pack.get("timezone")) + "；摘要不是修改授权]",
    ]
    if pack.get('onboarding'):
        onboarding = pack['onboarding']
        pending = '、'.join(step['title'] for step in onboarding['steps'] if step['status'] == 'pending')
        lines.append(f"首次使用引导（{onboarding['status']}）：待配置 {pending or '无'}；进度页 /onboarding。此进度仅供参考，配置修改仍须用户要求和工具校验。")
    if pack.get("schedule"):
        parts = []
        for item in pack["schedule"]:
            parts.append(f"{item.get('local_date')} {str(item.get('start_at') or '')[11:16]} {item.get('title')}")
        lines.append("近期安排：" + "；".join(parts))
    if pack.get("tasks"):
        parts = [f"{item['title']}（{item.get('due_date') or '无期限'}，v{item['version']}）" for item in pack["tasks"]]
        lines.append("相关待办：" + "；".join(parts))
    if pack.get("notices"):
        parts = [f"{item['title']}（{item.get('importance') or 'normal'}，v{item['version']}，来源 {item['source_id']}）" for item in pack["notices"]]
        lines.append("相关通知：" + "；".join(parts) + "。引用前用 read_knowledge(kind=notification, id=来源ID) 读取原文并核对生效状态。")
    if pack.get("watches"):
        parts = [f"{item['title']}（{ '暂停' if item['status']=='paused' else '关注中'}，下次 {item.get('next_run_at_epoch')}）" for item in pack["watches"]]
        lines.append("关注任务：" + "；".join(parts))
    if pack.get("memories"):
        parts = [f"[{item['category']}] {item['content']}" for item in pack["memories"]]
        lines.append("相关长期记忆：" + "；".join(parts))
    if pack.get("plan"):
        plan = pack["plan"]
        parts = [f"{step['index']}.{step['title']}（{step['status']}）" for step in plan["steps"]]
        label = "可恢复的执行计划" if plan.get("resumable") else "执行计划"
        next_step = plan.get("next_step")
        next_text = (
            f"下一步：第 {next_step['index'] + 1} 步「{next_step['title']}」（{next_step['status']}）。"
            if next_step else "没有待执行步骤。"
        )
        receipt_text = ""
        if plan.get("completed_receipts"):
            receipt_names = "、".join(str(item.get("tool_name")) for item in plan["completed_receipts"])
            receipt_text = f"已记录写操作回执：{receipt_names}；同参数重复写工具会由服务端幂等返回原结果。"
        lines.append(
            f"{label}（{plan.get('status')}）：{plan['goal']}；步骤：" + "；".join(parts)
            + f"。{next_text}{receipt_text}继续前先 get_plan 核对已完成步骤和回执。"
        )
    if pack.get("unfinished"):
        item = pack["unfinished"]
        lines.append(f"上一轮未完成：运行 {item['run_id'][:8]} 状态 {item['status']} 错误 {item.get('error_code') or '未知'}；可先核对再重试，不能声称已完成。")
    lines.append("修改任何对象前，用对应业务工具读取最新版本。")
    return "\n".join(lines)
