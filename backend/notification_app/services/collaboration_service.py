"""在已有办理路线权限上补齐留言、文件收集及只返回交集的空闲统筹。"""

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..models import AgentAttachment, JourneyAvailability, JourneyComment, JourneyFile, JourneySourceTrust
from ..schemas.journeys import AvailabilityInput
from . import agent_material_service, journey_service as journey, planning_guard


def _write_access(db, user_id, journey_id):
    """与退出、撤回使用同一把路线锁，等待后重新核验当前身份。"""
    route = journey._access(db, user_id, journey_id)
    planning_guard.lock(db, route.user_id)
    return journey._access(db, user_id, journey_id)


def _aliases(db, route):
    """共享身份只使用路线内昵称，不返回账户邮箱。"""
    return {route.user_id: "发起人", **{m.user_id: m.alias for m in journey._members(db, route)}}


def list_content(user_id, journey_id):
    with Session(journey._engine()) as db:
        route = journey._access(db, user_id, journey_id)
        labels = _aliases(db, route)
        comments = db.scalars(select(JourneyComment).where(JourneyComment.journey_id == route.id)
                             .order_by(JourneyComment.created_at_epoch.desc(), JourneyComment.id).limit(100)).all()
        query = select(JourneyFile, AgentAttachment).join(AgentAttachment, JourneyFile.attachment_id == AgentAttachment.id)
        query = query.where(JourneyFile.journey_id == route.id)
        if route.user_id != user_id:
            query = query.where(or_(JourneyFile.user_id == user_id, JourneyFile.audience == "members"))
        files = db.execute(query.order_by(JourneyFile.created_at_epoch.desc(), JourneyFile.id).limit(100)).all()
        return {"comments": [{"id": c.id, "content": c.content, "author": labels.get(c.user_id, "已退出成员"),
                              "created_at_epoch": c.created_at_epoch, "can_delete": c.user_id == user_id or route.user_id == user_id} for c in comments],
                "files": [{"id": f.id, "filename": a.filename, "size_bytes": a.size_bytes, "audience": f.audience,
                           "author": labels.get(f.user_id, "已退出成员"), "can_delete": f.user_id == user_id or route.user_id == user_id,
                           "download_url": f"/api/v1/journeys/{route.id}/files/{f.id}/download"} for f, a in files]}


def add_comment(user_id, journey_id, content):
    """服务端再次校验内容，空白或超长留言不会落库。"""
    if not isinstance(content, str) or not 1 <= len(content.strip()) <= 2000:
        raise ApiProblem("invalid_comment", "留言需要为1至2000字。", 422)
    with Session(journey._engine()) as db, db.begin():
        route = _write_access(db, user_id, journey_id)
        db.add(JourneyComment(id=str(uuid4()), user_id=user_id, journey_id=route.id,
                             content=content.strip(), created_at_epoch=int(journey._now().timestamp())))
    return list_content(user_id, journey_id)


def share_file(user_id, journey_id, attachment_id, audience):
    """仅能发布自己的附件，默认仅发起人及上传者可见。"""
    if audience not in {"owner", "members"}:
        raise ApiProblem("invalid_audience", "请选择文件的共享范围。", 422)
    with Session(journey._engine()) as db, db.begin():
        route = _write_access(db, user_id, journey_id)
        agent_material_service.owned_material(db, user_id, attachment_id)
        existing = db.scalar(select(JourneyFile).where(JourneyFile.journey_id == route.id,
            JourneyFile.user_id == user_id, JourneyFile.attachment_id == attachment_id))
        if existing is None:
            db.add(JourneyFile(id=str(uuid4()), user_id=user_id, journey_id=route.id, attachment_id=attachment_id,
                               audience=audience, created_at_epoch=int(journey._now().timestamp())))
        elif existing.audience != audience:
            raise ApiProblem("file_scope_conflict", "该文件已提交；请撤回后再修改共享范围。", 409)
    return list_content(user_id, journey_id)


def download_file(user_id, journey_id, file_id):
    """每次下载重新核验成员资格，撤回访问立即生效。"""
    with Session(journey._engine()) as db:
        route = journey._access(db, user_id, journey_id)
        item = db.scalar(select(JourneyFile).where(JourneyFile.id == file_id, JourneyFile.journey_id == route.id))
        if item is None or (item.audience != "members" and user_id not in {item.user_id, route.user_id}):
            raise ApiProblem("not_found", "未找到该共享文件。", 404)
        owner, attachment_id = item.user_id, item.attachment_id
    return agent_material_service.material_file(owner, attachment_id)


def remove_content(user_id, journey_id, kind, item_id):
    """作者可撤回自己的内容，发起人可管理路线内容；不删除原私有附件。"""
    model = JourneyComment if kind == "comments" else JourneyFile
    with Session(journey._engine()) as db, db.begin():
        route = _write_access(db, user_id, journey_id)
        item = db.scalar(select(model).where(model.id == item_id, model.journey_id == route.id))
        if item is None:
            raise ApiProblem("not_found", "内容不存在。", 404)
        if user_id not in {item.user_id, route.user_id}:
            raise ApiProblem("forbidden", "只能撤回自己的内容。", 403)
        db.delete(item)
    return list_content(user_id, journey_id)


def consent_state(user_id, journey_id):
    with Session(journey._engine()) as db:
        journey._access(db, user_id, journey_id)
        item = db.scalar(select(JourneyAvailability).where(JourneyAvailability.journey_id == journey_id, JourneyAvailability.user_id == user_id))
        return {"version": item.version if item else 0, "availability": item.availability_json if item else AvailabilityInput().model_dump(),
                "expires_at_epoch": item.expires_at_epoch if item else None,
                "enabled": bool(item and item.expires_at_epoch > int(journey._now().timestamp()))}


def save_consent(user_id, journey_id, payload):
    """授权必须由本人提交，过期时间至多七天，并发更新使用版本校验。"""
    try:
        with Session(journey._engine()) as db, db.begin():
            _write_access(db, user_id, journey_id)
            values = {"availability_json": payload.availability.model_dump(),
                      "expires_at_epoch": int(journey._now().timestamp()) + payload.valid_days * 86400 if payload.enabled else 0,
                      "version": payload.expected_version + 1}
            item = db.scalar(select(JourneyAvailability).where(JourneyAvailability.journey_id == journey_id, JourneyAvailability.user_id == user_id))
            if item is not None and not payload.enabled:
                values['availability_json'] = item.availability_json
            if item is None and payload.expected_version == 0:
                db.add(JourneyAvailability(id=str(uuid4()), user_id=user_id, journey_id=journey_id, **values))
            elif item is None or db.execute(update(JourneyAvailability).where(JourneyAvailability.id == item.id,
                    JourneyAvailability.version == payload.expected_version).values(**values)).rowcount != 1:
                raise ApiProblem("version_conflict", "授权已变化，请刷新后重试。", 409)
    except IntegrityError:
        raise ApiProblem("version_conflict", "授权已变化，请刷新后重试。", 409) from None
    return consent_state(user_id, journey_id)


def clear_consent(db, journey_id, user_id):
    """退出与撤回访问时清除授权，重新加入也需本人重新同意。"""
    db.execute(delete(JourneyAvailability).where(JourneyAvailability.journey_id == journey_id, JourneyAvailability.user_id == user_id))
    db.execute(delete(JourneySourceTrust).where(JourneySourceTrust.journey_id == journey_id, JourneySourceTrust.user_id == user_id))


def common_availability(user_id, journey_id):
    """只返回全体已同意成员的公共交集，不泄漏任一人的课程、忙碌原因或个人窗口。"""
    with Session(journey._engine()) as db:
        route = journey._access(db, user_id, journey_id)
        people = [route.user_id] + [m.user_id for m in journey._members(db, route)]
        now = journey._now()
        consents = db.scalars(select(JourneyAvailability).where(JourneyAvailability.journey_id == route.id,
            JourneyAvailability.user_id.in_(people), JourneyAvailability.expires_at_epoch > int(now.timestamp()))).all()
        if len(consents) != len(people):
            return {"status": "awaiting_consent", "participants": len(people), "consented": len(consents), "slots": []}
        end = min(now + timedelta(days=7), *(datetime.fromtimestamp(c.expires_at_epoch, now.tzinfo) for c in consents))
        common = [(now, end)]
        for consent in consents:
            free = journey._free_intervals(db, consent.user_id, AvailabilityInput.model_validate(consent.availability_json), now, end)
            common = [(max(a, c), min(b, d)) for a, b in common for c, d in free if min(b, d) > max(a, c)]
        # 排除不足半小时、难以实际安排协作的零碎窗口。
        slots = [{"start_at": a.isoformat(), "end_at": b.isoformat()} for a, b in common if (b-a).total_seconds() >= 1800]
        return {"status": "ready", "participants": len(people), "consented": len(consents), "slots": slots[:100],
                "horizon_end": end.isoformat()}
