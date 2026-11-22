"""把不确定的自动结果留作可编辑建议；确认与业务写入共用一个事务。"""
import time
from datetime import date
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..errors import ApiProblem
from ..models import ChangeSet, NotificationSuggestion, SourceDocument
from ..schemas.business import NormalizedBundle
from . import business_service, planning_guard


def _engine(): return business_service._engine()


def defer(user_id, source_id, change_id, reason, bundle=None):
    with Session(_engine()) as db, db.begin():
        planning_guard.lock(db, user_id)
        source = business_service._owned(db, SourceDocument, source_id, user_id)
        change = business_service._owned(db, ChangeSet, change_id, user_id)
        if change.source_document_id != source.id:
            raise ApiProblem("invalid_suggestion_source", "建议与来源不一致。", 422)
        if business_service.change_applied(change):
            return {"decision": "skip", "reason": "此来源已经处理。", "source_id": source_id}
        candidate = NormalizedBundle.model_validate(bundle or change.bundle_json)
        business_service._verify_bundle_evidence(db, user_id=user_id, source=source, bundle=candidate)
        item = db.scalar(select(NotificationSuggestion).where(NotificationSuggestion.user_id == user_id, NotificationSuggestion.source_id == source_id))
        if item is None:
            now = int(time.time())
            item = NotificationSuggestion(id=str(uuid4()), user_id=user_id, source_id=source_id, change_set_id=change.id,
                change_version=change.version, source_hash=source.content_hash, bundle_json=candidate.model_dump(mode="json"),
                reason=str(reason)[:1000], confidence="low", status="pending", version=1, created_at_epoch=now, updated_at_epoch=now)
            db.add(item)
        return {"decision": "review", "reason": item.reason, "source_id": source_id, "suggestion_id": item.id, "href": "/today"}


def list_pending(user_id):
    with Session(_engine()) as db:
        pairs = db.execute(select(NotificationSuggestion, SourceDocument).join(SourceDocument, SourceDocument.id == NotificationSuggestion.source_id)
            .where(NotificationSuggestion.user_id == user_id, NotificationSuggestion.status == "pending")
            .order_by(NotificationSuggestion.created_at_epoch.desc()).limit(50)).all()
        items = []
        for item, source in pairs:
            # 已被用户在其他入口处理的来源，不再显示过期建议。
            change = db.get(ChangeSet, item.change_set_id)
            if not change or business_service.change_applied(change): continue
            events = []
            for event in item.bundle_json.get("events", [])[:20]:
                deadline = event.get("deadline") or {}
                # 自动推断出的完整年份不作为确认表单默认值，避免用户无意中接受猜测。
                from .main_assistant_service import _explicit_date
                local_date = deadline.get("local_date")
                explicit = bool(local_date and _explicit_date(date.fromisoformat(local_date), source.original_text))
                events.append({"title": event["title"], "actor": event.get("actor"), "stage": event.get("stage"), "scope": event.get("scope"),
                    "deadline_date": local_date if explicit else None, "deadline_time": ((deadline.get("local_time") or "")[:5] or None) if explicit else None,
                    "deadline_raw": deadline.get("raw"), "materials": event.get("materials", []), "submission": event.get("submission")})
            items.append({"id": item.id, "version": item.version, "source_id": source.id, "source_title": source.title,
                "source_text": source.original_text, "reason": item.reason, "confidence": item.confidence, "events": events})
        return {"items": items}


def confirm(user_id, suggestion_id, payload):
    with Session(_engine()) as db, db.begin():
        planning_guard.lock(db, user_id)
        item = business_service._owned(db, NotificationSuggestion, suggestion_id, user_id)
        if item.status == "accepted": return {"accepted": True, "change_set_id": item.change_set_id}
        if item.status != "pending" or item.version != payload.expected_version:
            raise ApiProblem("version_conflict", "建议已变化，请刷新。", 409)
        source = business_service._owned(db, SourceDocument, item.source_id, user_id)
        change = business_service._owned(db, ChangeSet, item.change_set_id, user_id)
        if source.content_hash != item.source_hash or change.version != item.change_version or not business_service.change_unapplied(change):
            raise ApiProblem("suggestion_stale", "来源或处理状态已变化，请重新核对。", 409)
        result = None
        for index, event in enumerate(payload.events):
            partial = business_service._manual_bundle(source.id, source.title, "", event)
            partial["events"][0]["temp_id"] = f"confirmed-{index}"
            if result is None: result = partial
            else: result["events"].extend(partial["events"])
        # 用户明确填写的字段与模型提取事实分开标注，正式日程仍需在日程入口安排。
        bundle = NormalizedBundle.model_validate(result)
        business_service._apply_in_session(db, user_id, user_id, change, int(time.time()), actor_type="user", bundle_override=bundle)
        item.status = "accepted"; item.version += 1; item.updated_at_epoch = int(time.time())
        return {"accepted": True, "change_set_id": change.id}


def dismiss(user_id, suggestion_id, expected):
    with Session(_engine()) as db, db.begin():
        planning_guard.lock(db, user_id)
        item = business_service._owned(db, NotificationSuggestion, suggestion_id, user_id)
        if item.version != expected or item.status != "pending": raise ApiProblem("version_conflict", "建议已变化，请刷新。", 409)
        item.status = "dismissed"; item.version += 1; item.updated_at_epoch = int(time.time())
    return {"dismissed": True}
