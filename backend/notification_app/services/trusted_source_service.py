"""在既有协作空间内复用一次解析结果，持续信任与每次接收均校验成员资格。"""
import time
from uuid import uuid4
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from ..errors import ApiProblem
from ..models import Event, JourneyPublication, JourneySourceTrust, Task
from ..schemas.business import CreateSourceInput, ManualEventInput
from . import business_service, journey_service as journey, planning_guard


def _preview(db, route):
    task = db.get(Task, route.task_id)
    event = db.get(Event, task.event_id) if task and task.event_id else None
    if event is None or event.user_id != route.user_id:
        raise ApiProblem("source_event_required", "该事务没有可分享的结构化通知要求。", 422)
    if not route.shared_title:
        raise ApiProblem("shared_title_required", "请先生成邀请并填写共享标题。", 422)
    return ManualEventInput(title=route.shared_title, actor=event.actor, stage=event.stage, scope=event.scope,
        structured_deadline=event.deadline_json, materials=event.materials_json or [], submission=event.submission).model_dump(mode="json")


def state(user_id, journey_id):
    with Session(journey._engine()) as db:
        route = journey._access(db, user_id, journey_id)
        trust = db.scalar(select(JourneySourceTrust).where(JourneySourceTrust.journey_id == route.id, JourneySourceTrust.user_id == user_id))
        rows = db.scalars(select(JourneyPublication).where(JourneyPublication.journey_id == route.id).order_by(JourneyPublication.created_at_epoch.desc(), JourneyPublication.id).limit(50)).all()
        preview = None
        if route.user_id == user_id:
            try: preview = _preview(db, route)
            except ApiProblem: pass
        return {"trusted": bool(trust and trust.enabled), "preview": preview, "preview_hash": planning_guard.digest(preview) if preview else None,
                "items": [{"id": row.id, "event": row.event_json, "created_at_epoch": row.created_at_epoch} for row in rows]}


def set_trust(user_id, journey_id, enabled):
    with Session(journey._engine()) as db, db.begin():
        route = journey._access(db, user_id, journey_id)
        # 同一路线下的授权变更串行化，避免重复授权产生多条记录。
        # 使用既有事务写锁入口，随后再读取当前授权。
        planning_guard.lock(db, route.user_id)
        journey._access(db, user_id, journey_id)
        item = db.scalar(select(JourneySourceTrust).where(JourneySourceTrust.journey_id == journey_id, JourneySourceTrust.user_id == user_id))
        if item is None: db.add(JourneySourceTrust(id=str(uuid4()), user_id=user_id, journey_id=journey_id, enabled=enabled))
        else: item.enabled = enabled
    return state(user_id, journey_id)


def publish(user_id, journey_id, expected, expected_snapshot_hash):
    with Session(journey._engine()) as db, db.begin():
        route = journey._access(db, user_id, journey_id, owner=True)
        snapshot = _preview(db, route)
        if planning_guard.digest(snapshot) != expected_snapshot_hash:
            raise ApiProblem("publication_changed", "分享字段已发生变化，请刷新后重新核对。", 409)
        journey._cas(db, route, expected, user_id, "journey.publish")
        item = JourneyPublication(id=str(uuid4()), user_id=user_id, journey_id=route.id, event_json=snapshot, created_at_epoch=int(time.time()))
        db.add(item)
        # 发布与投递工作同事务保存，重启后仍能继续分发。
        planning_guard.enqueue(db, user_id, "journey.distribute", {"publication_id": item.id}, item.id)
    return state(user_id, journey_id)


def adopt(user_id, journey_id, publication_id, *, require_trust=False):
    with Session(journey._engine()) as db:
        journey._access(db, user_id, journey_id)
        if require_trust:
            consent = db.scalar(select(JourneySourceTrust).where(JourneySourceTrust.journey_id == journey_id,
                JourneySourceTrust.user_id == user_id, JourneySourceTrust.enabled.is_(True)))
            if consent is None: return {"skipped": "trust_withdrawn"}
        item = db.scalar(select(JourneyPublication).where(JourneyPublication.id == publication_id, JourneyPublication.journey_id == journey_id))
        if item is None: raise ApiProblem("not_found", "未找到该共享要求。", 404)
        snapshot = dict(item.event_json)
    payload = CreateSourceInput(source_type="manual", title=snapshot["title"], manual_event=ManualEventInput.model_validate(snapshot))
    # 跳过解析与自动助手队列，直接沿用成员确认接收的字段；保留独立的私人通知。
    return business_service.create_source(user_id, payload, source_type_override="trusted_share", external_key="shared:" + publication_id,
        submission_hash=business_service.submission_fingerprint(payload), queue_assistant=False)


def distribute(claim):
    with Session(journey._engine()) as db:
        item = db.scalar(select(JourneyPublication).where(JourneyPublication.id == claim.payload["publication_id"], JourneyPublication.user_id == claim.user_id))
        if item is None: return {"delivered": 0}
        identifier, route_id = item.id, item.journey_id
        people = db.scalars(select(JourneySourceTrust.user_id).where(JourneySourceTrust.journey_id == route_id, JourneySourceTrust.enabled.is_(True))).all()
    delivered = 0
    for user_id in people:
        try:
            result = adopt(user_id, route_id, identifier, require_trust=True)
            delivered += "skipped" not in result
        except ApiProblem as error:
            if error.status_code != 404: raise
    return {"delivered": delivered}
