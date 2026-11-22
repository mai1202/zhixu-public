"""校园事务面板指标；所有数字直接从当前账户的真实记录聚合。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..models import (
    ActivityReport,
    AgentInsight,
    AgentPreference,
    ChangeSet,
    CourseException,
    DigestPreference,
    Job,
    MailOutbox,
    Notice,
    OperationLog,
    ProviderProfile,
    Reminder,
    SourceDocument,
    Task,
)
from . import business_service, operation_service

UTC = timezone.utc


def _engine():
    return current_app.extensions["notification_db_engine"]


def dashboard(user_id: str) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        agent_preference = db.get(AgentPreference, user_id)
        planning_profile = db.get(ProviderProfile, agent_preference.profile_id) if agent_preference and agent_preference.profile_id else None
        model_ready = bool(planning_profile and planning_profile.user_id == user_id and planning_profile.enabled
                           and planning_profile.purpose == 'planning' and planning_profile.execution_target == 'server')
        zone = ZoneInfo(preference.timezone if preference else "Asia/Shanghai")
        today = datetime.now(UTC).astimezone(zone).date()
        notices_total = int(
            db.scalar(
                select(func.count()).select_from(SourceDocument).where(SourceDocument.user_id == user_id)
            ) or 0
        )
        active_notices = int(
            db.scalar(
                select(func.count()).select_from(Notice).where(
                    Notice.user_id == user_id,
                    Notice.status == "active",
                )
            ) or 0
        )
        tasks = db.scalars(select(Task).where(Task.user_id == user_id, Task.archived_at_epoch.is_(None))).all()
        open_tasks = [item for item in tasks if item.status == "open"]
        overdue = 0
        due_7d = 0
        for item in open_tasks:
            raw_due = (item.due_json or {}).get("local_date")
            if not raw_due:
                continue
            try:
                due_date = date.fromisoformat(str(raw_due))
            except ValueError:
                continue
            overdue += int(due_date < today)
            due_7d += int(today <= due_date <= today + timedelta(days=7))
        course_changes = int(
            db.scalar(
                select(func.count()).select_from(CourseException).where(
                    CourseException.user_id == user_id,
                    CourseException.status == "active",
                )
            ) or 0
        )
        # 手动或 Agent 维护整门课程同样属于课程变更，不能只统计单次例外。
        course_changes += int(
            db.scalar(
                select(func.count()).select_from(OperationLog).where(
                    OperationLog.user_id == user_id,
                    OperationLog.action.in_(
                        ("course.create", "course.update", "course.archive", "course.restore")
                    ),
                )
            ) or 0
        )
        reminders_sent = int(
            db.scalar(
                select(func.count()).select_from(Reminder).where(
                    Reminder.user_id == user_id,
                    Reminder.status == "sent",
                )
            ) or 0
        )
        reminders_pending = int(
            db.scalar(
                select(func.count()).select_from(Reminder).where(
                    Reminder.user_id == user_id,
                    Reminder.status.in_(("scheduled", "queued")),
                )
            ) or 0
        )
        reports = int(
            db.scalar(select(func.count()).select_from(ActivityReport).where(ActivityReport.user_id == user_id)) or 0
        )
        mail_accepted = int(
            db.scalar(
                select(func.count()).select_from(MailOutbox).where(
                    MailOutbox.user_id == user_id,
                    MailOutbox.status == "accepted",
                )
            ) or 0
        )
        job_failures = int(
            db.scalar(
                select(func.count()).select_from(Job).where(
                    Job.user_id == user_id,
                    Job.status == "failed",
                )
            ) or 0
        )
        finished_jobs = db.scalars(
            select(Job).where(
                Job.user_id == user_id,
                Job.status == "succeeded",
                Job.finished_at_epoch.is_not(None),
            )
            # 稳定选取最近完成的样本，避免数据库任意返回最早的 500 个任务。
            .order_by(Job.finished_at_epoch.desc(), Job.id.desc())
            .limit(500)
        ).all()
        durations = [
            max(0, int(item.finished_at_epoch or 0) - int(item.created_at_epoch))
            for item in finished_jobs
        ]
        operations = db.scalars(
            select(OperationLog)
            .where(OperationLog.user_id == user_id)
            .order_by(OperationLog.created_at_epoch.desc())
            .limit(12)
        ).all()
        agent_ops = int(
            db.scalar(
                select(func.count()).select_from(OperationLog).where(
                    OperationLog.user_id == user_id,
                    OperationLog.actor_type == "agent",
                )
            ) or 0
        )
        user_ops = int(
            db.scalar(
                select(func.count()).select_from(OperationLog).where(
                    OperationLog.user_id == user_id,
                    OperationLog.actor_type == "user",
                )
            ) or 0
        )
        dedupe_count = int(
            db.scalar(
                select(func.count()).select_from(OperationLog).where(
                    OperationLog.user_id == user_id,
                    OperationLog.action == "source.deduplicate",
                )
            ) or 0
        )
        tasks_created = int(
            db.scalar(
                select(func.count()).select_from(OperationLog).where(
                    OperationLog.user_id == user_id,
                    OperationLog.action == "task.create",
                )
            ) or 0
        )
        task_updates = int(
            db.scalar(
                select(func.count()).select_from(OperationLog).where(
                    OperationLog.user_id == user_id,
                    OperationLog.action == "task.update",
                )
            ) or 0
        )
        running_jobs = int(
            db.scalar(
                select(func.count()).select_from(Job).where(
                    Job.user_id == user_id,
                    Job.status.in_(("leased", "running", "retry_wait")),
                )
            ) or 0
        )
        active_insights = int(
            db.scalar(
                select(func.count()).select_from(AgentInsight).where(
                    AgentInsight.user_id == user_id,
                    AgentInsight.status == "active",
                )
            ) or 0
        )
        last_agent_op = db.scalar(
            select(OperationLog)
            .where(OperationLog.user_id == user_id, OperationLog.actor_type == "agent")
            .order_by(OperationLog.created_at_epoch.desc())
            .limit(1)
        )
    total_decisions = agent_ops + user_ops
    return {
        "timezone": str(zone),
        "local_date": today.isoformat(),
        "metrics": {
            "notices_processed": notices_total,
            "notices_active": active_notices,
            "tasks_total": len(tasks),
            "tasks_created": tasks_created,
            "task_updates": task_updates,
            "tasks_open": len(open_tasks),
            "tasks_overdue": overdue,
            "tasks_due_7d": due_7d,
            "course_changes": course_changes,
            "deduplicated": dedupe_count,
            "reminders_sent": reminders_sent,
            "reminders_pending": reminders_pending,
            "reports_generated": reports,
            "mail_accepted": mail_accepted,
            "job_failures": job_failures,
            "average_processing_seconds": round(sum(durations) / len(durations), 1) if durations else None,
            "processing_sample_count": len(durations),
            "agent_actions": agent_ops,
            "manual_interventions": user_ops,
            "automation_rate": round(agent_ops / total_decisions * 100, 1) if total_decisions else 0.0,
        },
        "metric_definitions": {
            "automation_rate": "Agent 操作数 /（Agent 操作数 + 用户操作数），按操作记录计数；不代表任务成功率或节省时间。",
            "average_processing_seconds": "最近最多 500 个成功任务从入队到完成的平均秒数，包含等待与重试；不代表模型推理耗时。",
            "notices_processed": "已接收的来源材料数量，可能包括附件、手工输入等；不代表成功提取的通知数量。",
            "course_changes": "当前有效的单次课程例外数量，加上课程系列维护的历史操作次数。",
        },
        "agent_status": {
            "automatic_notifications": bool(agent_preference and agent_preference.automatic_notifications),
            "model_ready": model_ready,
            "running_jobs": running_jobs,
            "active_insights": active_insights,
            "last_action": operation_service.operation_public(last_agent_op) if last_agent_op else None,
        },
        "recent_operations": [operation_service.operation_public(item) for item in operations],
        "mail": {
            "daily_enabled": bool(preference and preference.enabled),
            "weekly_enabled": bool(preference and preference.weekly_enabled),
            "reminder_enabled": bool(preference and preference.reminder_enabled),
            "recipient_configured": bool(preference and preference.recipient_address_id),
        },
        "generated_at_epoch": operation_service.now_epoch(),
    }
