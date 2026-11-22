from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import time as clock
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..jobs.queue import ClaimedJob, enqueue_job
from ..mail.transports import (
    DisabledMailTransport,
    MailDeliveryError,
    MailNotConfigured,
)
from ..models import (
    ActivityReport,
    ChangeSet,
    DigestCursor,
    DigestPreference,
    Job,
    MailOutbox,
    Reminder,
    User,
    VerifiedAddress,
)
from ..schemas.mail import DigestPreferenceInput
from . import timetable_service

UTC = timezone.utc
DEFAULT_INCLUDE = {
    "courses": True,
    "arrangements": True,
    "deadlines": True,
    "changes": True,
    "tomorrow": True,
    "agent_summary": True,
    "suggestions": True,
}
DEFAULT_NOTIFICATION_FLAGS = {
    # 主动邮件默认关闭，用户首次保存邮件设置时必须显式勾选。
    "task_reminder": False,
    "course_reminder": False,
    "important_notice": False,
    "course_change": False,
    "conflict_warning": False,
}
DEFAULT_QUIET_HOURS = {"enabled": False, "start": "22:00", "end": "07:00"}


class MailRetryRequest(RuntimeError):
    """要求 worker 仅重试已确定未被邮件服务器接受的瞬时失败。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _engine():
    return current_app.extensions["notification_db_engine"]


def _transport():
    return current_app.extensions["notification_mail_transport"]


def _next_send_epoch(now_epoch: int, timezone_name: str, send_time: str) -> int:
    """计算下一个有效本地时刻，处理夏令时缺失和重复小时。"""
    zone = ZoneInfo(timezone_name)
    local_now = datetime.fromtimestamp(now_epoch, UTC).astimezone(zone)
    hour, minute = (int(value) for value in send_time.split(":"))
    day = local_now.date()
    for _ in range(370):
        naive = datetime.combine(day, time(hour, minute))
        candidates: set[int] = set()
        for fold in (0, 1):
            candidate = naive.replace(tzinfo=zone, fold=fold)
            utc_candidate = candidate.astimezone(UTC)
            actual_local = utc_candidate.astimezone(zone).replace(tzinfo=None)
            stamp = int(utc_candidate.timestamp())
            if actual_local.date() == day and actual_local >= naive and stamp > now_epoch:
                candidates.add(stamp)
        if candidates:
            return min(candidates)
        day += timedelta(days=1)
    raise ApiProblem("invalid_digest_schedule", "无法计算日报下次发送时间。", 422)


def _next_weekly_send_epoch(
    now_epoch: int,
    timezone_name: str,
    weekday: int,
    send_time: str,
) -> int:
    """计算下一个指定 ISO 星期（1=周一）的本地发送时刻。"""
    zone = ZoneInfo(timezone_name)
    local_now = datetime.fromtimestamp(now_epoch, UTC).astimezone(zone)
    hour, minute = (int(value) for value in send_time.split(":"))
    base = local_now.date()
    for offset in range(0, 367):
        day = base + timedelta(days=offset)
        if day.isoweekday() != weekday:
            continue
        naive = datetime.combine(day, time(hour, minute))
        first = naive.replace(tzinfo=zone, fold=0)
        if int(first.timestamp()) > now_epoch:
            return int(first.timestamp())
    raise ApiProblem("invalid_weekly_schedule", "无法计算周报下次发送时间。", 422)


def _preference_public(preference: DigestPreference, addresses: list[VerifiedAddress]) -> dict[str, Any]:
    selected = next((item for item in addresses if item.id == preference.recipient_address_id), None)
    return {
        "id": preference.id,
        "enabled": preference.enabled,
        "timezone": preference.timezone,
        "send_time": preference.send_time,
        "recipient_address_id": preference.recipient_address_id,
        "recipient": selected.email if selected else None,
        # 只返回仍支持的栏目，历史配置中的通知核对开关不再进入系统。
        "include": {key: (preference.include_json or {}).get(key, value) for key, value in DEFAULT_INCLUDE.items()},
        "weekly_enabled": preference.weekly_enabled,
        "weekly_send_day": preference.weekly_send_day,
        "weekly_send_time": preference.weekly_send_time,
        "next_weekly_send_epoch": preference.next_weekly_send_epoch,
        "reminder_enabled": preference.reminder_enabled,
        "reminder_offsets": preference.reminder_offsets_json or [1440, 120],
        "notification_flags": {**DEFAULT_NOTIFICATION_FLAGS, **(preference.notification_flags_json or {})},
        "quiet_hours": {**DEFAULT_QUIET_HOURS, **(preference.quiet_hours_json or {})},
        "merge_window_minutes": preference.merge_window_minutes,
        "version": preference.version,
        "next_send_epoch": preference.next_send_epoch,
        "addresses": [
            {
                "id": item.id,
                "email": item.email,
                "verified_at_epoch": item.verified_at_epoch,
                "is_primary": item.is_primary,
            }
            for item in addresses
        ],
    }


def get_preferences(user_id: str) -> dict[str, Any]:
    """读取或初始化日报设置，默认关闭且不预选收件地址。"""
    now = int(clock.time())
    try:
        with DbSession(_engine()) as db, db.begin():
            preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
            if preference is None:
                preference = DigestPreference(
                    id=str(uuid4()),
                    user_id=user_id,
                    enabled=False,
                    timezone="Asia/Shanghai",
                    send_time="07:30",
                    recipient_address_id=None,
                    include_json=deepcopy(DEFAULT_INCLUDE),
                    version=1,
                    next_send_epoch=None,
                    weekly_enabled=False,
                    weekly_send_day=7,
                    weekly_send_time="18:00",
                    next_weekly_send_epoch=None,
                    reminder_enabled=True,
                    reminder_offsets_json=[1440, 120],
                    notification_flags_json=deepcopy(DEFAULT_NOTIFICATION_FLAGS),
                    quiet_hours_json=deepcopy(DEFAULT_QUIET_HOURS),
                    merge_window_minutes=30,
                    created_at_epoch=now,
                    updated_at_epoch=now,
                )
                db.add(preference)
            if db.get(DigestCursor, user_id) is None:
                db.add(DigestCursor(user_id=user_id, last_change_seq=0, last_sent_local_date=None, updated_at_epoch=now))
    except IntegrityError:
        # 两个首次打开请求同时初始化时，唯一 user_id 会使其中一个获胜；随后读取获胜行。
        pass
    with DbSession(_engine()) as db:
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        addresses = db.scalars(
            select(VerifiedAddress)
            .where(VerifiedAddress.user_id == user_id)
            .order_by(VerifiedAddress.is_primary.desc(), VerifiedAddress.verified_at_epoch)
        ).all()
    if preference is None:
        raise ApiProblem("mail_settings_unavailable", "无法读取日报设置。", 503)
    return _preference_public(preference, addresses)


def update_preferences(user_id: str, payload: DigestPreferenceInput) -> dict[str, Any]:
    """保存用户明确选择的时间和已验证地址，并用版本号避免覆盖并发修改。"""
    now = int(clock.time())
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        preference = db.scalar(
            select(DigestPreference)
            .where(DigestPreference.user_id == user_id)
            .with_for_update()
        )
        if preference is None:
            raise ApiProblem("version_conflict", "请先重新加载日报设置。", 409)
        if preference.version != payload.expected_version:
            raise ApiProblem("version_conflict", "日报设置已变化，请刷新后重试。", 409)
        address = None
        if payload.recipient_address_id is not None:
            address = db.scalar(
                select(VerifiedAddress).where(
                    VerifiedAddress.id == str(payload.recipient_address_id),
                    VerifiedAddress.user_id == user_id,
                )
            )
            if address is None:
                raise ApiProblem("verified_address_required", "收件地址必须属于本人且已经验证。", 422)
        if payload.enabled and address is None:
            raise ApiProblem("verified_address_required", "启用日报前必须选择已验证收件地址。", 422)
        preference.enabled = payload.enabled
        preference.timezone = payload.timezone
        preference.send_time = payload.send_time
        preference.recipient_address_id = address.id if address else None
        preference.include_json = payload.include.model_dump()
        preference.next_send_epoch = (
            _next_send_epoch(now, payload.timezone, payload.send_time) if payload.enabled else None
        )
        if payload.weekly_enabled is not None:
            preference.weekly_enabled = payload.weekly_enabled
        if payload.weekly_send_day is not None:
            preference.weekly_send_day = payload.weekly_send_day
        if payload.weekly_send_time is not None:
            preference.weekly_send_time = payload.weekly_send_time
        preference.next_weekly_send_epoch = (
            _next_weekly_send_epoch(
                now,
                payload.timezone,
                preference.weekly_send_day,
                preference.weekly_send_time,
            )
            if preference.weekly_enabled
            else None
        )
        if payload.reminder_enabled is not None:
            preference.reminder_enabled = payload.reminder_enabled
        if payload.reminder_offsets is not None:
            preference.reminder_offsets_json = sorted(set(payload.reminder_offsets), reverse=True)
        if payload.notification_flags is not None:
            preference.notification_flags_json = {
                **DEFAULT_NOTIFICATION_FLAGS,
                **payload.notification_flags,
            }
        if payload.quiet_hours is not None:
            preference.quiet_hours_json = payload.quiet_hours.model_dump()
        if payload.merge_window_minutes is not None:
            preference.merge_window_minutes = payload.merge_window_minutes
        preference.version += 1
        preference.updated_at_epoch = now
        if not payload.enabled or address is None:
            db.execute(
                update(MailOutbox)
                .where(
                    MailOutbox.user_id == user_id,
                    MailOutbox.purpose == "digest",
                    MailOutbox.status == "queued",
                )
                .values(status="cancelled", error_code="preference_changed", updated_at_epoch=now)
            )
        if not preference.weekly_enabled or address is None:
            db.execute(
                update(MailOutbox)
                .where(
                    MailOutbox.user_id == user_id,
                    MailOutbox.purpose == "weekly_report",
                    MailOutbox.status == "queued",
                )
                .values(status="cancelled", error_code="preference_changed", updated_at_epoch=now)
            )
        updated_version = preference.version
    result = get_preferences(user_id)
    if result["version"] != updated_version:
        raise ApiProblem("version_conflict", "日报设置刚刚又被修改，请重新加载。", 409)
    return result


def _sections(
    user_id: str,
    local_date: date,
    include: dict[str, bool],
) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    """按 owner 查询日报事实，邮件只呈现摘要，不复制通知原文。"""
    schedule_end = local_date + timedelta(days=6)
    schedule = timetable_service.list_schedule(user_id, local_date, schedule_end)
    courses = [item for item in schedule["items"] if item.get("item_type") == "course" and item.get("local_date") == local_date.isoformat()]
    arrangements = [item for item in schedule["items"] if item.get("item_type") == "personal" and item.get("local_date") == local_date.isoformat()]
    deadlines = schedule["deadlines"]
    with DbSession(_engine()) as db:
        user = db.scalar(select(User).where(User.id == user_id))
        cursor = db.get(DigestCursor, user_id)
        from_seq = cursor.last_change_seq if cursor else 0
        through_seq = user.change_sequence if user else from_seq
        changes = db.scalars(
            select(ChangeSet)
            .where(
                ChangeSet.user_id == user_id,
                ChangeSet.change_seq > from_seq,
                ChangeSet.change_seq <= through_seq,
            )
            .order_by(ChangeSet.change_seq)
            .limit(100)
        ).all()
    tomorrow = local_date + timedelta(days=1)
    tomorrow_courses = [
        item for item in schedule["items"]
        if item.get("item_type") == "course" and item.get("local_date") == tomorrow.isoformat()
    ]
    tomorrow_deadlines = [
        item for item in deadlines
        if item.get("local_date") == tomorrow.isoformat()
    ]
    applied_count = sum(1 for item in changes if item.confirmed_at_epoch is not None and item.undone_at_epoch is None)
    agent_changes = sum(
        1 for item in changes
        if item.operations_json and any(
            operation.get("object_type") in {"task", "course_occurrence", "course_exception"}
            for operation in item.operations_json
        )
    )
    sections = {
        "courses": courses if include.get("courses", True) else [],
        "arrangements": arrangements if include.get("arrangements", True) else [],
        "deadlines": deadlines if include.get("deadlines", True) else [],
        "changes": [
            {"change_seq": item.change_seq, "summary": item.summary}
            for item in changes
        ] if include.get("changes", True) else [],
        "tomorrow": (
            [{"kind": "course", **item} for item in tomorrow_courses]
            + [{"kind": "deadline", **item} for item in tomorrow_deadlines]
        ) if include.get("tomorrow", True) else [],
        "agent_summary": [
            {
                "processed": len(changes),
                "applied": applied_count,
                "cross_module": agent_changes,
            }
        ] if include.get("agent_summary", True) else [],
        "suggestions": (
            [f"明天有 {len(tomorrow_deadlines)} 个截止事项，建议提前完成准备。"] if tomorrow_deadlines else
            ["当前未发现逾期或高优先级预警，可继续保持现有安排。"]
        ) if include.get("suggestions", True) else [],
    }
    return sections, from_seq, through_seq


def _item_line(item: dict[str, Any], *, include_room: bool = False) -> str:
    """将课程或个人安排压缩为不含来源原文的明确信息。"""
    date_text = str(item.get("local_date", ""))
    start = str(item.get("start_at", ""))
    end = str(item.get("end_at", ""))
    if "T" in start:
        start = start[11:16]
    if "T" in end:
        end = end[11:16]
    period = f" {start}–{end}" if start and end else ""
    room = f"，地点：{item['room']}" if include_room and item.get("room") else ""
    return f"- {date_text}{period} · {item.get('title', '未命名安排')}{room}"


def _digest_body(local_date: date, sections: dict[str, list[dict[str, Any]]]) -> str:
    """生成确定性模板日报；任何推断都不混入事实栏目。"""
    chunks = [f"通知管理 · {local_date.isoformat()} 日报", ""]
    for key, title in (
        ("courses", "今日课程"),
        ("arrangements", "今日已确认安排"),
        ("deadlines", "临近截止（未来 7 天）"),
        ("changes", "上次日报后已处理变更"),
        ("tomorrow", "明日预警"),
        ("agent_summary", "Agent 自动处理摘要"),
        ("suggestions", "建议"),
    ):
        rows = sections.get(key, [])
        if not rows:
            continue
        chunks.extend([title, "- 未发现相关事项" if not rows else ""])
        if key in {"courses", "arrangements"}:
            chunks.extend(_item_line(row, include_room=key == "courses") for row in rows)
        elif key == "deadlines":
            chunks.extend(
                f"- {row.get('local_date')} 截止 · {row.get('title')}（精度：{row.get('precision') or '未知'}）"
                for row in rows
            )
        elif key == "changes":
            chunks.extend(
                f"- #{row.get('change_seq')} · {row.get('summary')}"
                for row in rows
            )
        elif key == "tomorrow":
            chunks.extend(
                f"- {'课程' if row.get('kind') == 'course' else '截止'} · "
                f"{row.get('start_at') or row.get('local_date')} · {row.get('title')}"
                for row in rows
            )
        elif key == "agent_summary":
            chunks.extend(
                f"- 处理 {row.get('processed', 0)} 条变更，实际应用 {row.get('applied', 0)} 条，"
                f"三端维护 {row.get('cross_module', 0)} 次。"
                for row in rows
            )
        else:
            chunks.extend(f"- {row}" for row in rows)
        chunks.append("")
    if len(chunks) == 2:
        chunks.extend(["今日暂无已登记课程、安排或待办。", ""])
    chunks.append("本邮件概括已登记记录和实际处理结果。")
    return "\n".join(chunks)


def preview_digest(user_id: str) -> dict[str, Any]:
    """预览当前时区的日报内容和目标，不排队也不发送邮件。"""
    preferences = get_preferences(user_id)
    zone = ZoneInfo(preferences["timezone"])
    local_date = datetime.now(UTC).astimezone(zone).date()
    sections, from_seq, through_seq = _sections(user_id, local_date, preferences["include"])
    return {
        "local_date": local_date.isoformat(),
        "recipient": preferences["recipient"],
        "subject": f"通知管理 · {local_date.isoformat()} 日报",
        "body_text": _digest_body(local_date, sections),
        "sections": sections,
        "change_seq_from": from_seq,
        "change_seq_through": through_seq,
        "delivery_status": "preview_only",
    }


def _outbox_public(item: MailOutbox) -> dict[str, Any]:
    """只向 owner 返回自己的邮件快照与受控投递状态。"""
    return {
        "id": item.id,
        "purpose": item.purpose,
        "template_name": item.template_name,
        "recipient": item.recipient,
        "subject": item.subject,
        "body_text": item.body_text,
        "status": item.status,
        "priority": item.priority,
        "attempt": item.attempt,
        "accepted_at_epoch": item.accepted_at_epoch,
        "error_code": item.error_code,
        "created_at_epoch": item.created_at_epoch,
        "updated_at_epoch": item.updated_at_epoch,
    }


def _create_outbox(
    *,
    user_id: str,
    purpose: str,
    template_name: str,
    address: VerifiedAddress,
    subject: str,
    body_text: str,
    metadata: dict[str, Any],
    dedupe_key: str,
    priority: int,
    now: int,
) -> MailOutbox:
    """写入不可变消息快照；重复日期或重试键沿用已有 outbox 项。"""
    with DbSession(_engine()) as db:
        existing = db.scalar(select(MailOutbox).where(MailOutbox.dedupe_key == dedupe_key))
        if existing is not None:
            item_id = existing.id
        else:
            item_id = str(uuid4())
            db.add(
                MailOutbox(
                    id=item_id,
                    user_id=user_id,
                    purpose=purpose,
                    template_name=template_name,
                    recipient=address.email,
                    recipient_address_id=address.id,
                    recipient_verified_at_epoch=address.verified_at_epoch,
                    subject=subject,
                    body_text=body_text,
                    metadata_json=metadata,
                    dedupe_key=dedupe_key,
                    priority=priority,
                    status="queued",
                    job_id=None,
                    scheduled_at_epoch=now,
                    attempt=0,
                    max_attempts=3,
                    accepted_at_epoch=None,
                    error_code=None,
                    created_at_epoch=now,
                    updated_at_epoch=now,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.scalar(select(MailOutbox).where(MailOutbox.dedupe_key == dedupe_key))
                if existing is None:
                    raise
                item_id = existing.id
    _ensure_send_job(item_id)
    with DbSession(_engine()) as db:
        item = db.get(MailOutbox, item_id)
        if item is None:
            raise ApiProblem("mail_outbox_unavailable", "邮件任务无法读取。", 503)
        return item


def _ensure_send_job(outbox_id: str) -> str:
    """outbox 与 worker job 使用各自幂等键，崩溃后可重新补齐关联。"""
    engine = _engine()
    with DbSession(engine) as db:
        item = db.get(MailOutbox, outbox_id)
    if item is None or item.status != "queued":
        return item.job_id if item else ""
    job = enqueue_job(
        engine,
        user_id=item.user_id,
        kind="mail.send",
        payload={"outbox_id": item.id},
        idempotency_key=f"mail-send:{item.id}",
        max_attempts=item.max_attempts,
        priority=item.priority,
    )
    with DbSession(engine) as db, db.begin():
        current = db.get(MailOutbox, item.id)
        if current is not None and current.job_id is None:
            current.job_id = job.id
    return job.id


def queue_test_mail(user_id: str) -> dict[str, Any]:
    """用户点击确认后，向本人已验证地址排队一封无个人业务数据的测试信。"""
    now = int(clock.time())
    with DbSession(_engine()) as db:
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        address_id = preference.recipient_address_id if preference else None
        address = db.scalar(
            select(VerifiedAddress).where(
                VerifiedAddress.id == address_id if address_id else VerifiedAddress.user_id == user_id,
                VerifiedAddress.user_id == user_id,
            ).order_by(VerifiedAddress.is_primary.desc())
        )
    if address is None:
        raise ApiProblem("verified_address_required", "没有可用的本人已验证邮箱。", 422)
    dedupe_key = f"test:{user_id}:{uuid4()}"
    item = _create_outbox(
        user_id=user_id,
        purpose="test",
        template_name="mail_test_v1",
        address=address,
        subject="通知管理测试邮件（合成内容）",
        body_text="这是一封由你主动请求的测试邮件。\n\n邮件投递适配器已接受本条合成测试，不包含日程、通知或微信内容。",
        metadata={},
        dedupe_key=dedupe_key,
        priority=80,
        now=now,
    )
    return _outbox_public(item)


def queue_onboarding_reminder(user_id: str, *, pending: str, started_at_epoch: int, now_epoch: int) -> dict | None:
    """设置未完成提醒只投递本人已验证主邮箱，不要求用户先完成邮件设置。"""
    with DbSession(_engine()) as db:
        address = db.scalar(select(VerifiedAddress).where(VerifiedAddress.user_id == user_id)
            .order_by(VerifiedAddress.is_primary.desc(), VerifiedAddress.verified_at_epoch).limit(1))
    if address is None:
        return None
    item = _create_outbox(user_id=user_id, purpose='onboarding', template_name='onboarding_reminder_v1', address=address,
        subject='知序 · 继续完成首次设置',
        body_text=f'你已开始使用知序，以下设置尚未完成：{pending}。\n\n打开知序的“首次使用引导”（/onboarding），主助手可继续协助设置和添加事务。\n本轮设置提醒只发送一次；可在引导页关闭邮件提醒或选择稍后设置。',
        metadata={'source': 'onboarding', 'started_at_epoch': started_at_epoch},
        dedupe_key=f'onboarding:{user_id}:{started_at_epoch}', priority=20, now=now_epoch)
    return _outbox_public(item)


def queue_agent_message(
    user_id: str,
    *,
    subject: str,
    body_text: str,
    dedupe_key: str,
    priority: int = 30,
    attachment_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Agent 只能向本人已验证地址发信；内容快照和去重键由服务端校验。"""
    from .agent_material_service import material_file
    identifiers = list(dict.fromkeys(attachment_ids or []))
    if len(identifiers) > 10:
        raise ApiProblem('attachment_limit', '每封邮件最多 10 个附件。', 422)
    materials = [material_file(user_id, identifier)[1] for identifier in identifiers]
    if sum(item.size_bytes for item in materials) > 10 * 1024 * 1024:
        raise ApiProblem('attachment_size', '邮件附件合计不能超过 10 MB。', 422)
    now = int(clock.time())
    with DbSession(_engine()) as db:
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
    if address is None:
        raise ApiProblem("verified_address_required", "请先在邮件设置中绑定已验证地址。", 422)
    item = _create_outbox(
        user_id=user_id,
        purpose="agent_notice",
        template_name="agent_message_v1",
        address=address,
        subject=subject[:300],
        body_text=body_text[:20_000],
        metadata={"source": "agent", "attachment_ids": identifiers},
        dedupe_key=dedupe_key[:250],
        priority=max(10, min(priority, 90)),
        now=now,
    )
    return _outbox_public(item)


def queue_change_alert(
    user_id: str,
    *,
    subject: str,
    body_text: str,
    source_id: str,
    change_set_id: str,
    flag_name: str,
) -> dict[str, Any] | None:
    """按用户开关发送重要变更摘要；未配置或不满足开关时静默跳过。"""
    now = int(clock.time())
    with DbSession(_engine()) as db:
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        flags = DEFAULT_NOTIFICATION_FLAGS | ((preference.notification_flags_json or {}) if preference else {})
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
    if address is None or not flags.get(flag_name, True):
        return None
    item = _create_outbox(
        user_id=user_id,
        purpose="agent_notice",
        template_name="change_alert_v1",
        address=address,
        subject=subject[:300],
        body_text=body_text[:20_000],
        metadata={
            "source_id": source_id,
            "change_set_id": change_set_id,
            "flag": flag_name,
        },
        dedupe_key=f"change-alert:{user_id}:{change_set_id}:{flag_name}"[:250],
        priority=15,
        now=now,
    )
    return _outbox_public(item)


def list_outbox(user_id: str, limit: int = 50) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        items = db.scalars(
            select(MailOutbox)
            .where(MailOutbox.user_id == user_id)
            .order_by(MailOutbox.created_at_epoch.desc(), MailOutbox.id)
            .limit(max(1, min(limit, 100)))
        ).all()
    return {"items": [_outbox_public(item) for item in items]}


def retry_failed_mail(user_id: str, outbox_id: str) -> dict[str, Any]:
    """只允许用户显式重试确定失败的邮件，unknown 状态必须先人工核实。"""
    now = int(clock.time())
    with DbSession(_engine()) as db:
        original = db.scalar(
            select(MailOutbox).where(MailOutbox.id == outbox_id, MailOutbox.user_id == user_id)
        )
        if original is None:
            raise ApiProblem("not_found", "未找到该邮件记录。", 404)
        if original.status != "failed":
            raise ApiProblem("mail_not_retryable", "仅确定失败的邮件可重新排队；未知状态不能自动重发。", 409)
        address = db.scalar(
            select(VerifiedAddress).where(
                VerifiedAddress.id == original.recipient_address_id,
                VerifiedAddress.user_id == user_id,
                VerifiedAddress.email == original.recipient,
                VerifiedAddress.verified_at_epoch == original.recipient_verified_at_epoch,
            )
        )
        if address is None:
            raise ApiProblem("verified_address_changed", "原收件地址已失效，请重新确认设置。", 409)
        payload = {
            "purpose": original.purpose,
            "template_name": original.template_name,
            "subject": original.subject,
            "body_text": original.body_text,
            "metadata": deepcopy(original.metadata_json),
        }
    item = _create_outbox(
        user_id=user_id,
        purpose=payload["purpose"],
        template_name=payload["template_name"],
        address=address,
        subject=payload["subject"],
        body_text=payload["body_text"],
        metadata=payload["metadata"],
        dedupe_key=f"manual-retry:{outbox_id}:{uuid4()}",
        priority=80,
        now=now,
    )
    return _outbox_public(item)


def dispatch_due_digests(engine, now_epoch: int | None = None) -> int:
    """worker 每分钟扫描有限用户集合，并只对当日本地日期补发日报。"""
    now = int(clock.time()) if now_epoch is None else now_epoch
    with DbSession(engine) as db:
        due = db.scalars(
            select(DigestPreference)
            .where(DigestPreference.enabled.is_(True), DigestPreference.next_send_epoch.is_not(None), DigestPreference.next_send_epoch <= now)
            .order_by(DigestPreference.next_send_epoch)
            .limit(500)
        ).all()
        entries = [
            (item.user_id, item.version, item.timezone, item.send_time, item.next_send_epoch, item.recipient_address_id)
            for item in due
        ]
    dispatched = 0
    for user_id, version, timezone_name, send_time, due_epoch, address_id in entries:
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            continue
        local_today = datetime.fromtimestamp(now, UTC).astimezone(zone).date()
        due_date = datetime.fromtimestamp(due_epoch, UTC).astimezone(zone).date()
        with DbSession(engine) as db:
            address = db.scalar(
                select(VerifiedAddress).where(
                    VerifiedAddress.id == address_id,
                    VerifiedAddress.user_id == user_id,
                )
            ) if address_id else None
        if address is None:
            with DbSession(engine) as db, db.begin():
                db.execute(
                    update(DigestPreference)
                    .where(DigestPreference.user_id == user_id, DigestPreference.version == version)
                    .values(enabled=False, next_send_epoch=None, updated_at_epoch=now, version=version + 1)
                )
            continue
        if due_date == local_today:
            idempotency = f"digest:{user_id}:{address.id}:{address.verified_at_epoch}:{local_today.isoformat()}"
            enqueue_job(
                engine,
                user_id=user_id,
                kind="digest.compose",
                payload={
                    "local_date": local_today.isoformat(),
                    "preference_version": version,
                    "recipient_address_id": address.id,
                    "recipient_verified_at_epoch": address.verified_at_epoch,
                },
                idempotency_key=idempotency,
                max_attempts=3,
                priority=60,
            )
            dispatched += 1
        next_epoch = _next_send_epoch(now, timezone_name, send_time)
        with DbSession(engine) as db, db.begin():
            result = db.execute(
                update(DigestPreference)
                .where(
                    DigestPreference.user_id == user_id,
                    DigestPreference.version == version,
                    DigestPreference.next_send_epoch == due_epoch,
                )
                .values(next_send_epoch=next_epoch, updated_at_epoch=now)
            )
            _ = result.rowcount
    return dispatched


def handle_digest_compose_job(claim: ClaimedJob) -> dict[str, Any]:
    """worker 根据用户数据生成不可变日报快照，再交由邮件发送任务处理。"""
    user_id = claim.user_id
    payload = claim.payload
    local_date = date.fromisoformat(payload["local_date"])
    now = int(clock.time())
    with DbSession(_engine()) as db:
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id))
        address = db.scalar(
            select(VerifiedAddress).where(
                VerifiedAddress.id == payload["recipient_address_id"],
                VerifiedAddress.user_id == user_id,
                VerifiedAddress.verified_at_epoch == payload["recipient_verified_at_epoch"],
            )
        )
        if (
            preference is None
            or not preference.enabled
            or preference.version != payload["preference_version"]
            or preference.recipient_address_id != payload["recipient_address_id"]
            or address is None
        ):
            return {"status": "skipped", "reason": "preference_or_recipient_changed"}
        timezone_name = preference.timezone
        include = deepcopy(preference.include_json)
    sections, from_seq, through_seq = _sections(user_id, local_date, include)
    body = _digest_body(local_date, sections)
    dedupe_key = f"digest:{user_id}:{address.id}:{address.verified_at_epoch}:{local_date.isoformat()}"
    item = _create_outbox(
        user_id=user_id,
        purpose="digest",
        template_name="daily_digest_v1",
        address=address,
        subject=f"通知管理 · {local_date.isoformat()} 日报",
        body_text=body,
        metadata={
            "local_date": local_date.isoformat(),
            "timezone": timezone_name,
            "preference_version": payload["preference_version"],
            "change_seq_from": from_seq,
            "change_seq_through": through_seq,
        },
        dedupe_key=dedupe_key,
        priority=40,
        now=now,
    )
    # 定时日报同时保存为可网页回看的报告快照，发送状态由 outbox 回写。
    report_dedupe = f"report:daily:{local_date.isoformat()}:{local_date.isoformat()}:{timezone_name}"
    with DbSession(_engine()) as db, db.begin():
        report = db.scalar(select(ActivityReport).where(ActivityReport.dedupe_key == report_dedupe))
        if report is None:
            report = ActivityReport(
                    id=str(uuid4()),
                    user_id=user_id,
                    kind="daily",
                    period_start=local_date.isoformat(),
                    period_end=local_date.isoformat(),
                    timezone=timezone_name,
                    status="queued",
                    subject=f"通知管理 · {local_date.isoformat()} 日报",
                    body_text=body,
                    sections_json=sections,
                    metadata_json={
                        "preference_version": payload["preference_version"],
                        "change_seq_from": from_seq,
                        "change_seq_through": through_seq,
                    },
                    dedupe_key=report_dedupe,
                    outbox_id=item.id,
                    generated_at_epoch=now,
                    version=1,
            )
            db.add(report)
            from . import operation_service

            operation_service.record_operation(
                db,
                user_id=user_id,
                actor_type="system",
                actor_id=None,
                action="report.daily.generate",
                object_type="activity_report",
                object_id=report.id,
                summary=f"生成定时日报：{local_date.isoformat()}",
                after={"subject": report.subject, "outbox_id": item.id},
                created_at_epoch=now,
            )
        elif report.outbox_id is None:
            report.outbox_id = item.id
            report.status = "queued"
            report.version += 1
    return {"status": "queued", "outbox_id": item.id}


def _advance_digest_cursor(db: DbSession, item: MailOutbox, now: int) -> None:
    """只在 accepted 后推进精确序号游标；重复 worker 恢复安全。"""
    if item.purpose != "digest":
        return
    through = int(item.metadata_json.get("change_seq_through", 0))
    local_date = str(item.metadata_json.get("local_date", "")) or None
    cursor = db.get(DigestCursor, item.user_id)
    if cursor is None:
        cursor = DigestCursor(user_id=item.user_id, last_change_seq=through, last_sent_local_date=local_date, updated_at_epoch=now)
        db.add(cursor)
    else:
        cursor.last_change_seq = max(cursor.last_change_seq, through)
        if local_date and (cursor.last_sent_local_date is None or local_date > cursor.last_sent_local_date):
            cursor.last_sent_local_date = local_date
        cursor.updated_at_epoch = now


def _mark_related_delivery(db: DbSession, item: MailOutbox, now: int) -> None:
    """邮件被服务接受后同步提醒/报告状态，不以短信箱“已读”冒充真实送达。"""
    if item.purpose == "reminder":
        reminder_ids = [str(value) for value in (item.metadata_json or {}).get("reminder_ids", [])]
        if reminder_ids:
            db.execute(
                update(Reminder)
                .where(Reminder.id.in_(reminder_ids), Reminder.user_id == item.user_id)
                .values(status="sent", sent_at_epoch=now, updated_at_epoch=now)
            )
    if item.purpose in {"digest", "daily_report", "weekly_report"}:
        db.execute(
            update(ActivityReport)
            .where(ActivityReport.outbox_id == item.id, ActivityReport.user_id == item.user_id)
            .values(status="sent", sent_at_epoch=now, version=ActivityReport.version + 1)
        )


def handle_mail_send_job(claim: ClaimedJob) -> dict[str, Any]:
    """只发送已验证地址快照；unknown 状态不自动重试，避免重复私密邮件。"""
    user_id = claim.user_id
    outbox_id = str(claim.payload.get("outbox_id", ""))
    now = int(clock.time())
    with DbSession(_engine()) as db:
        item = db.scalar(select(MailOutbox).where(MailOutbox.id == outbox_id, MailOutbox.user_id == user_id))
    if item is None:
        raise ApiProblem("mail_outbox_not_found", "邮件队列记录不存在。", 404)
    if item.purpose == 'onboarding' and item.status == 'queued':
        from ..models import UserOnboarding
        with DbSession(_engine()) as db:
            onboarding = db.get(UserOnboarding, user_id)
            # 稍后设置到期后仍可投递首次提醒，不能因为持久状态仍叫 snoozed 而永久取消。
            cancel = onboarding is None or not onboarding.reminder_enabled or onboarding.status == 'completed' or (
                onboarding.status == 'snoozed' and (onboarding.snoozed_until_epoch or 0) > now)
        if cancel:
            with DbSession(_engine()) as db, db.begin():
                current = db.get(MailOutbox, item.id)
                if current and current.status == 'queued':
                    current.status, current.error_code = 'cancelled', 'onboarding_preference_changed'
            return {'status': 'cancelled', 'outbox_id': item.id}
    if item.status == "accepted":
        with DbSession(_engine()) as db, db.begin():
            current = db.get(MailOutbox, item.id)
            if current is not None:
                _advance_digest_cursor(db, current, now)
        return {"status": "accepted", "outbox_id": item.id}
    if item.status in {"unknown", "failed", "cancelled"}:
        return {"status": item.status, "outbox_id": item.id}
    if item.status == "sending":
        with DbSession(_engine()) as db, db.begin():
            current = db.get(MailOutbox, item.id)
            if current is not None and current.status == "sending":
                current.status = "unknown"
                current.error_code = "worker_interrupted_during_send"
                current.updated_at_epoch = now
        return {"status": "unknown", "outbox_id": item.id}

    with DbSession(_engine()) as db:
        address = db.scalar(
            select(VerifiedAddress).where(
                VerifiedAddress.id == item.recipient_address_id,
                VerifiedAddress.user_id == user_id,
                VerifiedAddress.email == item.recipient,
                VerifiedAddress.verified_at_epoch == item.recipient_verified_at_epoch,
            )
        )
        preference = db.scalar(select(DigestPreference).where(DigestPreference.user_id == user_id)) if item.purpose == "digest" else None
    if address is None:
        with DbSession(_engine()) as db, db.begin():
            current = db.get(MailOutbox, item.id)
            if current:
                current.status = "failed"
                current.error_code = "verified_recipient_changed"
                current.updated_at_epoch = now
        return {"status": "failed", "outbox_id": item.id, "error_code": "verified_recipient_changed"}
    if item.purpose == "digest" and (
        preference is None
        or not preference.enabled
        or preference.version != item.metadata_json.get("preference_version")
        or preference.recipient_address_id != item.recipient_address_id
    ):
        with DbSession(_engine()) as db, db.begin():
            current = db.get(MailOutbox, item.id)
            if current:
                current.status = "cancelled"
                current.error_code = "preference_changed"
                current.updated_at_epoch = now
        return {"status": "cancelled", "outbox_id": item.id}

    with DbSession(_engine()) as db, db.begin():
        current = db.get(MailOutbox, item.id)
        if current is None or current.status != "queued":
            return {"status": current.status if current else "missing", "outbox_id": item.id}
        current.status = "sending"
        current.attempt += 1
        current.updated_at_epoch = now
        recipient, subject, body = current.recipient, current.subject, current.body_text
        current_attempt = current.attempt
    try:
        # 发信前再次验证附件归属，撤销或丢失的材料不会进入邮件。
        from .agent_material_service import material_file
        attachments = []
        for identifier in (item.metadata_json or {}).get('attachment_ids', []):
            path, material = material_file(user_id, identifier)
            attachments.append((material.filename, material.media_type, path.read_bytes()))
        if attachments:
            _transport().send_message(recipient, subject, body, attachments=attachments)
        else:
            _transport().send_message(recipient, subject, body)
    except ApiProblem as error:
        _set_outbox_failure(item.id, error.code, 'failed', now)
        return {'status': 'failed', 'outbox_id': item.id, 'error_code': error.code}
    except MailNotConfigured:
        _set_outbox_failure(item.id, "mail_not_configured", "failed", now)
        return {"status": "failed", "outbox_id": item.id, "error_code": "mail_not_configured"}
    except MailDeliveryError as error:
        if error.state == "unknown":
            _set_outbox_failure(item.id, error.code, "unknown", now)
            return {"status": "unknown", "outbox_id": item.id, "error_code": error.code}
        if error.retryable and current_attempt < item.max_attempts and claim.attempt < 3:
            _set_outbox_failure(item.id, error.code, "queued", now)
            raise MailRetryRequest(error.code) from None
        _set_outbox_failure(item.id, error.code, "failed", now)
        return {"status": "failed", "outbox_id": item.id, "error_code": error.code}
    except Exception:
        _set_outbox_failure(item.id, "mail_delivery_unknown", "unknown", now)
        return {"status": "unknown", "outbox_id": item.id, "error_code": "mail_delivery_unknown"}

    with DbSession(_engine()) as db, db.begin():
        current = db.get(MailOutbox, item.id)
        if current is not None:
            current.status = "accepted"
            current.accepted_at_epoch = now
            current.error_code = None
            current.updated_at_epoch = now
            _advance_digest_cursor(db, current, now)
            _mark_related_delivery(db, current, now)
    return {"status": "accepted", "outbox_id": item.id}


def _set_outbox_failure(outbox_id: str, code: str, status: str, now: int) -> None:
    with DbSession(_engine()) as db, db.begin():
        current = db.get(MailOutbox, outbox_id)
        if current is not None:
            current.status = status
            current.error_code = code[:80]
            current.updated_at_epoch = now
            reminder_ids = [str(value) for value in (current.metadata_json or {}).get("reminder_ids", [])]
            if reminder_ids:
                reminder_status = "queued" if status == "queued" else status
                db.execute(
                    update(Reminder)
                    .where(Reminder.id.in_(reminder_ids), Reminder.user_id == current.user_id)
                    .values(status=reminder_status, error_code=code[:80], updated_at_epoch=now)
                )
            if current.purpose in {"digest", "daily_report", "weekly_report"}:
                report_status = "queued" if status == "queued" else status
                db.execute(
                    update(ActivityReport)
                    .where(ActivityReport.outbox_id == current.id, ActivityReport.user_id == current.user_id)
                    .values(status=report_status, version=ActivityReport.version + 1)
                )


def mail_transport_status() -> dict[str, str]:
    """只返回已配置状态，不泄露发件地址、服务器、用户名或密钥。"""
    transport = _transport()
    channel = type(transport).__name__
    return {
        "channel": channel,
        "status": "disabled" if isinstance(transport, DisabledMailTransport) else "configured",
        "delivery_claim": "adapter_acceptance_only",
    }
