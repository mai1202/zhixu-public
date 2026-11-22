"""把来源主张变成可审阅、可恢复的字段变更；不改变原始通知。"""

from copy import deepcopy
from datetime import date
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..models import (ChangeSet, Event, OperationLog, RequirementClaim,
    SourceDocument, SourceSegment, StudentJourney, Task)
from ..schemas.innovation import RequirementUpdateInput
from . import business_service, journey_service, operation_service, planning_guard
from .evidence_scope import check_deadline

FIELDS = {"deadline": "deadline_json", "time_range": "time_range_json", "submission": "submission",
    "cancelled": "status", "location": "identity_json", "material": "materials_json"}


def _engine():
    return current_app.extensions["notification_db_engine"]


def owned(db, model, identifier, user_id):
    """所有关联端点均按 owner 校验，不接受正文中的用户身份。"""
    return business_service._owned(db, model, str(identifier), user_id)


def public_claim(item):
    return {"id": item.id, "field": item.field_key, "value": item.value_json.get("value"),
        "basis_kind": item.value_json.get("basis_kind", "source"),
        "source_id": item.source_id, "evidence": item.evidence_json, "relation": item.relation,
        "supersedes_id": item.supersedes_id, "status": item.status, "event_version": item.event_version,
        "evidence_check": item.value_json.get("evidence_check")}


def get_requirements(user_id, event_id):
    """读取实际字段与原文证据，不再维护或计算核对状态。"""
    with Session(_engine()) as db:
        event = owned(db, Event, event_id, user_id)
        claims = db.scalars(select(RequirementClaim).where(RequirementClaim.user_id == user_id,
            RequirementClaim.event_id == event.id).order_by(RequirementClaim.created_at_epoch, RequirementClaim.id)).all()
        return {"event_id": event.id, "version": event.version, "identity": event.identity_json or {},
            "field_policies": event.field_policies_json or {}, "current": {"deadline": event.deadline_json,
            "time_range": event.time_range_json, "materials": event.materials_json, "submission": event.submission,
            "location": (event.identity_json or {}).get("location"), "cancelled": event.status == "cancelled"},
            "claims": [public_claim(item) for item in claims]}


def _candidates(db, user_id, identity):
    """标题只用于召回；主体、阶段和届次仍需要逐项比较。"""
    query = select(Event).where(Event.user_id == user_id, Event.archived_at_epoch.is_(None))
    if identity.get("activity"):
        query = query.where(Event.title.contains(identity["activity"], autoescape=True))
    result = []
    for event in db.scalars(query.order_by(Event.updated_at_epoch.desc()).limit(30)):
        known = {"activity": event.title, "actor": event.actor, "stage": event.stage, "scope": event.scope,
            **(event.identity_json or {})}
        if any(identity.get(key) and known.get(key) and identity[key] != known[key]
               for key in ("edition", "actor", "stage", "scope")):
            continue
        result.append({"id": event.id, "title": event.title, "version": event.version, "identity": known})
    return result


def _verify_evidence(db, user_id, source, claims):
    """精确定位只是硬校验，不能当作语义结论正确率。"""
    segments = {s.id: s.text for s in db.scalars(select(SourceSegment).where(
        SourceSegment.user_id == user_id, SourceSegment.source_document_id == source.id))}
    for claim in claims:
        if any(not ref["quote"].strip() or ref["quote"] not in segments.get(ref["segment_id"], "")
               for ref in claim["evidence"]):
            raise ApiProblem("invalid_evidence", "每个字段必须引用本次来源中的精确原文。", 422)


def _snapshot(db, user_id, event):
    """依赖对象版本与投影一起冻结，提交时检测人工修改和路线变化。"""
    tasks = db.scalars(select(Task).where(Task.user_id == user_id, Task.event_id == event.id)).all()
    routes = db.scalars(select(StudentJourney).where(StudentJourney.user_id == user_id,
        StudentJourney.task_id.in_([task.id for task in tasks]))).all()
    return {"event_version": event.version, "tasks": {task.id: task.version for task in tasks},
        "routes": {route.id: route.version for route in routes}}


def _issues(db, user_id, source, event, proposal):
    issues = []
    identity = proposal["identity"]
    known = {"activity": event.title, "actor": event.actor, "stage": event.stage, "scope": event.scope,
        **(event.identity_json or {})}
    if identity.get("activity") != known.get("activity") or (known.get("edition") and not identity.get("edition")):
        issues.append(("target_ambiguous", "活动名称或届次与目标不一致，未执行更新。"))
    if any(identity.get(k) and known.get(k) and known.get(k) != identity[k] for k in ("edition", "actor", "stage", "scope")):
        issues.append(("target_ambiguous", "主体、阶段或届次与目标不一致，未执行更新。"))
    if not all(identity.get(k) for k in ("activity", "actor", "stage")):
        issues.append(("target_ambiguous", "缺少活动、主体或阶段，无法确定更新目标。"))
    for claim in proposal["claims"]:
        field = claim["field"]
        policy = "materials" if field == "material" else field
        if (event.field_policies_json or {}).get(policy) is False:
            issues.append(("manual_override", "账户已关闭该字段跟随来源，保留现有字段。"))
        if claim["relation"] == "conflict":
            issues.append(("source_conflict", "来源要求有冲突，未覆盖现有字段。"))
        quote = " ".join(ref["quote"] for ref in claim["evidence"])
        if field == "deadline" and claim["value"]:
            value = claim["value"]
            from .main_assistant_service import _explicit_date
            if not value.get("local_date") or not _explicit_date(date.fromisoformat(value["local_date"]), source.original_text):
                issues.append(("incomplete_date", "日期缺少完整原文依据，未修改日期。"))
            elif check_deadline(value, claim["evidence"], identity)["status"] != "supported":
                # 全文命中仍可能引用了另一个阶段；仅用该字段引文进行局部日期校验。
                issues.append(("evidence_scope", "字段引文不能唯一支持此主体、阶段、日期或时刻，未修改期限。"))
            old_date = (event.deadline_json or {}).get("local_date")
            if old_date and value.get("local_date") and old_date[:4] != value["local_date"][:4] and not identity.get("edition"):
                issues.append(("target_ambiguous", "期限跨年度且届次缺失，无法确定是同一活动。"))
        if claim["relation"] in {"replace", "retract"} and not any(word in quote for word in
                ("延期", "延长", "推迟", "调整", "改为", "更正", "取消", "撤回", "不再", "替换", "新增", "增加", "变更")):
            issues.append(("revision_scope", "原文没有明确的替代或撤回依据，未覆盖现有字段。"))
        if claim["relation"] == "retract" and not any(word in quote for word in ("撤回", "取消", "不再", "无需")):
            issues.append(("revision_scope", "原文未明确撤回，不能仅凭延期措辞清空要求。"))
        if claim.get("supersedes_id"):
            previous = owned(db, RequirementClaim, claim["supersedes_id"], user_id)
            if previous.event_id != event.id or previous.field_key.split(":")[0] != field:
                raise ApiProblem("invalid_predecessor", "被替代主张不属于同一事项和字段。", 422)
            if previous.status not in {"active", "legacy"}:
                issues.append(("source_conflict", "本次修订引用过期字段记录，未覆盖最新要求。"))
        material_key = claim.get("material_id") or str(uuid5(NAMESPACE_URL, f"material:{event.id}:{claim['value']}"))
        active_key = field if field != "material" else f"material:{material_key}"
        latest = db.scalar(select(RequirementClaim).where(RequirementClaim.user_id == user_id,
            RequirementClaim.event_id == event.id, RequirementClaim.field_key == active_key,
            RequirementClaim.status == "active").limit(1))
        if latest and claim["relation"] not in {"duplicate", "supplement"} and str(claim.get("supersedes_id")) != latest.id:
            issues.append(("source_conflict", "修订未指向当前有效字段记录，未覆盖最新要求。"))
        if field != "material" and claim["relation"] == "supplement":
            old = (event.identity_json or {}).get("location") if field == "location" else event.status == "cancelled" if field == "cancelled" else getattr(event, FIELDS[field])
            if old is not None and old != claim["value"]:
                issues.append(("revision_scope", "该字段已有不同要求，补充不能直接当作替代。"))
        if field == "material" and claim["relation"] == "duplicate" and claim["value"] not in event.materials_json:
            issues.append(("source_conflict", "重复材料与当前清单不符，保留当前清单。"))
        if claim["relation"] == "duplicate" and field != "material":
            current = getattr(event, FIELDS[field])
            if field == "cancelled": current = event.status == "cancelled"
            if field == "location": current = (event.identity_json or {}).get("location")
            if current != claim["value"]:
                issues.append(("source_conflict", "重复通知包含不同字段值，未覆盖当前版本。"))
    return list(dict.fromkeys(issues))


def update_requirements(user_id, payload, *, automatic=False):
    """证据与身份校验成功即应用；存在冲突时返回原因，不保存待核对状态。"""
    proposal = payload.model_dump(mode="json")
    identifier = str(uuid5(NAMESPACE_URL, f"requirements:{user_id}:{payload.idempotency_key}"))
    with Session(_engine(), expire_on_commit=False) as db, db.begin():
        planning_guard.lock(db, user_id)
        previous = db.get(ChangeSet, identifier)
        if previous:
            if (previous.bundle_json or {}).get("request_hash") != planning_guard.digest(proposal):
                raise ApiProblem("idempotency_conflict", "同一提交标识不能用于不同提案。", 409)
            if previous.undone_at_epoch is not None:
                raise ApiProblem("change_undone", "本次操作已撤销；新的更新请使用新的提交标识。", 409)
            return _public_change(db, previous, user_id)
        source = owned(db, SourceDocument, payload.source_id, user_id)
        _verify_evidence(db, user_id, source, proposal["claims"])
        candidates = _candidates(db, user_id, proposal["identity"])
        event = owned(db, Event, payload.event_id, user_id) if payload.event_id else (
            db.get(Event, candidates[0]["id"]) if len(candidates) == 1 else None)
        if event and payload.expected_version and event.version != payload.expected_version:
            raise ApiProblem("version_conflict", "目标事项已变化，请重新读取。", 409)
        now = int(time.time())
        issues = _issues(db, user_id, source, event, proposal) if event else [("target_ambiguous", "未找到唯一匹配事项，缺少明确更新目标。")]
        if event and not payload.event_id and len(candidates) > 1:
            issues.append(("target_ambiguous", "存在多个身份相近事项，缺少明确更新目标。"))
        if issues:
            raise ApiProblem("requirement_update_conflict", "；".join(message for _, message in issues), 409,
                {"issues": [{"kind": kind, "reason": message} for kind, message in issues]})
        proposal.update(event_id=event.id, request_hash=planning_guard.digest(proposal), snapshot=_snapshot(db, user_id, event),
            impacts=[{"field": c["field"], "relation": c["relation"], "value": c["value"]} for c in proposal["claims"]])
        proposal["evidence_checks"] = [check_deadline(c["value"], c["evidence"], proposal["identity"])
            for c in proposal["claims"] if c["field"] == "deadline" and c["value"]]
        change = ChangeSet(id=identifier, user_id=user_id, source_document_id=source.id, kind="requirements",
            status=None, summary=payload.reason, bundle_json=proposal, operations_json=[], version=1,
            created_at_epoch=now, updated_at_epoch=now)
        db.add(change)
        db.flush()
        return _apply_in_session(db, user_id, change, automatic=automatic)


def _public_change(db, change, user_id):
    """返回写入回执与影响，不返回核对、通过与否等旧状态。"""
    return {**business_service.change_set_public(change), "proposal": deepcopy(change.bundle_json or {}),
        "noop": bool((change.bundle_json or {}).get("noop"))}


def get_change(user_id, identifier):
    with Session(_engine()) as db:
        change = owned(db, ChangeSet, identifier, user_id)
        if change.kind != "requirements": raise ApiProblem("not_found", "未找到要求提案。", 404)
        return _public_change(db, change, user_id)


def _save_before(entity, names):
    return {name: deepcopy(getattr(entity, name)) for name in names}


def _apply_in_session(db, user_id, change, *, automatic=False):
    """所有投影、路线、审计及后续任务在同一事务中提交，无人工确认中间态。"""
    proposal = deepcopy(change.bundle_json)
    event = owned(db, Event, proposal["event_id"], user_id)
    source = owned(db, SourceDocument, change.source_document_id, user_id)
    if automatic:
        from .main_assistant_service import get_preferences
        if not get_preferences(user_id)["automatic_notifications"]:
            raise ApiProblem("autopilot_disabled", "自动通知处理已关闭。", 409)
    if all(claim["relation"] == "duplicate" for claim in proposal["claims"]):
        # 同值重复只保存核验回执，不递增业务版本或再次同步路线、提醒和日程。
        proposal.update(noop=True, applied_event_version=event.version)
        change.bundle_json = proposal
        change.status = None
        change.version += 1
        change.confirmed_at_epoch = int(time.time())
        return _public_change(db, change, user_id)
    now = int(time.time())
    old_due = deepcopy(event.deadline_json)
    operations = [{"model": "event", "id": event.id, "before": _save_before(event,
        ["actor", "stage", "scope", "deadline_json", "time_range_json", "materials_json", "submission", "status", "identity_json", "evidence_json"])}]
    claims_before = {c.id: c.status for c in db.scalars(select(RequirementClaim).where(
        RequirementClaim.user_id == user_id, RequirementClaim.event_id == event.id))}
    material_renames = {}
    for claim in proposal["claims"]:
        key = claim["field"]
        if key == "material":
            # 改名明确指定旧材料 ID，材料步骤的稳定标识随之保留。
            material_id = claim.get("material_id") or str(uuid5(NAMESPACE_URL, f"material:{event.id}:{claim['value']}"))
            field_key = f"material:{material_id}"
        else:
            field_key = key
        current = db.scalar(select(RequirementClaim).where(RequirementClaim.user_id == user_id,
            RequirementClaim.event_id == event.id, RequirementClaim.field_key == field_key,
            RequirementClaim.status == "active").order_by(RequirementClaim.created_at_epoch.desc()).limit(1))
        if claim["relation"] == "duplicate":
            continue
        fingerprint = planning_guard.digest([change.id, field_key, claim])
        new_claim = RequirementClaim(id=str(uuid4()), user_id=user_id, event_id=event.id,
            source_id=source.id, change_set_id=change.id, field_key=field_key, value_json={"value": claim["value"],
                "basis_kind": "source",
                "evidence_check": check_deadline(claim["value"], claim["evidence"], proposal["identity"])
                    if key == "deadline" and claim["value"] else None},
            evidence_json=claim["evidence"], relation=claim["relation"], supersedes_id=current.id if current else None,
            status="retracted" if claim["relation"] == "retract" else "active", fingerprint=fingerprint,
            event_version=event.version + 1, created_at_epoch=now)
        if current: current.status = "superseded"
        db.add(new_claim)
        if key == "material":
            materials = list(event.materials_json)
            old_name = current.value_json.get("value") if current else claim["value"]
            if current and claim["relation"] == "replace" and old_name != claim["value"]:
                material_renames[f"material:{old_name}"] = f"material:{claim['value']}"
            if claim["relation"] in {"replace", "retract"} and old_name in materials: materials.remove(old_name)
            if claim["relation"] != "retract" and claim["value"] not in materials: materials.append(claim["value"])
            event.materials_json = materials
        elif key == "cancelled": event.status = "cancelled" if claim["value"] else "open"
        elif key == "location": event.identity_json = {**(event.identity_json or {}), "location": claim["value"]}
        else: setattr(event, FIELDS[key], None if claim["relation"] == "retract" else claim["value"])
        event.evidence_json = {**event.evidence_json, key: claim["evidence"]}
    event.identity_json = {**(event.identity_json or {}), **{k: v for k, v in proposal["identity"].items() if v}}
    for field in ("actor", "stage", "scope"):
        if not getattr(event, field) and proposal["identity"].get(field):
            setattr(event, field, proposal["identity"][field])
    event.version += 1
    event.updated_at_epoch = now
    operations[0]["applied_version"] = event.version
    # 跟随来源的未完成任务才更新期限，个人覆盖与已完成任务保持原值。
    for task in db.scalars(select(Task).where(Task.user_id == user_id, Task.event_id == event.id)):
        change_due = task.status == "open" and task.due_json == old_due and event.deadline_json != old_due
        cancel = task.status == "open" and event.status == "cancelled"
        if change_due or cancel:
            operations.append({"model": "task", "id": task.id, "before": _save_before(task, ["due_json", "due_at_epoch", "status"])})
            if change_due:
                task.due_json = deepcopy(event.deadline_json)
                task.due_at_epoch = business_service.deadline_sort_epoch(event.deadline_json)
            if cancel: task.status = "cancelled"
            task.version += 1
            task.updated_at_epoch = now
            operations[-1]["applied_version"] = task.version
        for route in db.scalars(select(StudentJourney).where(StudentJourney.user_id == user_id, StudentJourney.task_id == task.id)):
            before = _save_before(route, ["steps_json", "source_version"])
            steps = deepcopy(route.steps_json)
            generated = journey_service._generated_steps(task, event)
            keys = {step["source_key"] for step in generated}
            for step in steps:
                if step.get("source_key") in material_renames:
                    # 只更新来源键，保留步骤 ID、人工标题、完成状态和已有预约。
                    step["source_key"] = material_renames[step["source_key"]]
                if step.get("source_key", "").startswith("material:"):
                    step["source_removed"] = step["source_key"] not in keys
                if event.status == "cancelled" and step["status"] != "done": step["source_removed"] = True
            existing_keys = {step.get("source_key") for step in steps}
            steps.extend(step for step in generated if step["source_key"] not in existing_keys)
            if len(steps) > 40: raise ApiProblem("too_many_steps", "同步后超过40步，请核对清单。", 409)
            route.steps_json = steps
            route.source_version = event.version
            route.version += 1
            route.updated_at_epoch = now
            operations.append({"model": "journey", "id": route.id, "before": before, "applied_version": route.version})
    proposal["claim_status_before"] = claims_before
    proposal["applied_event_version"] = event.version
    change.bundle_json = proposal
    change.operations_json = operations
    change.status = None
    change.version += 1
    change.confirmed_at_epoch = now
    change.updated_at_epoch = now
    change.change_seq = business_service._next_change_seq(db, user_id)
    operation_service.record_operation(db, user_id=user_id, actor_type="agent" if automatic else "user",
        actor_id=user_id, action="requirements.apply", object_type="event", object_id=event.id,
        change_set_id=change.id, source_document_id=source.id, summary=change.summary,
        before=operations[0]["before"], after={"version": event.version, "impacts": proposal["impacts"]}, undoable=True)
    planning_guard.enqueue(db, user_id, "requirements.reminders", {"event_id": event.id}, f"requirements-reminders:{change.id}")
    db.flush()
    return _public_change(db, change, user_id)


def field_policy(user_id, event_id, payload):
    with Session(_engine()) as db, db.begin():
        planning_guard.lock(db, user_id)
        event = owned(db, Event, event_id, user_id)
        if event.version != payload.expected_version: raise ApiProblem("version_conflict", "事项已变化。", 409)
        event.field_policies_json = {**event.field_policies_json, payload.field: payload.follow_source}
        event.version += 1
    return get_requirements(user_id, event_id)


def undo_in_session(db, change, user_id):
    """新类型撤销复用现有入口；任一后续版本冲突整笔撤销失败。"""
    planning_guard.lock(db, user_id)
    models = {"event": Event, "task": Task, "journey": StudentJourney}
    from ..models import ScheduleEntry
    models["schedule"] = ScheduleEntry
    for op in reversed(change.operations_json):
        obj = owned(db, models[op["model"]], op["id"], user_id)
        if obj.version != op["applied_version"]:
            raise ApiProblem("version_conflict", "对象有后续修改，不能自动撤销。", 409)
        if op.get("created"):
            obj.status = "archived"
        else:
            for key, value in op["before"].items(): setattr(obj, key, deepcopy(value))
        obj.version += 1
        obj.updated_at_epoch = int(time.time())
    if change.kind == "requirements":
        for claim in db.scalars(select(RequirementClaim).where(RequirementClaim.user_id == user_id,
                RequirementClaim.event_id == change.bundle_json["event_id"])):
            if claim.change_set_id == change.id: claim.status = "undone"
            elif claim.id in change.bundle_json["claim_status_before"]:
                claim.status = change.bundle_json["claim_status_before"][claim.id]
    change.status = None if business_service.is_notification_change(change) else "undone"
    change.version += 1
    change.undone_at_epoch = int(time.time())
    operation_service.record_operation(db, user_id=user_id, actor_type="user", actor_id=user_id,
        action=f"{change.kind}.undo", object_type="change_set", object_id=change.id,
        change_set_id=change.id, summary="撤销要求或安排变更，已核对对象版本。")
    return business_service.change_set_public(change)
