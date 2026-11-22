"""今日行动与来源健康只根据数据库观测，未知状态不冒充成功。"""

from datetime import datetime, timedelta, timezone
import time

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ..errors import ApiProblem
from ..text_sanitize import clean_title

from ..models import (AgentScheduledTask, ChangeSet, Device, DeviceAttachment, Job,
    MailIngressCursor, MailIngressReceipt, OperationLog, ScheduleEntry, StudentJourney, Task, UserPlanningState)
from . import planning_service


def source_health(user_id):
    now = int(time.time())
    with Session(current_app.extensions["notification_db_engine"]) as db:
        devices = []
        for device in db.scalars(select(Device).where(Device.user_id == user_id, Device.status != "revoked")):
            interval = int((device.settings_json or {}).get("sync_interval_seconds", 300))
            # 空采集也可能成功，不能只看是否上传了新消息；心跳本身不算成功采集。
            last = max(filter(None, [device.last_upload_epoch, device.last_collect_epoch]), default=None)
            devices.append({"id": device.id, "name": device.name, "last_success": last,
                "last_collect": device.last_collect_epoch, "last_heartbeat": device.last_heartbeat_epoch,
                "status": "unknown" if last is None else "delayed" if now - last > max(interval * 3, 900) else "current",
                "sync_request": (device.settings_json or {}).get("sync_result", "unknown")})
        pending = db.scalar(select(func.count()).select_from(Job).where(Job.user_id == user_id,
            Job.kind == "agent.notification", Job.status.in_(("queued", "running", "retry_wait", "leased")))) or 0
        failed = db.scalar(select(func.count()).select_from(Job).where(Job.user_id == user_id,
            Job.kind == "agent.notification", Job.status == "failed")) or 0
        attachment_states = dict(db.execute(select(DeviceAttachment.status, func.count()).where(
            DeviceAttachment.user_id == user_id).group_by(DeviceAttachment.status)).all())
        mail = db.scalar(select(MailIngressReceipt).where(MailIngressReceipt.user_id == user_id,
            MailIngressReceipt.status == "completed").order_by(MailIngressReceipt.updated_at_epoch.desc()).limit(1))
        cursor = db.scalar(select(MailIngressCursor).order_by(MailIngressCursor.last_poll_epoch.desc()).limit(1))
        poll_status = "failed" if cursor and cursor.error_code else (
            "unknown" if cursor is None or cursor.last_success_epoch is None else
            "delayed" if now - cursor.last_success_epoch > 180 else "current")
        watches = []
        for watch in db.scalars(select(AgentScheduledTask).where(AgentScheduledTask.user_id == user_id)):
            data = watch.__dict__
            if data.get("watch_url"):
                last = data.get("last_success_epoch")
                delayed = last is not None and now - last > max(3 * (data.get("interval_seconds") or 3600), 900)
                status = "paused" if data.get("status") != "active" else "failed" if data.get("last_error_code") else "unknown" if last is None else "delayed" if delayed else "current"
                watches.append({"id": watch.id, "status": status,
                    "last_success": data.get("last_success_epoch"), "scope": "仅此关注地址"})
        return {"devices": devices, "attachments": attachment_states,
            "notification_processing": {"pending": pending, "failed": failed},
            "mail": {"last_ingested": mail.updated_at_epoch if mail else None,
                "status": "has_ingestion_receipt" if mail else "unknown", "poll_status": poll_status,
                "last_success": cursor.last_success_epoch if cursor else None, "note": "入库与轮询成功分别报告；不公开运营邮箱或其他用户邮件。"},
            "watches": watches, "coverage_note": "只报告已授权来源的实际观测，不代表全部通知覆盖。"}


def today_actions(user_id):
    now = datetime.now(timezone.utc)
    actions = []
    with Session(current_app.extensions["notification_db_engine"]) as db:
        state = db.get(UserPlanningState, user_id)
        for entry in db.scalars(select(ScheduleEntry).where(ScheduleEntry.user_id == user_id,
            ScheduleEntry.status == "active")):
            if entry.start_at and entry.end_at and planning_service._dt(entry.end_at) > now and planning_service._dt(entry.start_at) <= now + timedelta(hours=2):
                actions.append({"kind": "schedule", "id": entry.id, "title": clean_title(entry.title, "个人安排"),
                    # 结构化时间供界面按当地时区显示，说明文字不直接暴露 ISO 时间串。
                    "reason": "即将开始或正在进行", "start_at": entry.start_at,
                    "href": f"/schedule?focus={entry.id}&date={entry.local_date}", "rank": 1})
        routes = db.scalars(select(StudentJourney).where(StudentJourney.user_id == user_id)
            .order_by(StudentJourney.updated_at_epoch.desc(), StudentJourney.id).limit(11)).all()
        for route in routes[:10]:
            task = db.get(Task, route.task_id)
            if not task or task.status != "open": continue
            done = {s["id"] for s in route.steps_json if s["status"] == "done"}
            latest_start = None
            if state and state.availability_json:
                from .journey_service import _plan
                from ..schemas.journeys import JourneyPlanInput
                try:
                    plan = _plan(db, route, JourneyPlanInput(expected_version=route.version, availability=state.availability_json))
                    latest_start = plan.get("latest_start")
                except (ApiProblem, ValueError):
                    # 独立路线估时失败不影响今日的真实日程和核对入口。
                    latest_start = None
            for step in route.steps_json:
                if step["status"] != "done" and step["assigned_user_id"] == user_id and not step.get("source_removed") and all(parent in done for parent in step["depends_on"]):
                    actions.append({"kind": "step", "id": step["id"], "title": clean_title(step["title"], "待办步骤"),
                        "reason": "前置已完成；耗时待补。" if step.get("estimate_minutes") is None else (
                            f"预计{step['estimate_minutes']}分钟；本路线最晚开始{latest_start}，其他未预约事务需统一试算。" if latest_start else f"预计{step['estimate_minutes']}分钟，截止与容量请统一试算。"),
                        "href": f"/matters/journeys?task={task.id}", "rank": 2,
                        "due": task.due_at_epoch, "slack_seconds": int((planning_service._dt(latest_start) - now).total_seconds()) if latest_start else None})
        changes = db.scalars(select(OperationLog).where(OperationLog.user_id == user_id,
            OperationLog.action.in_(("requirements.apply", "planning.apply")))
            .order_by(OperationLog.created_at_epoch.desc()).limit(10)).all()
        cards = [{"id": op.id, "summary": op.summary, "change_set_id": op.change_set_id,
            "before": op.before_json, "after": op.after_json, "status": op.status} for op in changes]
    actions.sort(key=lambda item: (item["rank"], item.get("slack_seconds") if item.get("slack_seconds") is not None else 2**62,
        item.get("due") or 2**62, item["id"]))
    return {"items": actions[:30], "changes": cards, "generated_at": now.isoformat(),
        "coverage": {"routes_checked": min(10, len(routes)), "routes_has_more": len(routes) > 10}}



def handle_reminders(claim):
    from .reminder_service import sync_task_reminders
    with Session(current_app.extensions["notification_db_engine"]) as db:
        ids = list(db.scalars(select(Task.id).where(Task.user_id == claim.user_id,
            Task.event_id == claim.payload["event_id"])))
    for identifier in ids: sync_task_reminders(claim.user_id, task_id=identifier)
    return {"status": "synchronized", "task_count": len(ids)}
