"""基于实时业务事实生成日报、周报，并记录生成与投递状态。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..jobs.queue import ClaimedJob, enqueue_job
from ..models import (
    ActivityReport,
    ChangeSet,
    CourseException,
    CourseOccurrence,
    CourseSeries,
    DigestPreference,
    Notice,
    OperationLog,
    Task,
    User,
    VerifiedAddress,
)
from . import business_service, operation_service, timetable_service
from .mail_service import DEFAULT_INCLUDE, DEFAULT_NOTIFICATION_FLAGS, _create_outbox

UTC = timezone.utc


def _engine():
    return current_app.extensions["notification_db_engine"]


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.isoweekday() - 1)
    return start, start + timedelta(days=6)


def _preference(user_id: str) -> DigestPreference:
    from . import mail_service

    # 用户可能直接请求报告而尚未打开邮件设置；这里复用统一初始化，避免 409。
    mail_service.get_preferences(user_id)
    with DbSession(_engine()) as db:
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
    if preference is None:
        raise ApiProblem("mail_settings_unavailable", "请先打开邮件设置完成初始化。", 409)
    return preference


def _task_line(task: Task, *, prefix: str = "") -> str:
    due = task.due_json or {}
    deadline = due.get("raw") or due.get("local_date") or "期限未提供"
    priority = {"urgent": "紧急", "high": "重要", "normal": "普通", "low": "低"}[task.priority]
    return f"- {prefix}{task.title}｜{priority}｜{deadline}"


def _schedule_rows(user_id: str, start: date, end: date) -> dict[str, Any]:
    return timetable_service.list_schedule(user_id, start, end)


def _collect_facts(
    user_id: str,
    *,
    kind: str,
    period_start: date,
    period_end: date,
    timezone_name: str,
    include: dict[str, bool],
) -> dict[str, Any]:
    """所有栏目都来自数据库；缺失值写明未知，不生成虚构学习进度。"""
    zone = ZoneInfo(timezone_name)
    start_epoch = int(datetime.combine(period_start, time.min, tzinfo=zone).timestamp())
    end_epoch = int(datetime.combine(period_end + timedelta(days=1), time.min, tzinfo=zone).timestamp())
    schedule = _schedule_rows(user_id, period_start, period_end)
    today = datetime.now(UTC).astimezone(zone).date()
    with DbSession(_engine()) as db:
        tasks = db.scalars(select(Task).where(Task.user_id == user_id, Task.archived_at_epoch.is_(None))).all()
        notices = db.scalars(
            select(Notice).where(
                Notice.user_id == user_id,
                Notice.archived_at_epoch.is_(None),
                Notice.created_at_epoch >= start_epoch,
                Notice.created_at_epoch < end_epoch,
            ).order_by(Notice.created_at_epoch.desc())
        ).all()
        changes = db.scalars(
            select(ChangeSet).where(
                ChangeSet.user_id == user_id,
                ChangeSet.created_at_epoch >= start_epoch,
                ChangeSet.created_at_epoch < end_epoch,
            ).order_by(ChangeSet.created_at_epoch.desc()).limit(200)
        ).all()
        exceptions = db.scalars(
            select(CourseException).where(
                CourseException.user_id == user_id,
                CourseException.created_at_epoch >= start_epoch,
                CourseException.created_at_epoch < end_epoch,
            ).order_by(CourseException.created_at_epoch.desc())
        ).all()
        operations = db.scalars(
            select(OperationLog).where(
                OperationLog.user_id == user_id,
                OperationLog.created_at_epoch >= start_epoch,
                OperationLog.created_at_epoch < end_epoch,
            ).order_by(OperationLog.created_at_epoch.desc()).limit(300)
        ).all()
        user = db.scalar(select(User).where(User.id == user_id))
        # 课程标题在例外记录里只有 ID；一次性映射避免逐条查询。
        exception_titles: dict[str, tuple[str, CourseOccurrence]] = {}
        for item in exceptions:
            occurrence = db.get(CourseOccurrence, item.occurrence_id)
            series = db.get(CourseSeries, occurrence.course_series_id) if occurrence else None
            if occurrence and series:
                exception_titles[item.id] = (series.display_name, occurrence)

    def due_date(task: Task) -> date | None:
        value = (task.due_json or {}).get("local_date")
        try:
            return date.fromisoformat(str(value)) if value else None
        except ValueError:
            return None

    tasks_in_period = [task for task in tasks if (value := due_date(task)) and period_start <= value <= period_end]
    open_tasks = [task for task in tasks if task.status == "open"]
    overdue = [task for task in tasks if task.status == "open" and (value := due_date(task)) and value < today]
    completed = [
        task for task in tasks
        if task.status == "completed" and task.completed_at_epoch
        and start_epoch <= task.completed_at_epoch < end_epoch
    ]
    day_courses = [
        item for item in schedule["items"]
        if item.get("item_type") == "course" and item.get("local_date") == period_start.isoformat()
    ] if kind == "daily" else [
        item for item in schedule["items"] if item.get("item_type") == "course"
    ]
    tomorrow = period_start + timedelta(days=1)
    tomorrow_courses = [
        item for item in schedule["items"]
        if item.get("item_type") == "course" and item.get("local_date") == tomorrow.isoformat()
    ]
    next_week_start = _week_bounds(period_end)[0] + timedelta(days=7)
    next_week_end = next_week_start + timedelta(days=6)
    next_schedule = _schedule_rows(user_id, next_week_start, next_week_end)
    next_courses = [item for item in next_schedule["items"] if item.get("item_type") == "course"]
    next_deadlines = [
        task for task in open_tasks
        if (value := due_date(task)) and next_week_start <= value <= next_week_end
    ]
    conflicts = schedule.get("conflicts", [])
    return {
        "courses": day_courses if include.get("courses", True) else [],
        "today_courses": [
            item for item in schedule["items"]
            if item.get("item_type") == "course" and item.get("local_date") == today.isoformat()
        ] if kind == "daily" and include.get("courses", True) else [],
        "arrangements": [
            item for item in schedule["items"] if item.get("item_type") == "personal"
        ] if include.get("arrangements", True) else [],
        "tasks_due": tasks_in_period if include.get("deadlines", True) else [],
        "open_tasks": open_tasks if include.get("deadlines", True) else [],
        "overdue": overdue,
        "completed": completed,
        "notices": [business_service.notice_public(item) for item in notices] if include.get("changes", True) else [],
        "changes": [
            {"id": item.id, "summary": item.summary, "change_seq": item.change_seq}
            for item in changes
        ] if include.get("changes", True) else [],
        "course_changes": [
            {
                "id": item.id,
                "title": exception_titles.get(item.id, ("未知课程", None))[0],
                "local_date": (
                    exception_titles[item.id][1].local_date
                    if item.id in exception_titles
                    else None
                ),
                "kind": item.kind,
                "occurrence_id": item.occurrence_id,
                "replacement": item.replacement_json,
                "reason": item.reason,
                "notice_id": item.notice_id,
            }
            for item in exceptions
            if item.status == "active"
        ],
        "tomorrow_courses": tomorrow_courses if include.get("tomorrow", True) else [],
        "next_courses": next_courses if kind == "weekly" or include.get("tomorrow", True) else [],
        "next_deadlines": next_deadlines,
        "conflicts": conflicts,
        "agent_actions": [operation_service.operation_public(item) for item in operations],
        "agent_summary": {
            "agent_actions": sum(1 for item in operations if item.actor_type == "agent"),
            "automatic_changes": sum(1 for item in operations if item.action.startswith(("course.", "task.")) and item.actor_type == "agent"),
            "manual_interventions": sum(1 for item in operations if item.actor_type == "user" and item.undoable),
            "processed_notices": len(notices),
            "total_changes": user.change_sequence if user else 0,
        } if include.get("agent_summary", True) else {},
        "suggestions": [] if not include.get("suggestions", True) else _suggestions(
            overdue=overdue,
            tasks_due=tasks_in_period,
            conflicts=conflicts,
            course_changes=return_course_changes(exceptions, exception_titles),
            next_deadlines=next_deadlines,
        ),
    }


def return_course_changes(exceptions: list[CourseException], titles: dict[str, tuple[str, CourseOccurrence]]) -> list[dict[str, Any]]:
    """将课程例外科目标题化，供建议生成和网页展示复用。"""
    return [
        {
            "id": item.id,
            "title": titles.get(item.id, ("未知课程", None))[0],
            "kind": item.kind,
            "occurrence_id": item.occurrence_id,
            "notice_id": item.notice_id,
        }
        for item in exceptions
        if item.status == "active"
    ]


def _suggestions(
    *,
    overdue: list[Task],
    tasks_due: list[Task],
    conflicts: list[dict[str, Any]],
    course_changes: list[dict[str, Any]],
    next_deadlines: list[Task],
) -> list[str]:
    """只给出可由事实直接支持的建议，不推断学习效果或未提供的信息。"""
    suggestions: list[str] = []
    if overdue:
        suggestions.append(f"先处理 {len(overdue)} 个已逾期任务，优先核对期限是否已被新通知更新。")
    high = [task for task in tasks_due if task.priority in {"urgent", "high"} and task.status == "open"]
    if high:
        suggestions.append(f"本周期有 {len(high)} 个高优先级任务，建议在最近一段完整空档内完成。")
    if conflicts:
        suggestions.append(f"日程中有 {len(conflicts)} 处时间冲突，需要确认是否保留重叠或调整安排。")
    if course_changes:
        suggestions.append(f"有 {len(course_changes)} 项课程单次调整，请以最新通知和地点为准。")

    if next_deadlines:
        suggestions.append(f"下周有 {len(next_deadlines)} 个截止事项，可提前拆分准备任务。")
    if not suggestions:
        suggestions.append("当前没有逾期、冲突或高优先级预警，可继续保持现有安排。")
    return suggestions


def _schedule_line(item: dict[str, Any]) -> str:
    return f"- {item.get('local_date', '')} {item.get('start_at', '')}–{item.get('end_at', '')} · {item.get('title', '未命名课程')} · {item.get('room') or '地点未提供'}"


def _body(kind: str, period_start: date, period_end: date, facts: dict[str, Any]) -> tuple[str, str]:
    if kind == "daily":
        title = f"通知管理 · {period_start.isoformat()} 日报"
        chunks = [title, ""]
        chunks.extend(["今日课程", *(_schedule_line(item) for item in facts["courses"])] if facts["courses"] else ["今日课程", "- 未发现已登记课程"])
        chunks.extend(["", "今日待办与截止"])
        due = [task for task in facts["tasks_due"] if task.status == "open"]
        chunks.extend(_task_line(task) for task in due) if due else chunks.append("- 未发现今日截止任务")
        chunks.extend(["", "逾期与重点预警"])
        warnings = facts["overdue"] + facts["conflicts"]
        if facts["overdue"]:
            chunks.extend(_task_line(task, prefix="逾期 · ") for task in facts["overdue"])
        if facts["conflicts"]:
            chunks.extend(
                f"- 时间冲突：{item['local_date']} {item['first']['title']} / {item['second']['title']}"
                for item in facts["conflicts"]
            )
        if not warnings:
            chunks.append("- 未发现逾期或时间冲突")
        chunks.extend(["", "今日新通知与变更"])
        if facts["notices"] or facts["changes"]:
            chunks.extend(f"- 新通知：{item['title']}" for item in facts["notices"])
            chunks.extend(f"- {item['summary']}" for item in facts["changes"])
        else:
            chunks.append("- 今日暂无新通知或业务变更")
        chunks.extend(["", "明日预警"])
        chunks.extend(_schedule_line(item) for item in facts["tomorrow_courses"]) if facts["tomorrow_courses"] else chunks.append("- 明日暂无已登记课程")
        chunks.extend(_task_line(task, prefix="明日截止 · ") for task in facts["tasks_due"] if (task.due_json or {}).get("local_date") == (period_start + timedelta(days=1)).isoformat())
        chunks.extend(["", "Agent 自动处理摘要"])
        summary = facts["agent_summary"]
        chunks.append(
            f"- 本周期处理通知 {summary.get('processed_notices', 0)} 条，Agent 操作 {summary.get('agent_actions', 0)} 次，"
            f"其中自动维护任务/课程 {summary.get('automatic_changes', 0)} 次。"
        )
        chunks.extend(["", "建议", *(f"- {item}" for item in facts["suggestions"])])
    else:
        title = f"通知管理 · {period_start.isoformat()}—{period_end.isoformat()} 周报"
        chunks = [title, "", "本周回顾"]
        chunks.append(f"- 已完成任务 {len(facts['completed'])} 个，未完成 {len(facts['open_tasks'])} 个。")
        chunks.extend(_task_line(task, prefix="已完成 · ") for task in facts["completed"])
        chunks.extend(["", "本周新增通知与变更"])
        chunks.extend(f"- {item['title']}" for item in facts["notices"]) if facts["notices"] else chunks.append("- 本周暂无新增通知")
        chunks.extend(f"- {item['summary']}" for item in facts["changes"])
        chunks.extend(["", "课程调整"])
        chunks.extend(
            f"- {item['title']}：{item['kind']}（有通知依据：{'是' if item['notice_id'] else '否'}）"
            for item in facts["course_changes"]
        ) if facts["course_changes"] else chunks.append("- 本周无课程单次调整")
        chunks.extend(["", "下周计划"])
        chunks.extend(_schedule_line(item) for item in facts["next_courses"]) if facts["next_courses"] else chunks.append("- 下周暂无已登记课程")
        chunks.extend(_task_line(task, prefix="下周截止 · ") for task in facts["next_deadlines"]) if facts["next_deadlines"] else chunks.append("- 下周暂无已登记截止任务")
        chunks.extend(["", "重点预警"])
        if facts["overdue"]:
            chunks.extend(_task_line(task, prefix="仍逾期 · ") for task in facts["overdue"])
        if facts["conflicts"]:
            chunks.extend(
                f"- 时间冲突：{item['local_date']} {item['first']['title']} / {item['second']['title']}"
                for item in facts["conflicts"]
            )
        if not facts["overdue"] and not facts["conflicts"]:
            chunks.append("- 未发现逾期或时间冲突")
        chunks.extend(["", "Agent 自动处理摘要"])
        summary = facts["agent_summary"]
        chunks.append(
            f"- 处理通知 {summary.get('processed_notices', 0)} 条，Agent 操作 {summary.get('agent_actions', 0)} 次，"
            f"人工可撤销操作 {summary.get('manual_interventions', 0)} 次。"
        )
        chunks.extend(["", "个性化行动建议", *(f"- {item}" for item in facts["suggestions"])])
    chunks.extend(["", "说明：报告只汇总已登记记录；未知信息按缺失字段列出。"])
    return title, "\n".join(chunks)


def generate_report(
    user_id: str,
    *,
    kind: str,
    period_start: date | None = None,
    send: bool = False,
) -> dict[str, Any]:
    """生成不可变报告快照；同周期重复请求返回原快照，避免重复邮件。"""
    if kind not in {"daily", "weekly"}:
        raise ApiProblem("invalid_report_kind", "报告类型必须是日报或周报。", 422)
    preference = _preference(user_id)
    zone = ZoneInfo(preference.timezone)
    today = datetime.now(UTC).astimezone(zone).date()
    if kind == "daily":
        start = period_start or today
        end = start
    else:
        start, end = _week_bounds(period_start or today)
    include = {**DEFAULT_INCLUDE, **(preference.include_json or {})}
    dedupe = f"report:{kind}:{start.isoformat()}:{end.isoformat()}:{preference.timezone}"
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        existing = db.scalar(select(ActivityReport).where(ActivityReport.dedupe_key == dedupe))
        if existing is not None:
            report = existing
        else:
            facts = _collect_facts(
                user_id,
                kind=kind,
                period_start=start,
                period_end=end,
                timezone_name=preference.timezone,
                include=include,
            )
            subject, body = _body(kind, start, end, facts)
            report = ActivityReport(
                id=str(uuid4()),
                user_id=user_id,
                kind=kind,
                period_start=start.isoformat(),
                period_end=end.isoformat(),
                timezone=preference.timezone,
                status="generated",
                subject=subject,
                body_text=body,
                sections_json=_serializable_facts(facts),
                metadata_json={
                    "agent_summary": facts["agent_summary"],
                    "suggestions": facts["suggestions"],
                },
                dedupe_key=dedupe,
                generated_at_epoch=now,
                version=1,
            )
            db.add(report)
            db.flush()
            operation_service.record_operation(
                db,
                user_id=user_id,
                actor_type="system",
                actor_id=None,
                action=f"report.{kind}.generate",
                object_type="activity_report",
                object_id=report.id,
                summary=f"生成{('日' if kind == 'daily' else '周')}报：{start.isoformat()}",
                after={"subject": subject, "period_start": start.isoformat(), "period_end": end.isoformat()},
                created_at_epoch=now,
            )
    if send:
        return queue_report_email(user_id, report.id)
    with DbSession(_engine()) as db:
        return report_public(db.get(ActivityReport, report.id))


def queue_report_email(user_id: str, report_id: str) -> dict[str, Any]:
    """向已验证主地址投递报告；发送失败不回滚已生成报告。"""
    with DbSession(_engine()) as db:
        report = db.scalar(
            select(ActivityReport).where(ActivityReport.id == report_id, ActivityReport.user_id == user_id)
        )
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        address = (
            db.scalar(
                select(VerifiedAddress).where(
                    VerifiedAddress.id == preference.recipient_address_id,
                    VerifiedAddress.user_id == user_id,
                )
            )
            if preference and preference.recipient_address_id
            else None
        )
        if report is None:
            raise ApiProblem("not_found", "报告不存在。", 404)
        if address is None:
            raise ApiProblem("verified_address_required", "请先选择已验证收件地址。", 422)
    # 手动日报使用独立 purpose，避免被“每日自动日报关闭”规则误取消。
    purpose = "weekly_report" if report.kind == "weekly" else "daily_report"
    item = _create_outbox(
        user_id=user_id,
        purpose=purpose,
        template_name=f"{report.kind}_report_v1",
        address=address,
        subject=report.subject,
        body_text=report.body_text,
        metadata={
            "report_id": report.id,
            "kind": report.kind,
            "period_start": report.period_start,
            "period_end": report.period_end,
            "timezone": report.timezone,
        },
        dedupe_key=f"report-email:{report.id}:{address.id}:{address.verified_at_epoch}",
        priority=35,
        now=_now(),
    )
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        report = db.get(ActivityReport, report.id)
        report.outbox_id = item.id
        if report.status == "generated":
            report.status = "queued"
            report.version += 1
    return {"report": report_public(report), "outbox": {"id": item.id, "status": item.status}}


def _serializable_facts(value: Any) -> Any:
    """报告快照只能落 JSON；ORM 任务转换为公开字段，避免把内部状态写入数据库。"""
    if isinstance(value, Task):
        return business_service.task_public(value)
    if isinstance(value, dict):
        return {str(key): _serializable_facts(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable_facts(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def report_public(report: ActivityReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "kind": report.kind,
        "period_start": report.period_start,
        "period_end": report.period_end,
        "timezone": report.timezone,
        "status": report.status,
        "subject": report.subject,
        "body_text": report.body_text,
        "sections": report.sections_json,
        "metadata": report.metadata_json,
        "links": _report_links(report.sections_json),
        "outbox_id": report.outbox_id,
        "generated_at_epoch": report.generated_at_epoch,
        "sent_at_epoch": report.sent_at_epoch,
        "version": report.version,
    }


def report_summary_public(report: ActivityReport) -> dict[str, Any]:
    """列表只返回导航与状态字段，避免把多份完整报告塞进 Agent 上下文。"""
    result = report_public(report)
    result.pop("body_text", None)
    result.pop("sections", None)
    result["link_count"] = len(result.get("links", []))
    return result


def _report_links(sections: dict[str, Any]) -> list[dict[str, str]]:
    """从报告事实中提取可跳转对象，链接只使用当前账户数据库 ID。"""
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, entity_type, href_prefix in (
        ("notices", "notice", "/inbox?panel=notices"),
        ("tasks_due", "task", "/tasks/"),
        ("open_tasks", "task", "/tasks/"),
        ("overdue", "task", "/tasks/"),
        ("completed", "task", "/tasks/"),
        ("deadlines", "task", "/tasks/"),
        ("next_deadlines", "task", "/tasks/"),
    ):
        for item in sections.get(key, []) or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            identity = (entity_type, str(item["id"]))
            if identity in seen:
                continue
            seen.add(identity)
            links.append(
                {
                    "type": entity_type,
                    "id": str(item["id"]),
                    "title": str(item.get("title") or "未命名对象"),
                    "href": (
                        f"{href_prefix}{item['id']}"
                        if entity_type == "task"
                        else f"{href_prefix}&notice={item['id']}"
                    ),
                }
            )
    course_rows = (
        (sections.get("courses", []) or [])
        + (sections.get("tomorrow_courses", []) or [])
        + (sections.get("next_courses", []) or [])
    )
    for item in course_rows:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        identity = ("course_occurrence", str(item["id"]))
        if identity in seen:
            continue
        seen.add(identity)
        links.append(
            {
                "type": "course_occurrence",
                "id": str(item["id"]),
                "title": str(item.get("title") or "课程"),
                "href": (
                    f"/schedule?focus={item['id']}&date={item['local_date']}"
                    if item.get("local_date")
                    else f"/schedule?focus={item['id']}"
                ),
            }
        )
    for item in sections.get("course_changes", []) or []:
        if not isinstance(item, dict) or not item.get("occurrence_id"):
            continue
        identity = ("course_occurrence", str(item["occurrence_id"]))
        if identity in seen:
            continue
        seen.add(identity)
        links.append(
            {
                "type": "course_occurrence",
                "id": str(item["occurrence_id"]),
                "title": str(item.get("title") or "课程调整"),
                "href": (
                    f"/schedule?focus={item['occurrence_id']}&date={item['local_date']}"
                    if item.get("local_date")
                    else f"/schedule?focus={item['occurrence_id']}"
                ),
            }
        )
    return links


def list_reports(user_id: str, *, limit: int = 30, summary: bool = False) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        rows = db.scalars(
            select(ActivityReport)
            .where(ActivityReport.user_id == user_id)
            .order_by(ActivityReport.generated_at_epoch.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
    formatter = report_summary_public if summary else report_public
    return {"items": [formatter(item) for item in rows]}


def get_report(user_id: str, report_id: str) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        report = db.scalar(
            select(ActivityReport).where(ActivityReport.id == report_id, ActivityReport.user_id == user_id)
        )
    if report is None:
        raise ApiProblem("not_found", "报告不存在。", 404)
    return report_public(report)


def dispatch_due_weekly_reports(engine, *, now_epoch: int | None = None) -> int:
    """按用户本地星期与时间排队生成周报，更新下一次时间防止重复。"""
    from .mail_service import _next_weekly_send_epoch

    now = now_epoch or _now()
    with DbSession(engine) as db:
        due = db.scalars(
            select(DigestPreference)
            .where(
                DigestPreference.weekly_enabled.is_(True),
                DigestPreference.next_weekly_send_epoch.is_not(None),
                DigestPreference.next_weekly_send_epoch <= now,
            )
            .limit(200)
        ).all()
        entries = [
            (item.user_id, item.version, item.timezone, item.weekly_send_day, item.weekly_send_time, item.next_weekly_send_epoch)
            for item in due
        ]
    count = 0
    for user_id, version, timezone_name, weekday, send_time, due_epoch in entries:
        local_day = datetime.fromtimestamp(now, UTC).astimezone(ZoneInfo(timezone_name)).date()
        enqueue_job(
            engine,
            user_id=user_id,
            kind="report.generate",
            payload={"kind": "weekly", "period_start": local_day.isoformat(), "send": True},
            idempotency_key=f"weekly-report:{user_id}:{local_day.isoformat()}",
            max_attempts=3,
            priority=45,
        )
        next_epoch = _next_weekly_send_epoch(now, timezone_name, weekday, send_time)
        with DbSession(engine) as db, db.begin():
            preference = db.scalar(
                select(DigestPreference).where(
                    DigestPreference.user_id == user_id,
                    DigestPreference.version == version,
                )
            )
            if preference is not None:
                preference.next_weekly_send_epoch = next_epoch
                preference.updated_at_epoch = now
        count += 1
    return count


def handle_report_job(claim: ClaimedJob) -> dict[str, Any]:
    payload = claim.payload
    kind = str(payload.get("kind", "weekly"))
    period_start = date.fromisoformat(payload["period_start"]) if payload.get("period_start") else None
    report = generate_report(
        claim.user_id,
        kind=kind,
        period_start=period_start,
        send=bool(payload.get("send", True)),
    )
    return {"status": "queued" if payload.get("send") else "generated", "report_id": report["id"]}
