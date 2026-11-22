"""截止提醒的幂等计划、安静时段和合并发送。"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from ..models import CourseOccurrence, CourseSeries, DigestPreference, Reminder, Task, VerifiedAddress

UTC = timezone.utc


def _engine():
    return current_app.extensions["notification_db_engine"]


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _in_quiet_hours(now_epoch: int, timezone_name: str, settings: dict[str, Any]) -> bool:
    """支持跨午夜安静时段；配置无效时宁可不抑制提醒。"""
    if not settings.get("enabled"):
        return False
    try:
        start = time.fromisoformat(str(settings.get("start", "22:00")))
        end = time.fromisoformat(str(settings.get("end", "07:00")))
        local_clock = datetime.fromtimestamp(now_epoch, UTC).astimezone(ZoneInfo(timezone_name)).time()
    except (ValueError, ZoneInfoNotFoundError):
        return False
    if start <= end:
        return start <= local_clock < end
    return local_clock >= start or local_clock < end


def sync_task_reminders(
    user_id: str,
    *,
    task_id: str | None = None,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """为开放任务补齐提醒并清理已失效计划，重复调用不会重复创建。"""
    now = now_epoch or _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        offsets = sorted({int(item) for item in (preference.reminder_offsets_json or [1440, 120]) if int(item) >= 0}, reverse=True)
        enabled = bool(preference and preference.reminder_enabled)
        task_query = select(Task).where(
            Task.user_id == user_id,
            Task.archived_at_epoch.is_(None),
        )
        if task_id:
            task_query = task_query.where(Task.id == task_id)
        tasks = db.scalars(task_query).all()
        expected_keys: set[str] = set()
        created = 0
        if enabled:
            for task in tasks:
                if task.status != "open" or not task.reminder_enabled or task.due_at_epoch is None:
                    continue
                for offset in offsets:
                    trigger = task.due_at_epoch - offset * 60
                    # 已经错过的提前提醒不补发，避免上线时集中轰炸历史任务。
                    if trigger <= now:
                        continue
                    dedupe = f"task:{task.id}:due:{task.due_at_epoch}:offset:{offset}"
                    expected_keys.add(dedupe)
                    existing = db.scalar(select(Reminder).where(Reminder.dedupe_key == dedupe))
                    if existing is None:
                        db.add(
                            Reminder(
                                id=str(uuid4()),
                                user_id=user_id,
                                task_id=task.id,
                                notice_id=task.notice_id,
                                course_occurrence_id=task.course_occurrence_id,
                                kind="deadline",
                                scheduled_at_epoch=trigger,
                                offset_minutes=offset,
                                status="scheduled",
                                dedupe_key=dedupe,
                                metadata_json={
                                    "title": task.title,
                                    "due": task.due_json,
                                    "priority": task.priority,
                                },
                                version=1,
                                created_at_epoch=now,
                                updated_at_epoch=now,
                            )
                        )
                        created += 1
                    elif existing.status == "cancelled":
                        # 用户重新开启提醒且任务/期限未变时，复用原唯一记录而不是被历史取消挡住。
                        existing.status = "scheduled"
                        existing.outbox_id = None
                        existing.sent_at_epoch = None
                        existing.error_code = None
                        existing.metadata_json = {
                            "title": task.title,
                            "due": task.due_json,
                            "priority": task.priority,
                        }
                        existing.version += 1
                        existing.updated_at_epoch = now
        # 任务完成、关闭提醒或截止时间变化后，旧计划必须退出扫描。
        stale = db.scalars(
            select(Reminder).where(
                Reminder.user_id == user_id,
                Reminder.status == "scheduled",
                Reminder.kind == "deadline",
                Reminder.dedupe_key.not_in(expected_keys or {"__none__"}),
            )
        ).all()
        for item in stale:
            item.status = "cancelled"
            item.error_code = "task_changed"
            item.version += 1
            item.updated_at_epoch = now
        return {"created": created, "cancelled": len(stale), "enabled": enabled}


def sync_course_reminders(
    user_id: str,
    *,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """为未来课程开始时间生成独立提醒，和任务截止提醒使用不同开关与去重键。"""
    now = now_epoch or _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        offsets = sorted(
            {
                int(item)
                for item in (preference.reminder_offsets_json or [1440, 120])
                if int(item) >= 0
            },
            reverse=True,
        ) if preference else []
        flags = {
            key: bool(value)
            for key, value in (preference.notification_flags_json or {}).items()
        } if preference else {}
        enabled = bool(
            preference
            and preference.reminder_enabled
            and preference.recipient_address_id
            and flags.get("course_reminder", False)
        )
        today = datetime.fromtimestamp(now, UTC).date()
        max_offset_days = (max(offsets) // 1440 + 2) if offsets else 2
        rows = db.execute(
            select(CourseOccurrence, CourseSeries)
            .join(CourseSeries, CourseSeries.id == CourseOccurrence.course_series_id)
            .where(
                CourseOccurrence.user_id == user_id,
                CourseOccurrence.local_date >= today.isoformat(),
                CourseOccurrence.local_date <= (today + timedelta(days=max_offset_days)).isoformat(),
                CourseOccurrence.status.in_(("scheduled", "rescheduled")),
                CourseSeries.status == "active",
            )
            .order_by(CourseOccurrence.local_date, CourseOccurrence.display_start)
            .limit(500)
        ).all()
        expected_keys: set[str] = set()
        created = 0
        if enabled:
            for occurrence, series in rows:
                if not occurrence.busy_segments_json:
                    continue
                start_at = datetime.fromisoformat(occurrence.busy_segments_json[0]["start_at"])
                start_epoch = int(start_at.timestamp())
                if start_epoch <= now:
                    continue
                for offset in offsets:
                    trigger = start_epoch - offset * 60
                    if trigger <= now:
                        continue
                    dedupe = f"course:{occurrence.id}:start:{start_epoch}:offset:{offset}"
                    expected_keys.add(dedupe)
                    existing = db.scalar(select(Reminder).where(Reminder.dedupe_key == dedupe))
                    if existing is not None:
                        if existing.status == "cancelled":
                            existing.status = "scheduled"
                            existing.outbox_id = None
                            existing.sent_at_epoch = None
                            existing.error_code = None
                            existing.metadata_json = {
                                "title": series.display_name,
                                "kind": "course_start",
                                "room": occurrence.room_override or series.room,
                                "due": {
                                    "raw": start_at.isoformat(),
                                    "local_date": occurrence.local_date,
                                    "local_time": occurrence.display_start,
                                },
                                "priority": "normal",
                            }
                            existing.version += 1
                            existing.updated_at_epoch = now
                        continue
                    db.add(
                        Reminder(
                            id=str(uuid4()),
                            user_id=user_id,
                            task_id=None,
                            notice_id=None,
                            course_occurrence_id=occurrence.id,
                            kind="course_start",
                            scheduled_at_epoch=trigger,
                            offset_minutes=offset,
                            status="scheduled",
                            dedupe_key=dedupe,
                            metadata_json={
                                "title": series.display_name,
                                "kind": "course_start",
                                "room": occurrence.room_override or series.room,
                                "due": {
                                    "raw": start_at.isoformat(),
                                    "local_date": occurrence.local_date,
                                    "local_time": occurrence.display_start,
                                },
                                "priority": "normal",
                            },
                            version=1,
                            created_at_epoch=now,
                            updated_at_epoch=now,
                        )
                    )
                    created += 1
        stale = db.scalars(
            select(Reminder).where(
                Reminder.user_id == user_id,
                Reminder.status == "scheduled",
                Reminder.kind == "course_start",
                Reminder.dedupe_key.not_in(expected_keys or {"__none__"}),
            )
        ).all()
        for item in stale:
            item.status = "cancelled"
            item.error_code = "course_changed"
            item.version += 1
            item.updated_at_epoch = now
        return {"created": created, "cancelled": len(stale), "enabled": enabled}


def sync_all_reminders(engine, *, now_epoch: int | None = None) -> int:
    """worker 周期性补齐所有启用提醒账户，单用户失败不阻塞其他人。"""
    now = now_epoch or _now()
    with DbSession(engine) as db:
        user_ids = db.scalars(
            select(DigestPreference.user_id).where(
                DigestPreference.reminder_enabled.is_(True),
                DigestPreference.recipient_address_id.is_not(None),
            ).limit(500)
        ).all()
    total = 0
    for user_id in user_ids:
        try:
            total += int(sync_task_reminders(user_id, now_epoch=now)["created"])
            total += int(sync_course_reminders(user_id, now_epoch=now)["created"])
        except Exception:
            # 调度扫描失败由 worker 日志记录类别，不输出任务标题或用户邮箱。
            continue
    return total


def _reminder_line(item: Reminder) -> str:
    metadata = item.metadata_json or {}
    if item.kind == "course_start":
        due = metadata.get("due") or {}
        return (
            f"- 课程开始：{metadata.get('title', '未命名课程')}"
            f"｜{due.get('local_date', '日期未知')} {(due.get('local_time') or '')[:5]}"
            f"｜{metadata.get('room') or '地点未提供'}"
        )
    due = metadata.get("due") or {}
    due_text = due.get("raw") or due.get("local_date") or "未提供具体时间"
    priority = {"urgent": "紧急", "high": "重要", "normal": "普通", "low": "低"}.get(
        str(metadata.get("priority", "normal")), "普通"
    )
    return f"- {metadata.get('title', '未命名任务')}｜{priority}｜{due_text}"


def dispatch_due_reminders(engine, *, now_epoch: int | None = None) -> int:
    """每个用户一次合并发送到期提醒；邮件 outbox 唯一键保证 worker 重试幂等。"""
    from . import mail_service

    now = now_epoch or _now()
    with DbSession(engine) as db:
        due_rows = db.scalars(
            select(Reminder)
            .where(Reminder.status == "scheduled", Reminder.scheduled_at_epoch <= now)
            .order_by(Reminder.scheduled_at_epoch)
            .limit(500)
        ).all()
        grouped: dict[str, list[Reminder]] = {}
        for item in due_rows:
            grouped.setdefault(item.user_id, []).append(item)
    dispatched = 0
    for user_id, reminders in grouped.items():
        with DbSession(engine) as db:
            task_ids = [item.task_id for item in reminders if item.task_id]
            active_tasks = db.scalars(
                select(Task.id).where(
                    Task.user_id == user_id,
                    Task.id.in_(task_ids or ["__none__"]),
                    Task.status == "open",
                    Task.reminder_enabled.is_(True),
                    Task.archived_at_epoch.is_(None),
                )
            ).all()
            inactive = [
                item for item in reminders
                if item.task_id and item.task_id not in set(active_tasks)
            ]
            if inactive:
                db.execute(
                    update(Reminder)
                    .where(Reminder.id.in_([item.id for item in inactive]))
                    .values(status="cancelled", error_code="task_closed", updated_at_epoch=now)
                )
                db.commit()
                reminders = [item for item in reminders if item not in inactive]
            if not reminders:
                continue
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
            if preference is None or not preference.reminder_enabled or address is None:
                continue
            flags = mail_service.DEFAULT_NOTIFICATION_FLAGS | (preference.notification_flags_json or {})
            disabled = [
                item for item in reminders
                if (
                    item.kind == "course_start" and not flags.get("course_reminder", False)
                ) or (
                    item.kind != "course_start" and not flags.get("task_reminder", False)
                )
            ]
            if disabled:
                db.execute(
                    update(Reminder)
                    .where(Reminder.id.in_([item.id for item in disabled]))
                    .values(
                        status="cancelled",
                        error_code="notification_disabled",
                        updated_at_epoch=now,
                    )
                )
                db.commit()
                reminders = [item for item in reminders if item not in disabled]
            if not reminders:
                continue
            if _in_quiet_hours(now, preference.timezone, preference.quiet_hours_json or {}):
                task_ids = [item.task_id for item in reminders if item.task_id]
                urgent = db.scalars(
                    select(Task.id).where(
                        Task.user_id == user_id,
                        Task.id.in_(task_ids or ["__none__"]),
                        Task.priority == "urgent",
                    )
                ).all()
                if not urgent:
                    # 保留 scheduled 状态，安静时段结束后由下一轮扫描发送。
                    continue
        dedupe = "reminder:" + user_id + ":" + ",".join(sorted(item.id for item in reminders))
        body = [
            "通知管理 · 主动提醒",
            "",
            "以下校园事项即将开始或临近截止：",
            *(_reminder_line(item) for item in reminders),
            "",
            "提醒只依据已登记课程与任务；如需调整时间或关闭提醒，可在通知管理设置中修改。",
        ]
        item = mail_service._create_outbox(
            user_id=user_id,
            purpose="reminder",
            template_name="proactive_reminder_v2",
            address=address,
            subject=f"通知管理 · {len(reminders)} 项校园提醒",
            body_text="\n".join(body),
            metadata={
                "reminder_ids": [entry.id for entry in reminders],
                "task_ids": [entry.task_id for entry in reminders if entry.task_id],
                "course_occurrence_ids": [
                    entry.course_occurrence_id
                    for entry in reminders
                    if entry.course_occurrence_id
                ],
            },
            dedupe_key=dedupe,
            priority=20,
            now=now,
        )
        with DbSession(engine) as db, db.begin():
            db.execute(
                update(Reminder)
                .where(Reminder.id.in_([entry.id for entry in reminders]), Reminder.user_id == user_id)
                .values(status="queued", outbox_id=item.id, updated_at_epoch=now)
            )
        dispatched += len(reminders)
    return dispatched


def list_reminders(user_id: str, *, limit: int = 50) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        rows = db.scalars(
            select(Reminder)
            .where(Reminder.user_id == user_id)
            .order_by(Reminder.scheduled_at_epoch, Reminder.id)
            .limit(max(1, min(limit, 100)))
        ).all()
    return {
        "items": [
            {
                "id": item.id,
                "task_id": item.task_id,
                "notice_id": item.notice_id,
                "kind": item.kind,
                "scheduled_at_epoch": item.scheduled_at_epoch,
                "offset_minutes": item.offset_minutes,
                "status": item.status,
                "metadata": item.metadata_json,
                "outbox_id": item.outbox_id,
                "sent_at_epoch": item.sent_at_epoch,
                "error_code": item.error_code,
            }
            for item in rows
        ]
    }


def create_manual_reminder(user_id: str, *, task_id: str, scheduled_for, note: str | None) -> dict[str, Any]:
    """手动提醒同样走唯一键和合并通道，不能绕过用户设置无限发信。"""
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        task = db.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id))
        if task is None:
            from ..errors import ApiProblem

            raise ApiProblem("not_found", "任务不存在。", 404)
        local_date = scheduled_for.isoformat()
        dedupe = f"manual:{task.id}:{local_date}:{(note or '').strip()[:80]}"
        existing = db.scalar(select(Reminder).where(Reminder.dedupe_key == dedupe))
        if existing is not None:
            return {"reminder_id": existing.id, "status": existing.status}
        scheduled = int(
            datetime.combine(scheduled_for, time(9, 0), tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        )
        item = Reminder(
            id=str(uuid4()),
            user_id=user_id,
            task_id=task.id,
            notice_id=task.notice_id,
            course_occurrence_id=task.course_occurrence_id,
            kind="manual",
            scheduled_at_epoch=max(now, scheduled),
            offset_minutes=None,
            status="scheduled",
            dedupe_key=dedupe,
            metadata_json={"title": task.title, "due": task.due_json, "note": note},
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
        )
        db.add(item)
    return {"reminder_id": item.id, "status": item.status}
