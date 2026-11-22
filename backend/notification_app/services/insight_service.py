"""后台主动扫描：在没有新通知时也发现逾期、冲突、缺期限和待核对事项。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..text_sanitize import clean_display_text, clean_title
from ..models import (
    AgentInsight,
    ChangeSet,
    CourseException,
    CourseOccurrence,
    CourseSeries,
    DigestPreference,
    Task,
    User,
)
from . import operation_service, timetable_service

UTC = timezone.utc


def _engine():
    return current_app.extensions["notification_db_engine"]


def insight_public(item: AgentInsight) -> dict[str, Any]:
    return {
        "id": item.id,
        "module": item.module,
        "kind": item.kind,
        "severity": item.severity,
        "title": clean_title(item.title, "待处理事项"),
        "detail": clean_display_text(item.detail, limit=1000),
        "object_type": item.object_type,
        "object_id": item.object_id,
        "action_href": item.action_href,
        "metadata": item.metadata_json,
        "status": item.status,
        "version": item.version,
        "created_at_epoch": item.created_at_epoch,
        "updated_at_epoch": item.updated_at_epoch,
    }


def _upsert(db: DbSession, *, user_id: str, dedupe_key: str, values: dict[str, Any], now: int) -> AgentInsight:
    item = db.scalar(select(AgentInsight).where(AgentInsight.dedupe_key == dedupe_key))
    if item is None:
        item = AgentInsight(
            id=str(uuid4()),
            user_id=user_id,
            dedupe_key=dedupe_key,
            status="active",
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
            **values,
        )
        db.add(item)
        return item
    # 用户主动关闭的同一事实不反复弹出；内容发生实质变化时才重新展示。
    content_changed = (
        item.title != values.get("title")
        or item.detail != values.get("detail")
        or item.action_href != values.get("action_href")
    )
    if item.status == "dismissed" and not content_changed:
        return item
    if item.status == "resolved" or content_changed:
        item.status = "active"
        item.version += 1
    for key, value in values.items():
        setattr(item, key, value)
    item.updated_at_epoch = now
    return item


def sync_user_insights(user_id: str, *, now_epoch: int | None = None) -> dict[str, int]:
    """重算当前账户的事实型提醒，并自动关闭已解决事实。"""
    now = now_epoch or operation_service.now_epoch()
    with DbSession(_engine()) as db:
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        zone = ZoneInfo(preference.timezone if preference else "Asia/Shanghai")
        today = datetime.fromtimestamp(now, UTC).astimezone(zone).date()
    expected: dict[str, dict[str, Any]] = {}
    with DbSession(_engine()) as db:
        tasks = db.scalars(
            select(Task).where(Task.user_id == user_id, Task.archived_at_epoch.is_(None))
        ).all()
        exceptions = db.scalars(
            select(CourseException).where(
                CourseException.user_id == user_id,
                CourseException.status == "active",
            )
        ).all()
        exception_courses: dict[str, tuple[CourseSeries, CourseOccurrence]] = {}
        for exception in exceptions:
            occurrence = db.get(CourseOccurrence, exception.occurrence_id)
            series = db.get(CourseSeries, occurrence.course_series_id) if occurrence else None
            # 已归档课程或已停止演示的实例不应继续产生“当前课程变化”预警。
            if (
                occurrence
                and series
                and series.status == "active"
                and occurrence.status in {"scheduled", "rescheduled"}
            ):
                exception_courses[exception.id] = (series, occurrence)

    for task in tasks:
        if task.status != "open":
            continue
        due_text = (task.due_json or {}).get("local_date")
        try:
            due_date = date.fromisoformat(str(due_text)) if due_text else None
        except ValueError:
            due_date = None
        if due_date and due_date < today:
            key = f"task:{task.id}:overdue:{due_date.isoformat()}"
            expected[key] = {
                "module": "task",
                "kind": "overdue",
                "severity": "critical",
                "title": f"{task.title} 已经逾期"[:500],
                "detail": f"截止日期为 {due_date.isoformat()}，任务仍未完成。可以重新安排、完成或取消。",
                "object_type": "task",
                "object_id": task.id,
                "action_href": f"/tasks/{task.id}",
                "metadata_json": {"due_date": due_date.isoformat(), "priority": task.priority},
            }
        elif due_date and due_date - today in {timedelta(0), timedelta(days=1)}:
            key = f"task:{task.id}:deadline:{due_date.isoformat()}"
            expected[key] = {
                "module": "task",
                "kind": "deadline_soon",
                "severity": "warning",
                "title": f"{task.title} {'今天' if due_date == today else '明天'}截止"[:500],
                "detail": f"当前优先级：{task.priority}。建议确认材料是否齐全，并按剩余时间安排执行。",
                "object_type": "task",
                "object_id": task.id,
                "action_href": f"/tasks/{task.id}",
                "metadata_json": {"due_date": due_date.isoformat(), "priority": task.priority},
            }
        if due_date is None and task.priority in {"high", "urgent"}:
            key = f"task:{task.id}:missing_deadline"
            expected[key] = {
                "module": "task",
                "kind": "missing_deadline",
                "severity": "warning",
                "title": f"{task.title} 缺少明确期限"[:500],
                "detail": "这是高优先级任务，但没有可核验的截止时间。可补充期限或请 Agent 从原始通知中复核。",
                "object_type": "task",
                "object_id": task.id,
                "action_href": f"/tasks/{task.id}",
                "metadata_json": {"priority": task.priority},
            }

    for exception in exceptions:
        course = exception_courses.get(exception.id)
        if course is None:
            continue
        series, occurrence = course
        key = f"course:{occurrence.id}:exception:{exception.id}"
        expected[key] = {
            "module": "course",
            "kind": "course_change",
            "severity": "info",
            "title": f"{series.display_name} 有单次课程调整"[:500],
            "detail": f"{occurrence.local_date} {occurrence.display_start}–{occurrence.display_end}；"
            f"{'地点：' + (occurrence.room_override or series.room or '未提供') if exception.kind == 'room_change' else exception.reason}",
            "object_type": "course_occurrence",
            "object_id": occurrence.id,
            "action_href": f"/schedule?focus={occurrence.id}",
            "metadata_json": {"notice_id": exception.notice_id, "kind": exception.kind},
        }

    try:
        schedule = timetable_service.list_schedule(user_id, today, today + timedelta(days=13))
        for index, conflict in enumerate(schedule.get("conflicts", [])[:20]):
            ordered_ids = sorted((str(conflict["first"]["id"]), str(conflict["second"]["id"])))
            key = f"conflict:{conflict['local_date']}:{ordered_ids[0]}:{ordered_ids[1]}:{conflict['start_at']}"
            expected[key] = {
                "module": "course" if any(
                    item.get("id") in {conflict["first"]["id"], conflict["second"]["id"]}
                    and item.get("item_type") == "course"
                    for item in schedule.get("items", [])
                ) else "task",
                "kind": "schedule_conflict",
                "severity": "critical",
                "title": f"{conflict['first']['title']} 与 {conflict['second']['title']} 时间冲突"[:500],
                "detail": f"{conflict['local_date']} {conflict['start_at']}–{conflict['end_at']} 出现重叠，请确认是否调整。",
                "object_type": "schedule_conflict",
                "object_id": str(index),
                "action_href": "/schedule",
                "metadata_json": conflict,
            }
    except ApiProblem:
        pass

    with DbSession(_engine()) as db, db.begin():
        existing = db.scalars(select(AgentInsight).where(AgentInsight.user_id == user_id)).all()
        for key, values in expected.items():
            _upsert(db, user_id=user_id, dedupe_key=key, values=values, now=now)
        for item in existing:
            if item.status == "active" and item.dedupe_key not in expected:
                item.status = "resolved"
                item.version += 1
                item.updated_at_epoch = now
    # 紧急事实可以走用户已启用的主动邮件通道；同一组建议使用稳定去重键。
    try:
        with DbSession(_engine()) as db:
            critical = db.scalars(
                select(AgentInsight).where(
                    AgentInsight.user_id == user_id,
                    AgentInsight.status == "active",
                    AgentInsight.severity == "critical",
                ).order_by(AgentInsight.updated_at_epoch.desc()).limit(10)
            ).all()
        if critical:
            from . import mail_service

            groups: dict[str, list[AgentInsight]] = {}
            for item in critical:
                # 冲突预警、截止提醒和重要通知各有独立用户开关，不能混用同一开关。
                if item.kind == "schedule_conflict":
                    flag_name = "conflict_warning"
                elif item.module == "task":
                    flag_name = "task_reminder"
                else:
                    flag_name = "important_notice"
                groups.setdefault(flag_name, []).append(item)
            for flag_name, items in groups.items():
                key = "insight:" + hashlib.sha256(
                    ",".join(sorted(item.id for item in items)).encode()
                ).hexdigest()[:32]
                mail_service.queue_change_alert(
                    user_id,
                    subject=f"通知管理 · {len(items)} 项紧急事务",
                    body_text="\n".join(
                        [
                            "Agent 在后台主动检查中发现以下紧急事项：",
                            *(f"- {item.title}：{item.detail}" for item in items),
                            "",
                            "请登录课程、事务或通知模块核对；邮件只发送摘要。",
                        ]
                    ),
                    source_id=str(items[0].object_id or ""),
                    change_set_id=key,
                    flag_name=flag_name,
                )
    except Exception:
        # 邮件不影响洞察持久化，也不阻塞下一次扫描。
        pass
    return {"active": len(expected)}


def scan_all_users(engine, *, now_epoch: int | None = None) -> int:
    """worker 周期扫描所有账户；单个账户异常不影响其他账户。"""
    now = now_epoch or operation_service.now_epoch()
    with DbSession(engine) as db:
        user_ids = db.scalars(select(User.id).limit(1000)).all()
    count = 0
    for user_id in user_ids:
        try:
            count += sync_user_insights(user_id, now_epoch=now)["active"]
        except Exception:
            continue
    return count


def list_insights(
    user_id: str,
    *,
    module: str | None = None,
    include_dismissed: bool = False,
) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        statement = select(AgentInsight).where(AgentInsight.user_id == user_id)
        if module in {"course", "task", "notice"}:
            statement = statement.where(AgentInsight.module == module)
        if not include_dismissed:
            statement = statement.where(AgentInsight.status == "active")
        rows = db.scalars(statement.order_by(AgentInsight.updated_at_epoch.desc()).limit(100)).all()
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    rows.sort(key=lambda item: (severity_order.get(item.severity, 3), -item.updated_at_epoch))
    return {
        "items": [insight_public(item) for item in rows],
        "counts": {
            "critical": sum(1 for item in rows if item.severity == "critical" and item.status == "active"),
            "warning": sum(1 for item in rows if item.severity == "warning" and item.status == "active"),
            "info": sum(1 for item in rows if item.severity == "info" and item.status == "active"),
        },
    }


def dismiss_insight(user_id: str, insight_id: str, expected_version: int) -> dict[str, Any]:
    with DbSession(_engine()) as db, db.begin():
        item = db.scalar(
            select(AgentInsight).where(AgentInsight.id == insight_id, AgentInsight.user_id == user_id)
        )
        if item is None:
            raise ApiProblem("not_found", "建议不存在。", 404)
        if item.version != expected_version:
            raise ApiProblem("version_conflict", "建议已更新，请刷新后重试。", 409)
        item.status = "dismissed"
        item.version += 1
        item.updated_at_epoch = operation_service.now_epoch()
        return insight_public(item)
