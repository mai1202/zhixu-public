"""把通知要求组织成办理路线，复用任务、日程、证据和操作审计。"""

from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import secrets
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..models import Event, JourneyMember, ScheduleEntry, SourceSegment, StudentIntent, StudentJourney, Task, User
from ..schemas.agent_ops import TaskCreateInput
from ..schemas.business import DeadlineCandidate
from ..schemas.journeys import AvailabilityInput
from ..schemas.timetable import ScheduleEntryInput
from ..text_sanitize import clean_title
from . import cross_module_service, operation_service, timetable_service


def _engine():
    return current_app.extensions["notification_db_engine"]


def _now():
    return datetime.now(timezone.utc)


def _owned_task(db, user_id, task_id):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id))
    if task is None:
        raise ApiProblem("not_found", "未找到该事务。", 404)
    return task


def _event(db, task):
    """无直接事项关联时，只在通知恰有一个事项的情况下使用其要求。"""
    if task.event_id:
        return db.scalar(select(Event).where(Event.id == task.event_id, Event.user_id == task.user_id))
    if task.notice_id:
        events = db.scalars(select(Event).where(Event.notice_id == task.notice_id,
            Event.user_id == task.user_id, Event.status == "open").limit(2)).all()
        return events[0] if len(events) == 1 else None
    return None


def _access(db, user_id, journey_id, *, owner=False):
    route = db.get(StudentJourney, journey_id)
    member = db.scalar(select(JourneyMember).where(JourneyMember.journey_id == journey_id,
        JourneyMember.user_id == user_id, JourneyMember.status == "active"))
    if route is None or (route.user_id != user_id and (owner or member is None)):
        raise ApiProblem("not_found", "未找到该办理路线。", 404)
    return route


def _members(db, route):
    return db.scalars(select(JourneyMember).where(JourneyMember.journey_id == route.id,
        JourneyMember.status == "active").order_by(JourneyMember.created_at_epoch, JourneyMember.id)).all()


def _public(db, route, viewer):
    """协作视图只共享主动发布的步骤，不包含私人原文、引文、日程或邮箱。"""
    owner = viewer == route.user_id
    members = _members(db, route)
    aliases = {route.user_id: "发起人", **{member.user_id: member.alias for member in members}}
    steps = []
    for raw in route.steps_json:
        item = {key: deepcopy(raw.get(key)) for key in (
            "id", "title", "depends_on", "estimate_minutes", "assigned_user_id", "status")}
        # 步骤标题可能来自旧通知原文，展示时统一去掉微信占位符与不可见字符。
        item["title"] = clean_title(item.get("title"), "未命名步骤")
        item["assignee_label"] = aliases.get(item["assigned_user_id"], "成员已退出，请重新分配")
        if owner:
            item.update({key: deepcopy(raw.get(key)) for key in ("source_key", "source_removed", "schedule_ids")})
        steps.append(item)
    result = {
        "id": route.id, "title": clean_title(route.title if owner else route.shared_title, "未命名路线"),
        "is_owner": owner, "version": route.version, "steps": steps,
        "members": [{"user_id": route.user_id, "alias": "我" if owner else "发起人", "is_owner": True}]
            + [{"user_id": item.user_id, "alias": item.alias, "is_owner": False} for item in members],
    }
    if owner:
        task = _owned_task(db, viewer, route.task_id)
        event = _event(db, task)
        # 证据只认可同账户、同来源中仍能精确定位的引文。
        segments = {item.id: item.text for item in db.scalars(select(SourceSegment).where(
            SourceSegment.user_id == viewer, SourceSegment.source_document_id == event.source_document_id))} if event else {}
        evidence = []
        for refs in (event.evidence_json or {}).values() if event else []:
            for ref in refs if isinstance(refs, list) else []:
                if isinstance(ref, dict) and ref.get("quote") and ref["quote"] in segments.get(ref.get("segment_id"), ""):
                    if ref not in evidence:
                        evidence.append(ref)
        result.update({"task_id": route.task_id, "task_status": task.status, "deadline": task.due_json,
            "title": clean_title(task.title, "未命名事务"),
            "source_changed": (event.version if event else None) != route.source_version,
            "source": {"actor": event.actor, "stage": event.stage, "submission": event.submission,
                "unknowns": event.unknowns_json, "evidence": evidence[:20], "notice_id": event.notice_id,
                "materials_total": len(event.materials_json),
                "unlisted_materials": [material for material in event.materials_json
                    if f"material:{material}" not in {step.get("source_key") for step in route.steps_json}]} if event else None,
            "shared_title": route.shared_title,
            "invite_active": bool(route.invite_digest and (route.invite_expires_at_epoch or 0) > int(_now().timestamp()))})
    return result


def _cas(db, route, expected, actor, action, **values):
    """数据库条件更新守住并发版本；修改与操作记录在同一事务内提交。"""
    from . import planning_guard
    planning_guard.lock(db, route.user_id)
    before = {"version": route.version, "steps": deepcopy(route.steps_json)}
    result = db.execute(update(StudentJourney).where(StudentJourney.id == route.id,
        StudentJourney.version == expected).values(**values, version=expected + 1,
        updated_at_epoch=int(_now().timestamp())).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ApiProblem("version_conflict", "路线已变化，请刷新后再操作。", 409)
    db.expire(route)
    # 条件 SQL 不经过 ORM dirty 检测，显式递增规划序列并保留自动触发。
    state = planning_guard.lock(db, route.user_id)
    state.version += 1
    state.changed_at_epoch = int(_now().timestamp())
    if state.automatic:
        planning_guard.enqueue(db, route.user_id, "planning.replan", {},
            f"planning:{state.changed_at_epoch // 60}", delay=60)
    return operation_service.record_operation(db, user_id=route.user_id, actor_type="user", actor_id=actor,
        action=action, object_type="journey", object_id=route.id, summary=f"办理路线：{action}",
        before=before, after={"version": route.version, "steps": deepcopy(route.steps_json)})


def _generated_steps(task, event):
    """仅从明确材料和任务标题生成清单，不脑补学院审批或依赖。"""
    materials = list(dict.fromkeys(event.materials_json)) if event else []
    requirements = [(f"material:{material}", f"准备：{material}") for material in materials[:39]]
    requirements.append(("action:submit", f"完成：{clean_title(task.title, '未命名事务')}"))
    return [{"id": str(uuid4()), "title": title[:300], "source_key": key,
        "depends_on": [], "estimate_minutes": None, "assigned_user_id": task.user_id,
        "status": "open", "source_removed": False, "schedule_ids": []} for key, title in requirements]


def _create(db, task):
    existing = db.scalar(select(StudentJourney).where(StudentJourney.task_id == task.id))
    if existing:
        return existing
    event = _event(db, task)
    now = int(_now().timestamp())
    route = StudentJourney(id=str(uuid4()), user_id=task.user_id, task_id=task.id, title=clean_title(task.title, "未命名路线"),
        steps_json=_generated_steps(task, event), source_version=event.version if event else None,
        version=1, created_at_epoch=now, updated_at_epoch=now)
    db.add(route)
    db.flush()
    operation_service.record_operation(db, user_id=task.user_id, actor_type="user", actor_id=task.user_id,
        action="journey.create", object_type="journey", object_id=route.id, summary=f"建立办理路线：{task.title}")
    return route


def create_journey(user_id, task_id):
    try:
        with Session(_engine()) as db, db.begin():
            return _public(db, _create(db, _owned_task(db, user_id, task_id)), user_id)
    except IntegrityError:
        # 同一任务的并发创建复用唯一记录；其他约束异常不伪装成成功。
        with Session(_engine()) as db:
            route = db.scalar(select(StudentJourney).where(StudentJourney.task_id == task_id, StudentJourney.user_id == user_id))
            if route is None:
                raise
            return _public(db, route, user_id)


def list_journeys(user_id):
    with Session(_engine()) as db:
        joined = select(JourneyMember.journey_id).where(JourneyMember.user_id == user_id, JourneyMember.status == "active")
        query = select(StudentJourney).where(or_(StudentJourney.user_id == user_id, StudentJourney.id.in_(joined)))
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        routes = db.scalars(query.order_by(StudentJourney.updated_at_epoch.desc(), StudentJourney.id).limit(100)).all()
        return {"items": [_public(db, route, user_id) for route in routes], "total": total, "limit": 100}


def get_journey(user_id, journey_id):
    with Session(_engine()) as db:
        return _public(db, _access(db, user_id, journey_id), user_id)


def save_journey(user_id, journey_id, payload):
    with Session(_engine()) as db, db.begin():
        route = _access(db, user_id, journey_id, owner=True)
        previous = {step["id"]: step for step in route.steps_json}
        # 用户已在日程取消的预约不再锁住步骤编辑；关联 ID 不作为永久锁。
        scheduled_ids = {value for step in previous.values() for value in step.get("schedule_ids", [])}
        active_ids = set(db.scalars(select(ScheduleEntry.id).where(ScheduleEntry.id.in_(scheduled_ids),
            ScheduleEntry.user_id == user_id, ScheduleEntry.status == "active")))
        previous = deepcopy(previous)
        for step in previous.values():
            step["schedule_ids"] = [value for value in step.get("schedule_ids", []) if value in active_ids]
        allowed = {route.user_id, *(member.user_id for member in _members(db, route))}
        steps = []
        for data in payload.steps:
            item = data.model_dump(mode="json")
            item["assigned_user_id"] = item["assigned_user_id"] or route.user_id
            old = previous.get(item["id"], {})
            # 已退出成员的完成记录保留历史归属，不阻塞其他步骤的维护。
            historical_completion = old.get("status") == "done" and old.get("assigned_user_id") == item["assigned_user_id"]
            if item["assigned_user_id"] not in allowed and not historical_completion:
                raise ApiProblem("invalid_assignee", "请先邀请成员加入，再分配步骤。", 422)
            # 进度和安排由独立操作维护；编辑清单不能代替同伴确认完成。
            item.update({"status": old.get("status", "open"), "source_key": old.get("source_key"),
                "source_removed": old.get("source_removed", False), "schedule_ids": old.get("schedule_ids", [])})
            if old.get("status") == "done" and (item["title"] != old["title"] or item["depends_on"] != old["depends_on"]
                    or item["assigned_user_id"] != old["assigned_user_id"]):
                raise ApiProblem("completed_step", "修改已完成步骤前，请由负责人重新打开。", 409)
            if old.get("schedule_ids") and (item["title"] != old["title"] or item["assigned_user_id"] != old["assigned_user_id"]
                    or item["depends_on"] != old["depends_on"] or item["estimate_minutes"] != old.get("estimate_minutes")):
                raise ApiProblem("scheduled_step", "请先在个人日程取消该步骤的安排，再修改步骤。", 409)
            steps.append(item)
        removed = [step for key, step in previous.items() if key not in {item["id"] for item in steps}]
        if any(step.get("schedule_ids") for step in removed):
            raise ApiProblem("scheduled_step", "已安排步骤请先取消个人日程，再删除。", 409)
        _cas(db, route, payload.expected_version, user_id, "journey.edit", steps_json=steps)
        return _public(db, route, user_id)


def refresh_source(user_id, journey_id, expected):
    with Session(_engine()) as db, db.begin():
        route = _access(db, user_id, journey_id, owner=True)
        task = _owned_task(db, user_id, route.task_id)
        event = _event(db, task)
        generated = _generated_steps(task, event)
        keys = {step["source_key"] for step in generated}
        steps = deepcopy(route.steps_json)
        old_keys = {step.get("source_key") for step in steps}
        for step in steps:
            # 源要求撤回时保留人工编辑、进度与预约，由用户决定如何处理。
            step["source_removed"] = bool(step.get("source_key") and step["source_key"] not in keys)
        additions = [step for step in generated if step["source_key"] not in old_keys]
        if len(steps) + len(additions) > 40:
            raise ApiProblem("too_many_steps", "请整理步骤后再同步新增材料（最多 40 步）。", 422)
        steps.extend(additions)
        _cas(db, route, expected, user_id, "journey.refresh_source", steps_json=steps,
            source_version=event.version if event else None)
        return _public(db, route, user_id)


def step_action(user_id, journey_id, step_id, payload):
    with Session(_engine()) as db, db.begin():
        route = _access(db, user_id, journey_id)
        steps = deepcopy(route.steps_json)
        item = next((step for step in steps if step["id"] == step_id), None)
        if item is None or item["assigned_user_id"] != user_id:
            raise ApiProblem("step_forbidden", "只能确认分配给自己的步骤。", 403)
        if payload.action == "complete" and any(step["status"] != "done" for step in steps if step["id"] in item["depends_on"]):
            raise ApiProblem("dependency_pending", "请先完成前置步骤。", 409)
        if payload.action == "reopen" and any(step["status"] == "done" and step_id in step["depends_on"] for step in steps):
            raise ApiProblem("dependent_completed", "请先重新打开已完成的后续步骤。", 409)
        item["status"] = "done" if payload.action == "complete" else "open"
        _cas(db, route, payload.expected_version, user_id, f"journey.{payload.action}", steps_json=steps)
        return _public(db, route, user_id)


def invite(user_id, journey_id, payload):
    with Session(_engine()) as db, db.begin():
        route = _access(db, user_id, journey_id, owner=True)
        code = secrets.token_urlsafe(32)
        _cas(db, route, payload.expected_version, user_id, "journey.invite",
            shared_title=payload.shared_title.strip(), invite_digest=hashlib.sha256(code.encode()).hexdigest(),
            invite_expires_at_epoch=int(_now().timestamp()) + 86400)
        return {"journey": _public(db, route, user_id), "code": code, "expires_in_seconds": 86400}


def close_invite(user_id, journey_id, expected):
    with Session(_engine()) as db, db.begin():
        route = _access(db, user_id, journey_id, owner=True)
        _cas(db, route, expected, user_id, "journey.close_invite", invite_digest=None, invite_expires_at_epoch=None)
        return _public(db, route, user_id)


def join(user_id, payload):
    with Session(_engine()) as db, db.begin():
        route = db.scalar(select(StudentJourney).where(StudentJourney.invite_digest == hashlib.sha256(payload.code.encode()).hexdigest()))
        if route is None or (route.invite_expires_at_epoch or 0) <= int(_now().timestamp()):
            raise ApiProblem("invalid_invite", "邀请码已失效。", 404)
        if route.user_id == user_id:
            return _public(db, route, user_id)
        member = db.scalar(select(JourneyMember).where(JourneyMember.journey_id == route.id, JourneyMember.user_id == user_id))
        if member and member.status == "active":
            return _public(db, route, user_id)
        if len(_members(db, route)) >= 30:
            raise ApiProblem("member_limit", "首版每条路线最多 30 名协作成员。", 422)
        if member:
            member.status, member.alias = "active", payload.alias.strip()
        else:
            db.add(JourneyMember(id=str(uuid4()), journey_id=route.id, user_id=user_id,
                alias=payload.alias.strip(), status="active", created_at_epoch=int(_now().timestamp())))
        _cas(db, route, route.version, user_id, "journey.join")
        db.flush()
        return _public(db, route, user_id)


def revoke(user_id, journey_id, member_user_id, expected):
    with Session(_engine()) as db, db.begin():
        route = _access(db, user_id, journey_id, owner=True)
        member = db.scalar(select(JourneyMember).where(JourneyMember.journey_id == route.id,
            JourneyMember.user_id == member_user_id, JourneyMember.status == "active"))
        if member is None:
            raise ApiProblem("not_found", "成员不存在。", 404)
        member.status = "revoked"
        # 撤回访问时一起关闭邀请码，避免成员用同一码立刻重新加入。
        from .collaboration_service import clear_consent
        clear_consent(db, route.id, member_user_id)
        _cas(db, route, expected, user_id, "journey.revoke", invite_digest=None, invite_expires_at_epoch=None)
        db.flush()
        return _public(db, route, user_id)


def leave(user_id, journey_id, expected):
    """成员可以主动退出；不删除发起人的路线或其他人的步骤。"""
    with Session(_engine()) as db, db.begin():
        route = _access(db, user_id, journey_id)
        if route.user_id == user_id:
            raise ApiProblem("owner_cannot_leave", "发起人不能退出自己的路线。", 422)
        member = db.scalar(select(JourneyMember).where(JourneyMember.journey_id == route.id,
            JourneyMember.user_id == user_id, JourneyMember.status == "active"))
        member.status = "revoked"
        # 再次加入也需要本人重新同意使用个人空闲窗口。
        from .collaboration_service import clear_consent
        clear_consent(db, route.id, user_id)
        _cas(db, route, expected, user_id, "journey.leave")
        return {"left": True}


def get_intent(user_id):
    with Session(_engine()) as db:
        item = db.get(StudentIntent, user_id)
        return {"version": item.version, **item.profile_json} if item else {
            "version": 0, "interests": [], "facts": "", "availability": AvailabilityInput().model_dump()}


def save_intent(user_id, payload):
    values = payload.model_dump(mode="json", exclude={"expected_version"})
    try:
        with Session(_engine()) as db, db.begin():
            item = db.get(StudentIntent, user_id)
            if item is None and payload.expected_version == 0:
                db.add(StudentIntent(user_id=user_id, profile_json=values, version=1, updated_at_epoch=int(_now().timestamp())))
            elif item and db.execute(update(StudentIntent).where(StudentIntent.user_id == user_id,
                    StudentIntent.version == payload.expected_version).values(profile_json=values,
                    version=payload.expected_version + 1, updated_at_epoch=int(_now().timestamp()))).rowcount == 1:
                pass
            else:
                raise ApiProblem("version_conflict", "偏好已变化，请刷新后重试。", 409)
    except IntegrityError:
        raise ApiProblem("version_conflict", "偏好已变化，请刷新后重试。", 409) from None
    return get_intent(user_id)


def _free_intervals(db, user_id, availability, now, end):
    """复用课程与个人忙碌片段；全天安排按整天保守排除。"""
    zone = ZoneInfo(availability.timezone)
    local_now, local_end = now.astimezone(zone), end.astimezone(zone)
    busy = timetable_service._busy_intervals_for_range(db, user_id, local_now, local_end)
    for entry in db.scalars(select(ScheduleEntry).where(ScheduleEntry.user_id == user_id,
            ScheduleEntry.status == "active", ScheduleEntry.kind == "all_day",
            ScheduleEntry.local_date >= (local_now.date() - timedelta(days=1)).isoformat(),
            ScheduleEntry.local_date <= (local_end.date() + timedelta(days=1)).isoformat())):
        start = datetime.combine(datetime.fromisoformat(entry.local_date).date(), time.min, ZoneInfo(entry.timezone))
        busy.append({"start_at": start, "end_at": start + timedelta(days=1)})
    intervals = []
    day = local_now.date()
    while day <= local_end.date():
        if day.isoweekday() in availability.weekdays:
            # 容量统一用 UTC 计算，跨夏令时的夜间窗口不会多算或漏算一小时。
            window_start = datetime.combine(day, time.fromisoformat(availability.start_time), zone)
            window_end = datetime.combine(day, time.fromisoformat(availability.end_time), zone)
            for boundary in (window_start, window_end):
                if boundary.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != boundary.replace(tzinfo=None):
                    raise ApiProblem("invalid_local_window", "空闲窗口落在夏令时不存在的时刻，请调整时间。", 422)
            start = max(window_start.astimezone(timezone.utc), now.astimezone(timezone.utc))
            finish = min(window_end.astimezone(timezone.utc), end.astimezone(timezone.utc))
            chunks = [(start, finish)] if finish > start else []
            for block in busy:
                a, b = block["start_at"].astimezone(timezone.utc), block["end_at"].astimezone(timezone.utc)
                chunks = [(left, right) for x, y in chunks for left, right in
                    ([(x, y)] if b <= x or a >= y else [(x, min(a, y)), (max(b, x), y)]) if right > left]
            intervals.extend(chunks)
        day += timedelta(days=1)
    return sorted(intervals)


def _plan(db, route, payload):
    """只用已知耗时试算，未知工作量和同伴交接明确阻断可完成结论。"""
    task = _owned_task(db, route.user_id, route.task_id)
    if route.version != payload.expected_version:
        raise ApiProblem("version_conflict", "路线已变化，请刷新后试算。", 409)
    zone = ZoneInfo(payload.availability.timezone)
    actual_now = _now()
    # 将开始上取整到五分钟；提交时跨越时间桶会要求重算，避免写入过去。
    stamp = int(actual_now.timestamp())
    now = datetime.fromtimestamp(((stamp + 299) // 300) * 300, timezone.utc)
    due = task.due_json or {}
    deadline = None
    assumptions = ["仅在所填空闲窗口内安排，避开已录入课程与个人安排。", "建议安排不会自动完成任务。"]
    if due.get("utc"):
        deadline = datetime.fromisoformat(due["utc"].replace("Z", "+00:00")).astimezone(timezone.utc)
    elif due.get("local_date"):
        deadline = datetime.combine(datetime.fromisoformat(due["local_date"]).date(), time(23, 59), zone).astimezone(timezone.utc)
        assumptions.append("通知只有日期；试算暂按所选时区当日 23:59 计算，请核实实际截止时刻。")
    horizon = now + timedelta(days=14)
    end = min(deadline, horizon) if deadline else horizon
    free = _free_intervals(db, route.user_id, payload.availability, now, max(now, end))
    original_free = list(free)
    capacity = sum(int((b - a).total_seconds() // 60) for a, b in free)
    finish_times, slots, unresolved = {}, [], []
    pending = [step for step in route.steps_json if step["status"] != "done"]
    for step in route.steps_json:
        if step["status"] == "done":
            finish_times[step["id"]] = now
    required, booked = 0, 0
    while pending:
        ready = next((step for step in pending if all(parent in finish_times for parent in step["depends_on"])), None)
        if ready is None:
            unresolved.extend({"step_id": step["id"], "title": step["title"], "reason": "前置步骤尚未确认或未能安排"} for step in pending)
            break
        pending.remove(ready)
        if ready["assigned_user_id"] != route.user_id:
            unresolved.append({"step_id": ready["id"], "title": ready["title"], "reason": "等待同伴确认交接，未读取同伴私人日程"})
            continue
        if ready.get("estimate_minutes") is None:
            unresolved.append({"step_id": ready["id"], "title": ready["title"], "reason": "请先填写预计耗时"})
            continue
        earliest = max([now, *(finish_times[parent] for parent in ready["depends_on"])])
        reservations = db.scalars(select(ScheduleEntry).where(ScheduleEntry.id.in_(ready.get("schedule_ids", [])),
            ScheduleEntry.user_id == route.user_id, ScheduleEntry.status == "active")).all()
        active = [(datetime.fromisoformat(entry.start_at).astimezone(timezone.utc), datetime.fromisoformat(entry.end_at).astimezone(timezone.utc))
            for entry in reservations if entry.start_at and entry.end_at]
        valid = [(a, b) for a, b in active if a >= earliest and b <= end]
        reserved = sum(int((b - a).total_seconds() // 60) for a, b in valid)
        remaining = max(0, ready["estimate_minutes"] - reserved)
        booked += min(reserved, ready["estimate_minutes"])
        required += remaining
        last = max([earliest, *(b for _, b in valid)])
        new_free = []
        for left, right in free:
            start = max(left, earliest)
            minutes = min(remaining, max(0, int((right - start).total_seconds() // 60)))
            if minutes >= 5:
                stop = start + timedelta(minutes=minutes)
                slots.append({"step_id": ready["id"], "title": ready["title"], "start_at": start.astimezone(zone).isoformat(),
                    "end_at": stop.astimezone(zone).isoformat(), "minutes": minutes})
                remaining -= minutes
                last = max(last, stop)
                if left < start:
                    new_free.append((left, start))
                if stop < right:
                    new_free.append((stop, right))
            else:
                new_free.append((left, right))
        free = new_free
        if remaining:
            unresolved.append({"step_id": ready["id"], "title": ready["title"], "reason": f"窗口内仍缺 {remaining} 分钟"})
        else:
            finish_times[ready["id"]] = last
    event = _event(db, task)
    source_changed = (event.version if event else None) != route.source_version
    other = db.scalar(select(func.count()).select_from(Task).where(Task.user_id == route.user_id,
        Task.status == "open", Task.id != task.id)) or 0
    assumptions.append(f"另有 {other} 项未完成事务；其未写入日程的工作量尚未占用空闲窗口。")
    if source_changed:
        assumptions.append("通知要求已变化，请先同步路线并核对步骤。")
    if event and any(f"material:{material}" not in {step.get("source_key") for step in route.steps_json} for material in event.materials_json):
        unresolved.append({"step_id": "source_materials", "title": "材料要求", "reason": "部分来源材料未列入路线，请先核对清单"})
    status = "needs_estimate_or_handoff" if unresolved else "fits"
    if any("窗口内仍缺" in item["reason"] for item in unresolved):
        status = "beyond_horizon" if deadline and deadline > horizon else "insufficient"
    if deadline is None:
        status = "deadline_unknown"
    elif deadline <= now:
        status = "overdue"
    if source_changed:
        status = "source_changed"
    if task.status != "open":
        status = "task_closed"
    latest_start = None
    if status == "fits" and not booked and not unresolved:
        latest_start = _latest_start(route.steps_json, original_free, end)
    digest = {"version": route.version, "task_version": task.version, "source_version": event.version if event else None,
        "slots": slots, "availability": payload.availability.model_dump(), "status": status}
    return {"status": status, "slots": slots, "unresolved": unresolved, "required_minutes": required,
        "available_minutes": capacity, "already_booked_minutes": booked,
        "latest_start": latest_start.astimezone(zone).isoformat() if latest_start else None,
        "horizon_end": end.isoformat(), "assumptions": assumptions,
        "plan_hash": hashlib.sha256(json.dumps(digest, sort_keys=True).encode()).hexdigest()}


def _latest_start(steps, windows, end):
    """按依赖从截止倒排，给出当前假设下最晚开始的边界，不替代耗时确认。"""
    pending = {step["id"]: step for step in steps if step["status"] != "done"}
    latest = {}
    while pending:
        item = next((step for step in pending.values() if not any(step["id"] in other["depends_on"] for other in pending.values())), None)
        if item is None or item.get("estimate_minutes") is None:
            return None
        remaining = item["estimate_minutes"]
        bound = min([end, *(latest[other["id"]] for other in steps if item["id"] in other["depends_on"] and other["id"] in latest)])
        next_windows, first = [], None
        for a, b in reversed(windows):
            finish = min(b, bound)
            minutes = min(remaining, max(0, int((finish - a).total_seconds() // 60)))
            if minutes >= 5:
                start = finish - timedelta(minutes=minutes)
                remaining -= minutes
                first = start
                if a < start:
                    next_windows.append((a, start))
                if finish < b:
                    next_windows.append((finish, b))
            else:
                next_windows.append((a, b))
        if remaining or first is None:
            return None
        latest[item["id"]] = first
        pending.pop(item["id"])
        windows = sorted(next_windows)
    return min(latest.values()) if latest else None


def plan_journey(user_id, journey_id, payload, *, commit=False):
    with Session(_engine()) as db, db.begin():
        route = _access(db, user_id, journey_id, owner=True)
        result = _plan(db, route, payload)
        if not commit:
            return result
        if not payload.plan_hash or result["plan_hash"] != payload.plan_hash:
            raise ApiProblem("plan_changed", "时间或任务已变化，请重新试算后确认。", 409)
        if result["status"] in {"source_changed", "overdue", "deadline_unknown", "task_closed"} or result["unresolved"]:
            raise ApiProblem("plan_incomplete", "请先补齐耗时、截止和交接，确认完整安排后再写入。", 409)
        steps = deepcopy(route.steps_json)
        # 先争取路线版本，再用现有日程冲突检查；任一步失败整笔回滚。
        audit = _cas(db, route, payload.expected_version, user_id, "journey.schedule")
        for slot in result["slots"]:
            start = datetime.fromisoformat(slot["start_at"])
            entry, _ = timetable_service.create_schedule_entry_in_session(db, user_id,
                ScheduleEntryInput(title=f"办理 · {slot['title']}"[:500], kind="busy_block",
                    local_date=start.date(), start_at=start, end_at=datetime.fromisoformat(slot["end_at"]),
                    timezone=payload.availability.timezone))
            db.flush()
            next(step for step in steps if step["id"] == slot["step_id"]).setdefault("schedule_ids", []).append(entry.id)
        route.steps_json = steps
        audit.after_json = {"version": route.version, "steps": deepcopy(steps)}
        return {"journey": _public(db, route, user_id), "plan": result}


def opportunities(user_id):
    intent = get_intent(user_id)
    now = _now().astimezone(ZoneInfo(intent["availability"]["timezone"]))
    with Session(_engine()) as db:
        query = select(Event).where(Event.user_id == user_id, Event.status == "open")
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        events = db.scalars(query.order_by(Event.updated_at_epoch.desc(), Event.id).limit(300)).all()
        items = []
        for event in events:
            deadline = event.deadline_json or {}
            if deadline.get("utc") and datetime.fromisoformat(deadline["utc"].replace("Z", "+00:00")) <= now:
                continue
            if deadline.get("local_date") and deadline["local_date"] < now.date().isoformat():
                continue
            if event.time_range_json and datetime.fromisoformat(event.time_range_json["end"].replace("Z", "+00:00")) <= now:
                continue
            text = " ".join(filter(None, [event.title, event.scope, event.actor, event.stage]))
            matched = [word for word in intent["interests"] if word.casefold() in text.casefold()]
            if intent["interests"] and not matched:
                continue
            risks = list(event.unknowns_json or [])
            risks.append("请对照适用范围与本人资料核实报名资格。")
            if not deadline.get("local_date"):
                risks.append("截止日期不明确。")
            conflict_titles = []
            if event.time_range_json:
                interval = event.time_range_json
                for conflict in timetable_service._conflicts_for_interval(db, user_id=user_id,
                        start_at=datetime.fromisoformat(interval["start"].replace("Z", "+00:00")),
                        end_at=datetime.fromisoformat(interval["end"].replace("Z", "+00:00"))):
                    conflict_titles.append(conflict["title"])
            items.append({"event_id": event.id, "version": event.version, "title": event.title,
                "notice_id": event.notice_id, "matched_interests": matched, "scope": event.scope,
                "actor": event.actor, "stage": event.stage, "deadline": event.deadline_json,
                "materials": event.materials_json, "submission": event.submission,
                "risks": risks, "conflicts": conflict_titles, "eligibility_status": "needs_confirmation",
                "reason": f"与你填写的 { '、'.join(matched) } 相关" if matched else "尚未填写兴趣，按已入库事项浏览"})
        items.sort(key=lambda item: (-len(item["matched_interests"]), (item["deadline"] or {}).get("local_date") or "9999"))
        return {"items": items[:50], "scanned": len(events), "total_events": total,
            "scope_note": "仅匹配本人已入库的最近 300 个未关闭事项，不代表全校机会覆盖或报名资格认定。"}


def adopt_opportunity(user_id, event_id, expected):
    with Session(_engine()) as db, db.begin():
        # 账户行上的无值变化写锁将并发关注串行化，避免重复创建待办。
        db.execute(update(User).where(User.id == user_id).values(change_sequence=User.change_sequence))
        event = db.scalar(select(Event).where(Event.id == event_id, Event.user_id == user_id, Event.status == "open"))
        if event is None:
            raise ApiProblem("not_found", "未找到该机会事项。", 404)
        if event.version != expected:
            raise ApiProblem("version_conflict", "机会要求已变化，请刷新后再建立路线。", 409)
        task = db.scalar(select(Task).where(Task.event_id == event.id, Task.user_id == user_id, Task.status == "open"))
        if task is None:
            task, _ = cross_module_service.create_task_in_session(db, user_id=user_id,
                payload=TaskCreateInput(title=event.title, task_kind="activity", notice_id=event.notice_id,
                    deadline=DeadlineCandidate.model_validate(event.deadline_json) if event.deadline_json else None),
                actor_type="user", actor_id=user_id, source_document_id=event.source_document_id,
                source_event_id=event.id)
            db.flush()
        return _public(db, _create(db, task), user_id)
