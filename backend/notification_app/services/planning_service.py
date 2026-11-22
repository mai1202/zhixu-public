"""有限范围内的多事务列表排程；不把启发式失败宣称为必然无解。"""

from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
import time as clock
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..models import (ChangeSet, CourseOccurrence, Event, PlanningRun, ScheduleEntry,
    StudentJourney, Task, UserPlanningState)
from ..schemas.journeys import AvailabilityInput
from ..schemas.timetable import ScheduleEntryInput
from ..text_sanitize import clean_title
from . import business_service, journey_service, operation_service, planning_guard, timetable_service
from .requirement_service import owned
from .planning_diagnostics import available_minutes, capacity_certificates

UTC = timezone.utc


def _engine():
    return current_app.extensions["notification_db_engine"]


def _now():
    return datetime.now(UTC)


def _dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def preferences(user_id):
    with Session(_engine()) as db, db.begin():
        state = planning_guard.lock(db, user_id)
        return {"version": state.policy_version, "automatic": state.automatic,
            "journey_ids": state.journey_ids_json or [],
            "availability": state.availability_json or AvailabilityInput().model_dump(),
            "automatic_available": bool(current_app.config.get("PLANNING_AUTOMATIC_VERIFIED", False)),
            "freeze_minutes": 120}


def save_preferences(user_id, payload):
    with Session(_engine()) as db, db.begin():
        state = planning_guard.lock(db, user_id)
        if state.policy_version != payload.expected_version:
            raise ApiProblem("version_conflict", "规划设置已变化。", 409)
        state.automatic = payload.automatic
        for identifier in payload.journey_ids: owned(db, StudentJourney, identifier, user_id)
        state.journey_ids_json = list(dict.fromkeys(str(identifier) for identifier in payload.journey_ids))
        state.availability_json = payload.availability.model_dump()
        state.policy_version += 1
        state.version += 1
        if state.automatic:
            planning_guard.enqueue(db, user_id, "planning.replan", {}, f"planning-policy:{state.policy_version}", delay=60)
    return preferences(user_id)


def set_policy(user_id, entry_id, payload):
    with Session(_engine(), expire_on_commit=False) as db, db.begin():
        planning_guard.lock(db, user_id)
        entry = owned(db, ScheduleEntry, entry_id, user_id)
        if entry.version != payload.expected_version or entry.status != "active":
            raise ApiProblem("version_conflict", "安排已变化。", 409)
        if entry.kind != "busy_block" or not entry.start_at or not entry.end_at:
            raise ApiProblem("not_movable", "只有具备明确起止时刻的个人安排可纳入重排。", 422)
        first = payload.earliest_date.isoformat() if payload.earliest_date else entry.local_date
        last = payload.latest_date.isoformat() if payload.latest_date else entry.local_date
        if first > last: raise ApiProblem("invalid_range", "允许移动范围倒置。", 422)
        before = deepcopy(entry.planning_policy_json)
        entry.planning_policy_json = {"movable": payload.movable, "locked": payload.locked,
            "earliest_date": first, "latest_date": last, "origin": before.get("origin", "manual")}
        entry.version += 1
        operation_service.record_operation(db, user_id=user_id, actor_type="user", actor_id=user_id,
            action="planning.policy", object_type="schedule_entry", object_id=entry.id,
            summary="用户明确设置安排的移动许可与日期范围。", before=before, after=entry.planning_policy_json)
        return {"id": entry.id, "version": entry.version, "policy": entry.planning_policy_json}


def _movable(entry, now):
    policy = entry.planning_policy_json or {}
    return bool(entry.kind == "busy_block" and entry.start_at and entry.end_at and policy.get("movable")
        and not policy.get("locked") and _dt(entry.start_at) > now + timedelta(hours=2))


def _fingerprint(db, user_id, state):
    """输入包含全体相关版本，避免只选中的路线通过而遗漏旁路修改。"""
    data = {"policy": state.policy_version, "revision": state.version}
    data["requirements"] = sorted((change.id, change.version, change.status) for change in db.scalars(
        select(ChangeSet).where(ChangeSet.user_id == user_id, ChangeSet.kind == "requirements")))
    for label, model in (("events", Event), ("tasks", Task), ("routes", StudentJourney),
                         ("calendar", ScheduleEntry), ("courses", CourseOccurrence)):
        data[label] = sorted((obj.id, obj.version) for obj in db.scalars(select(model).where(model.user_id == user_id)))
    return planning_guard.digest(data)


def _subtract(windows, start, end):
    result = []
    for left, right in windows:
        if end <= left or start >= right: result.append((left, right))
        else:
            if left < start: result.append((left, start))
            if end < right: result.append((end, right))
    return result


def _place(windows, earliest, deadline, minutes, split):
    """仅占用符合约束的窗口；放不下时整项回退，不消费其他事务容量。"""
    remaining, slots, free = minutes, [], list(windows)
    minimum = min(15, minutes)
    for left, right in windows:
        start, stop = max(left, earliest), min(right, deadline)
        capacity = max(0, int((stop - start).total_seconds() // 60))
        take = min(capacity, remaining)
        if take < minimum or (not split and take < remaining): continue
        end = start + timedelta(minutes=take)
        slots.append((start, end))
        free = _subtract(free, start, end)
        remaining -= take
        if remaining == 0: return slots, free
    return None, windows


def schedule_candidates(units, windows, done, unresolved, movable, managed_ids):
    """比较四种确定性策略；纯试算入口也供冻结样本实验直接调用。"""
    profiles, certificates = capacity_certificates(units, windows, done)
    candidates = []
    for strategy in ("deadline", "slack", "priority", "dependency_pressure"):
        pending, free, finish, slots, blocked = deepcopy(units), list(windows), dict(done), [], list(unresolved)
        while pending:
            ready = [unit for unit in pending if all(parent in finish for parent in unit["depends_on"])]
            if not ready:
                blocked.extend({"id": unit["id"], "route_id": unit.get("route_id"), "reason": "前置步骤尚未完成或未安排。"} for unit in pending)
                break
            def order(unit):
                due = unit["deadline"].timestamp()
                if strategy == "dependency_pressure":
                    # 先看后继最紧截止回传后的真实空闲余量，再用关键路径打破平局。
                    earliest = max([unit["earliest"], *(finish[parent] for parent in unit["depends_on"])])
                    limit = profiles[unit["id"]]["latest_start"] + timedelta(minutes=unit["minutes"])
                    return (available_minutes(free, earliest, limit) - unit["minutes"],
                        -profiles[unit["id"]]["path_minutes"], unit["priority"], unit["id"])
                return ((unit["priority"], due) if strategy == "priority" else
                    (due - unit["minutes"] * 60, unit["priority"]) if strategy == "slack" else
                    (due, unit["priority"])) + (unit["id"],)
            unit = min(ready, key=order)
            pending.remove(unit)
            earliest = max([unit["earliest"], *(finish[parent] for parent in unit["depends_on"])])
            placed = None
            if unit.get("old_slots"):
                old = unit["old_slots"]
                if sum(int((right - left).total_seconds() // 60) for left, right, _ in old) == unit["minutes"] and all(
                    left >= earliest and right <= unit["deadline"] and any(a <= left and right <= b for a, b in free) for left, right, _ in old):
                    placed, next_free = [(left, right) for left, right, _ in old], list(free)
                    for left, right in placed: next_free = _subtract(next_free, left, right)
            # 优先保留原位，只在原位不再有效时寻找新窗口。
            if unit.get("old"):
                left, right = unit["old"]
                if left >= earliest and right <= unit["deadline"] and any(a <= left and right <= b for a, b in free):
                    placed, next_free = [(left, right)], _subtract(free, left, right)
            if placed is None: placed, next_free = _place(free, earliest, unit["deadline"], unit["minutes"], unit["split"])
            if not placed:
                blocked.append({"id": unit["id"], "route_id": unit.get("route_id"), "reason": "当前策略未找到完整方案；不代表截止前必然无解。"})
                continue
            free = next_free
            finish[unit["id"]] = placed[-1][1]
            for index, (left, right) in enumerate(placed):
                slots.append({"id": unit["id"], "route_id": unit.get("route_id"), "entry_id": unit.get("entry_id"),
                    "title": unit["title"], "start_at": left.isoformat(), "end_at": right.isoformat()})
                if unit.get("old_slots") and index < len(unit["old_slots"]):
                    slots[-1]["entry_id"] = unit["old_slots"][index][2]
        moved = [slot for slot in slots if slot.get("entry_id") and (_dt(slot["start_at"]), _dt(slot["end_at"])) !=
            (_dt(movable[slot["entry_id"]].start_at), _dt(movable[slot["entry_id"]].end_at))]
        shift = sum(abs(int((_dt(slot["start_at"]) - _dt(movable[slot["entry_id"]].start_at)).total_seconds() // 60)) for slot in moved)
        candidates.append({"strategy": strategy, "slots": slots, "unresolved": blocked,
            "moved_count": len(moved), "moved_minutes": shift,
            "release_entry_ids": sorted((set(movable) & managed_ids) - {slot["entry_id"] for slot in slots if slot.get("entry_id")})})
    best = min(candidates, key=lambda c: (len(c["unresolved"]), c["moved_count"], c["moved_minutes"]))
    # 全部候选复用同一目标函数，新增策略失败时仍保留现有策略的更好方案。
    best["comparison"] = [{"strategy": c["strategy"], "unresolved_count": len(c["unresolved"]),
        "moved_count": c["moved_count"], "moved_minutes": c["moved_minutes"]} for c in candidates]
    best["diagnostics"] = {"method": "dependency-capacity-v1", "certificates": certificates,
        "units": [{"id": unit["id"], "title": unit["title"],
            "critical_path_minutes": profiles[unit["id"]]["path_minutes"],
            "latest_start_bound": profiles[unit["id"]]["latest_start"].isoformat()} for unit in units],
        "note": "容量不足是当前输入下的必要条件证据；未发现不足及策略失败均不能证明存在完整解。"}
    return best


def preview(user_id, payload, *, automatic=False):
    with Session(_engine(), expire_on_commit=False) as db, db.begin():
        state = planning_guard.lock(db, user_id)
        availability = payload.availability or AvailabilityInput.model_validate(state.availability_json or {})
        now = _now()
        now = datetime.fromtimestamp(((int(now.timestamp()) + 299) // 300) * 300, UTC)
        end = now + timedelta(days=14)
        routes = [owned(db, StudentJourney, identifier, user_id) for identifier in payload.journey_ids]
        if len({route.id for route in routes}) != len(routes): raise ApiProblem("duplicate_selection", "办理路线重复。", 422)
        entries = db.scalars(select(ScheduleEntry).where(ScheduleEntry.user_id == user_id, ScheduleEntry.status == "active")).all()
        requested_entries = {str(identifier) for identifier in payload.entry_ids}
        for identifier in requested_entries: owned(db, ScheduleEntry, identifier, user_id)
        movable = {entry.id: entry for entry in entries if entry.id in requested_entries and _movable(entry, now)}
        unresolved = [{"id": identifier, "reason": "安排未授权、已锁定或进入未来两小时保护窗口。"}
            for identifier in requested_entries - set(movable)]
        windows = journey_service._free_intervals(db, user_id, availability, now, end)
        # 重新从可用窗口扣除固定占用；被明确选中的可移动安排才释放旧位置。
        zone = ZoneInfo(availability.timezone)
        if movable:
            windows = []
            day = now.astimezone(zone).date()
            for offset in range(15):
                date = day + timedelta(days=offset)
                if date.isoweekday() not in availability.weekdays: continue
                left = datetime.combine(date, time.fromisoformat(availability.start_time), zone).astimezone(UTC)
                right = datetime.combine(date, time.fromisoformat(availability.end_time), zone).astimezone(UTC)
                # DST 不存在的本地时间必须由现有空闲计算器验证。
                if right > now and left < end: windows.append((max(left, now), min(right, end)))
            for block in timetable_service._busy_intervals_for_range(db, user_id, now, end):
                if block["object_id"] not in movable:
                    windows = _subtract(windows, block["start_at"].astimezone(UTC), block["end_at"].astimezone(UTC))
            for entry in entries:
                if entry.id in movable: continue
                if entry.start_at and entry.end_at: windows = _subtract(windows, _dt(entry.start_at), _dt(entry.end_at))
                elif entry.kind == "all_day":
                    left = datetime.combine(datetime.fromisoformat(entry.local_date).date(), time.min, ZoneInfo(entry.timezone)).astimezone(UTC)
                    next_day = datetime.combine(datetime.fromisoformat(entry.local_date).date() + timedelta(days=1), time.min, ZoneInfo(entry.timezone)).astimezone(UTC)
                    windows = _subtract(windows, left, next_day)
        units, done, route_ids = [], {}, []
        disputed_ids = {(change.bundle_json or {}).get("event_id") for change in db.scalars(select(ChangeSet).where(
            ChangeSet.user_id == user_id, ChangeSet.kind == "requirements", ChangeSet.status == "proposed"))}
        managed_ids = {identifier for route in routes for step in route.steps_json for identifier in step.get("schedule_ids", [])}
        count = sum(sum(step["status"] != "done" for step in route.steps_json) for route in routes)
        if count > 100: raise ApiProblem("planning_limit", "单次最多100个未完成步骤，请缩小选择。", 422)
        for route in routes:
            route_ids.append(route.id)
            task = owned(db, Task, route.task_id, user_id)
            event = journey_service._event(db, task)
            route_blocked = task.status != "open" or (event and (route.source_version != event.version
                or event.status != "open" or event.id in disputed_ids))
            due = task.due_json or {}
            deadline = _dt(due["utc"]) if due.get("utc") else (
                datetime.combine(datetime.fromisoformat(due["local_date"]).date(), time(23, 59), zone).astimezone(UTC) if due.get("local_date") else None)
            for step in route.steps_json:
                if step["status"] == "done": done[step["id"]] = now; continue
                if route_blocked or step.get("source_removed") or not deadline or step.get("estimate_minutes") is None or step.get("assigned_user_id") != user_id:
                    unresolved.append({"id": step["id"], "route_id": route.id, "reason": "请核对来源、截止、耗时、撤回要求或同伴交接。"})
                    continue
                reservations = [entry for entry in entries if entry.id in step.get("schedule_ids", []) and entry.start_at and entry.end_at and entry.id not in movable and _dt(entry.start_at) >= now and _dt(entry.end_at) <= deadline]
                reserved = sum(int((_dt(e.end_at) - _dt(e.start_at)).total_seconds() // 60) for e in reservations)
                remaining = max(0, step["estimate_minutes"] - reserved)
                earliest = max([now, *(_dt(e.end_at) for e in reservations)])
                movable_bookings = [entry for entry in entries if entry.id in step.get("schedule_ids", []) and entry.id in movable]
                if remaining == 0: done[step["id"]] = earliest; continue
                if automatic: earliest = max(earliest, now + timedelta(hours=2))
                units.append({"id": step["id"], "route_id": route.id, "task_id": task.id,
                    "title": clean_title(step["title"], "待办步骤"), "minutes": remaining, "earliest": earliest, "deadline": min(deadline, end),
                    "depends_on": step["depends_on"], "priority": {"high": 0, "normal": 1, "low": 2}.get(task.priority, 1), "split": True,
                    "old_slots": sorted([(_dt(entry.start_at), _dt(entry.end_at), entry.id) for entry in movable_bookings])})
        for entry in movable.values():
            if entry.id in managed_ids: continue
            policy = entry.planning_policy_json
            earliest = datetime.combine(datetime.fromisoformat(policy["earliest_date"]).date(), time.min, ZoneInfo(entry.timezone)).astimezone(UTC)
            latest = datetime.combine(datetime.fromisoformat(policy["latest_date"]).date() + timedelta(days=1), time.min, ZoneInfo(entry.timezone)).astimezone(UTC)
            units.append({"id": entry.id, "entry_id": entry.id, "title": entry.title,
                "minutes": int((_dt(entry.end_at) - _dt(entry.start_at)).total_seconds() // 60),
                "earliest": max(now + timedelta(hours=2), earliest), "deadline": min(end, latest),
                "depends_on": [], "priority": 1, "split": False, "old": (_dt(entry.start_at), _dt(entry.end_at))})
        best = schedule_candidates(units, windows, done, unresolved, movable, managed_ids)
        best.update(status="fits" if not best["unresolved"] else "needs_review", horizon_end=end.isoformat(),
            coverage={"routes": len(routes), "pending_steps": count},
            assumptions=["仅试算未来14天；日期截止暂按所选时区23:59计算。", "尚未选择的事务工作量未纳入；启发式不保证全局最优。"])
        run = PlanningRun(id=str(uuid4()), user_id=user_id, input_json={"request": payload.model_dump(mode="json"),
            "policy_version": state.policy_version, "automatic": state.automatic}, result_json=best,
            input_hash=_fingerprint(db, user_id, state), status="proposed", created_at_epoch=int(clock.time()))
        db.add(run)
        return _public(run)


def _public(run):
    return {"id": run.id, "status": run.status, "input_hash": run.input_hash,
        "result": run.result_json, "change_set_id": run.change_set_id}


def get_run(user_id, run_id):
    with Session(_engine()) as db: return _public(owned(db, PlanningRun, run_id, user_id))


def apply(user_id, run_id, payload, *, automatic=False):
    """结果与写入回执同事务保存；先核对版本和授权，再修改任何安排。"""
    with Session(_engine(), expire_on_commit=False) as db, db.begin():
        state = planning_guard.lock(db, user_id)
        run = owned(db, PlanningRun, run_id, user_id)
        if run.input_hash != payload.input_hash: raise ApiProblem("plan_changed", "方案指纹不一致。", 409)
        if run.status == "applied":
            result = _public(run)
            result["previously_applied"] = True
            change = owned(db, ChangeSet, run.change_set_id, user_id)
            result["state_changed_since_apply"] = change.status != "confirmed" or any(
                owned(db, ScheduleEntry if op["model"] == "schedule" else StudentJourney, op["id"], user_id).version != op["applied_version"] for op in change.operations_json)
            return result
        if _fingerprint(db, user_id, state) != run.input_hash:
            raise ApiProblem("plan_changed", "规划数据已变化，请重新试算。", 409)
        if automatic and (not state.automatic or state.policy_version != run.input_json["policy_version"]
                or not current_app.config.get("PLANNING_AUTOMATIC_VERIFIED", False)):
            raise ApiProblem("automatic_not_authorized", "自动授权已关闭或目标数据库尚未验收。", 409)
        unresolved = run.result_json["unresolved"]
        if unresolved and (automatic or not payload.allow_partial):
            raise ApiProblem("plan_incomplete", "方案不完整，请先核对，原安排保持不变。", 409)
        blocked_routes = {item.get("route_id") for item in unresolved}
        slots = [slot for slot in run.result_json["slots"] if slot.get("route_id") not in blocked_routes or not slot.get("route_id")]
        now = _now()
        for slot in slots:
            if _dt(slot["start_at"]) < now: raise ApiProblem("plan_expired", "方案包含过去时段，请重算。", 409)
            if slot.get("entry_id") and not _movable(owned(db, ScheduleEntry, slot["entry_id"], user_id), now):
                raise ApiProblem("protected_entry", "安排已进入保护窗口或许可已变化。", 409)
        db.info["planning_applying"] = True
        operations, route_steps = [], {}
        release_ids = run.result_json.get("release_entry_ids", []) if not unresolved else []
        for identifier in release_ids:
            entry = owned(db, ScheduleEntry, identifier, user_id)
            if not _movable(entry, now): raise ApiProblem("protected_entry", "待释放安排已进入保护窗口。", 409)
            operations.append({"model": "schedule", "id": entry.id, "before": {"status": entry.status}, "applied_version": entry.version + 1})
            entry.status = "archived"
            entry.version += 1
        # 移除旧预约关联必须和新日程在同一事务，步骤完成进度保持不变。
        for identifier in run.input_json["request"].get("journey_ids", []):
            route = owned(db, StudentJourney, identifier, user_id)
            if any(slot.get("route_id") == route.id for slot in slots) or any(id in release_ids for step in route.steps_json for id in step.get("schedule_ids", [])):
                operations.append({"model": "journey", "id": route.id, "before": {"steps_json": deepcopy(route.steps_json)}})
                route_steps[route.id] = deepcopy(route.steps_json)
                for step in route_steps[route.id]: step["schedule_ids"] = [id for id in step.get("schedule_ids", []) if id not in release_ids]
        # 所有移动对象先在当前事务内释放，再逐项复用冲突校验；失败会整体回滚。
        for slot in slots:
            if slot.get("entry_id"):
                entry = owned(db, ScheduleEntry, slot["entry_id"], user_id)
                operations.append({"model": "schedule", "id": entry.id, "before": {name: getattr(entry, name)
                    for name in ("start_at", "end_at", "local_date", "status")}})
                entry.status = "planning"
        db.flush()
        for slot in slots:
            start, end = _dt(slot["start_at"]), _dt(slot["end_at"])
            zone = (run.input_json["request"].get("availability") or state.availability_json or {"timezone": "Asia/Shanghai"})["timezone"]
            if slot.get("entry_id"):
                entry = owned(db, ScheduleEntry, slot["entry_id"], user_id)
                conflicts = timetable_service._conflicts_for_interval(db, user_id=user_id, start_at=start, end_at=end, exclude_entry_id=entry.id)
                if conflicts: raise ApiProblem("schedule_conflict", "写入时发现新冲突，请重新试算。", 409)
                entry.start_at, entry.end_at = start.isoformat(), end.isoformat()
                entry.local_date = start.astimezone(ZoneInfo(entry.timezone)).date().isoformat()
                entry.status = "active"
                entry.version += 1
                next(op for op in operations if op["id"] == entry.id)["applied_version"] = entry.version
            else:
                route = owned(db, StudentJourney, slot["route_id"], user_id)
                if route.id not in route_steps:
                    operations.append({"model": "journey", "id": route.id, "before": {"steps_json": deepcopy(route.steps_json)}})
                    route_steps[route.id] = deepcopy(route.steps_json)
                entry, _ = timetable_service.create_schedule_entry_in_session(db, user_id,
                    ScheduleEntryInput(title=f"办理 · {slot['title']}"[:500], kind="busy_block",
                        local_date=start.astimezone(ZoneInfo(zone)).date(), start_at=start, end_at=end, timezone=zone))
                enrolled = route.id in (state.journey_ids_json or [])
                entry.planning_policy_json = {"origin": "journey", "movable": enrolled, "locked": False,
                    "earliest_date": start.astimezone(ZoneInfo(zone)).date().isoformat(),
                    "latest_date": _dt(run.result_json["horizon_end"]).astimezone(ZoneInfo(zone)).date().isoformat()}
                db.flush()
                next(step for step in route_steps[route.id] if step["id"] == slot["id"]).setdefault("schedule_ids", []).append(entry.id)
                operations.append({"model": "schedule", "id": entry.id, "created": True, "applied_version": entry.version})
            db.flush()
        for route_id, steps in route_steps.items():
            route = owned(db, StudentJourney, route_id, user_id)
            route.steps_json = steps
            route.version += 1
            next(op for op in operations if op["id"] == route.id)["applied_version"] = route.version
        now_epoch = int(clock.time())
        change = ChangeSet(id=str(uuid4()), user_id=user_id, kind="planning", status="confirmed",
            summary=f"统一安排，移动{run.result_json['moved_count']}项，累计{run.result_json['moved_minutes']}分钟。",
            bundle_json={"run_id": run.id}, operations_json=operations, version=1,
            created_at_epoch=now_epoch, updated_at_epoch=now_epoch, confirmed_at_epoch=now_epoch)
        db.add(change)
        db.flush()
        run.status, run.change_set_id, run.applied_at_epoch = "applied", change.id, now_epoch
        operation_service.record_operation(db, user_id=user_id, actor_type="system" if automatic else "user",
            actor_id=user_id, action="planning.apply", object_type="planning_run", object_id=run.id,
            change_set_id=change.id, summary=change.summary, after=run.result_json, undoable=True)
        return _public(run)


def handle_replan(claim):
    """队列唤醒只按已存授权运行，不开放背景 Agent 任意日历写入工具。"""
    from ..schemas.innovation import PlanningApplyInput, PlanningPreviewInput
    pref = preferences(claim.user_id)
    if not pref["automatic"]: return {"status": "disabled"}
    if not pref["automatic_available"]: return {"status": "awaiting_database_acceptance"}
    with Session(_engine()) as db:
        entries = [entry.id for entry in db.scalars(select(ScheduleEntry).where(
            ScheduleEntry.user_id == claim.user_id, ScheduleEntry.status == "active")) if _movable(entry, _now())]
    if not entries and not pref["journey_ids"]: return {"status": "noop"}
    run = preview(claim.user_id, PlanningPreviewInput(entry_ids=entries[:100], journey_ids=pref["journey_ids"]), automatic=True)
    if not run["result"]["unresolved"] and (not run["result"]["slots"] or (
            run["result"]["moved_count"] == 0 and not run["result"].get("release_entry_ids")
            and all(slot.get("entry_id") for slot in run["result"]["slots"]))):
        return {"status": "noop", "run_id": run["id"]}
    if run["result"]["unresolved"]:
        # 直接记录无法安排的约束，不创建通知核对提案或问题队列。
        return {"status": "blocked", "run_id": run["id"], "unresolved": run["result"]["unresolved"]}
    return apply(claim.user_id, run["id"], PlanningApplyInput(input_hash=run["input_hash"]), automatic=True)
