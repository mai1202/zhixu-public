"""通知、任务与课程之间的受控操作和真实关联。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from flask import current_app
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..models import (
    BusinessLink,
    ChangeSet,
    CourseOccurrence,
    CourseSeries,
    Event,
    Notice,
    OperationLog,
    SourceDocument,
    SourceSegment,
    Task,
)
from ..schemas.agent_ops import LinkInput, NotificationChangePlan, TaskCreateInput, TaskPatchInput
from ..schemas.business import NoticePatchInput
from ..text_sanitize import clean_title
from . import business_service, operation_service


def _engine():
    return current_app.extensions["notification_db_engine"]


_ENTITY_MODELS = {
    "notice": Notice,
    "task": Task,
    "course": CourseSeries,
    "course_occurrence": CourseOccurrence,
}


def _owned_entity(db: DbSession, entity_type: str, entity_id: str, user_id: str):
    model = _ENTITY_MODELS.get(entity_type)
    if model is None:
        raise ApiProblem("invalid_entity_type", "关联对象类型无效。", 422)
    entity = db.scalar(select(model).where(model.id == entity_id, model.user_id == user_id))
    if entity is None:
        raise ApiProblem("not_found", "关联对象不存在或不属于当前账户。", 404)
    return entity


def _entity_title(db: DbSession, entity_type: str, entity_id: str, user_id: str) -> str:
    try:
        entity = _owned_entity(db, entity_type, entity_id, user_id)
    except ApiProblem:
        return "已失效对象"
    if entity_type == "course_occurrence":
        series = db.scalar(
            select(CourseSeries).where(
                CourseSeries.id == entity.course_series_id,
                CourseSeries.user_id == user_id,
            )
        )
        return f"{series.display_name if series else '课程'} · {entity.local_date}"
    return str(getattr(entity, "title", "") or getattr(entity, "display_name", "") or "未命名对象")


def link_public(link: BusinessLink, db: DbSession | None = None) -> dict[str, Any]:
    result = {
        "id": link.id,
        "from_type": link.from_type,
        "from_id": link.from_id,
        "to_type": link.to_type,
        "to_id": link.to_id,
        "relation": link.relation,
        "evidence": link.evidence_json,
        "source_document_id": link.source_document_id,
        "status": link.status,
        "version": link.version,
        "created_at_epoch": link.created_at_epoch,
        "updated_at_epoch": link.updated_at_epoch,
    }
    if db is not None:
        result["from_title"] = _entity_title(db, link.from_type, link.from_id, link.user_id)
        result["to_title"] = _entity_title(db, link.to_type, link.to_id, link.user_id)
    return result


def link_entities_in_session(
    db: DbSession,
    *,
    user_id: str,
    payload: LinkInput,
    source_document_id: str | None = None,
    created_at_epoch: int | None = None,
    operations: list[dict[str, Any]] | None = None,
) -> BusinessLink:
    """校验两端归属后建立关系；同一边再次关联时幂等复用。"""
    from_type, from_id = payload.from_type, str(payload.from_id)
    to_type, to_id = payload.to_type, str(payload.to_id)
    _owned_entity(db, from_type, from_id, user_id)
    _owned_entity(db, to_type, to_id, user_id)
    allowed_pairs = {
        ("notice", "task"),
        ("notice", "course"),
        ("notice", "course_occurrence"),
        ("task", "course"),
        ("task", "course_occurrence"),
        ("course", "course_occurrence"),
    }
    if (from_type, to_type) not in allowed_pairs and (to_type, from_type) not in allowed_pairs:
        raise ApiProblem("invalid_link_type", "这两类对象不能建立该关系。", 422)
    if payload.source_document_id:
        source = db.scalar(
            select(SourceDocument).where(
                SourceDocument.id == str(payload.source_document_id),
                SourceDocument.user_id == user_id,
            )
        )
        if source is None:
            raise ApiProblem("not_found", "关系依据的来源不存在。", 404)
    now = created_at_epoch or operation_service.now_epoch()
    link = db.scalar(
        select(BusinessLink).where(
            BusinessLink.user_id == user_id,
            BusinessLink.from_type == from_type,
            BusinessLink.from_id == from_id,
            BusinessLink.to_type == to_type,
            BusinessLink.to_id == to_id,
            BusinessLink.relation == payload.relation,
        )
    )
    evidence = {"refs": [item.model_dump(mode="json") for item in payload.evidence]}
    previous = ({'status': link.status, 'evidence_json': deepcopy(link.evidence_json),
        'source_document_id': link.source_document_id} if link is not None else None)
    changed = link is None or link.status != 'active'
    if link is None:
        link = BusinessLink(
            id=str(uuid4()),
            user_id=user_id,
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
            relation=payload.relation,
            evidence_json=evidence,
            source_document_id=source_document_id or (str(payload.source_document_id) if payload.source_document_id else None),
            status="active",
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
        )
        db.add(link)
    elif link.status != "active":
        link.status = "active"
        link.evidence_json = evidence
        link.source_document_id = source_document_id or link.source_document_id
        link.version += 1
        link.updated_at_epoch = now
    if operations is not None and changed:
        # 关系副作用与主对象共用变更集；已有有效关系不归本次更新所有，撤销时不能误删。
        db.flush()
        operations.append({'type': 'business_link_snapshot', 'object_id': link.id,
            'before': previous, 'applied_version': link.version})
    return link


def link_entities(
    user_id: str,
    payload: LinkInput,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> dict[str, Any]:
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        link = link_entities_in_session(db, user_id=user_id, payload=payload)
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id or user_id,
            action="entity.link",
            object_type="business_link",
            object_id=link.id,
            summary=f"已关联{payload.from_type}与{payload.to_type}",
            after=link_public(link),
            source_document_id=link.source_document_id,
        )
        db.flush()
    with DbSession(_engine()) as db:
        link = db.scalar(
            select(BusinessLink).where(BusinessLink.id == link.id, BusinessLink.user_id == user_id)
        )
        return {"link": link_public(link, db)}


def unlink_entity(user_id: str, link_id: str, *, actor_type: str = "user") -> dict[str, Any]:
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        link = db.scalar(select(BusinessLink).where(BusinessLink.id == link_id, BusinessLink.user_id == user_id))
        if link is None:
            raise ApiProblem("not_found", "关联关系不存在。", 404)
        before = link_public(link, db)
        link.status = "revoked"
        link.version += 1
        link.updated_at_epoch = operation_service.now_epoch()
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=user_id,
            action="entity.unlink",
            object_type="business_link",
            object_id=link.id,
            summary="已解除对象关联",
            before=before,
            after=link_public(link),
            source_document_id=link.source_document_id,
        )
    return {"link": before, "status": "revoked"}


def links_for_entity(user_id: str, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
    """返回一个对象的所有有效关联，并补上对端标题供页面直接跳转。"""
    with DbSession(_engine()) as db:
        _owned_entity(db, entity_type, entity_id, user_id)
        rows = db.scalars(
            select(BusinessLink).where(
                BusinessLink.user_id == user_id,
                BusinessLink.status == "active",
                or_(
                    (BusinessLink.from_type == entity_type) & (BusinessLink.from_id == entity_id),
                    (BusinessLink.to_type == entity_type) & (BusinessLink.to_id == entity_id),
                ),
            ).order_by(BusinessLink.updated_at_epoch.desc())
        ).all()
        return [link_public(item, db) for item in rows]


def _task_context(db: DbSession, task: Task) -> dict[str, Any]:
    """任务公开视图补充来源标题和课程名称，界面无需再发多次请求。"""
    result = business_service.task_public(task)
    notice = db.get(Notice, task.notice_id) if task.notice_id else None
    series = db.get(CourseSeries, task.course_series_id) if task.course_series_id else None
    occurrence = db.get(CourseOccurrence, task.course_occurrence_id) if task.course_occurrence_id else None
    result["notice_title"] = notice.title if notice else None
    result["course_title"] = series.display_name if series else None
    result["course_date"] = occurrence.local_date if occurrence else None
    return result


def create_task_in_session(
    db: DbSession,
    *,
    user_id: str,
    payload: TaskCreateInput,
    actor_type: str,
    actor_id: str | None,
    source_document_id: str | None = None,
    change_set_id: str | None = None,
    now: int | None = None,
    source_event_id: str | None = None,
) -> tuple[Task, ChangeSet]:
    now = now or operation_service.now_epoch()
    notice_id = str(payload.notice_id) if payload.notice_id else None
    series_id = str(payload.course_series_id) if payload.course_series_id else None
    occurrence_id = str(payload.course_occurrence_id) if payload.course_occurrence_id else None
    if notice_id:
        _owned_entity(db, "notice", notice_id, user_id)
    if series_id:
        _owned_entity(db, "course", series_id, user_id)
    if occurrence_id:
        occurrence = _owned_entity(db, "course_occurrence", occurrence_id, user_id)
        if series_id and occurrence.course_series_id != series_id:
            raise ApiProblem("invalid_course_reference", "课程实例不属于所选课程系列。", 422)
        series_id = series_id or occurrence.course_series_id
    if source_document_id:
        source = db.scalar(
            select(SourceDocument).where(SourceDocument.id == source_document_id, SourceDocument.user_id == user_id)
        )
        if source is None:
            raise ApiProblem("not_found", "任务来源不存在。", 404)
    if source_event_id:
        # 机会采纳复用同一创建事务，初始修订和审计也应包含真实事项关联。
        source_event = db.scalar(select(Event).where(Event.id == source_event_id, Event.user_id == user_id))
        if source_event is None or (source_document_id and source_event.source_document_id != source_document_id):
            raise ApiProblem("invalid_event_reference", "事项与任务来源不一致。", 422)
    deadline = payload.deadline.model_dump(mode="json") if payload.deadline else None
    task = Task(
        id=str(uuid4()),
        user_id=user_id,
        event_id=source_event_id,
        source_document_id=source_document_id,
        notice_id=notice_id,
        course_series_id=series_id,
        course_occurrence_id=occurrence_id,
        # Agent 常把微信原文直接当标题，写入前去掉占位符与不可见字符。
        title=clean_title(payload.title, "未命名事务"),
        description=payload.description,
        task_kind=payload.task_kind,
        priority=payload.priority,
        due_json=deadline,
        due_at_epoch=business_service.deadline_sort_epoch(deadline),
        original_requirement=payload.original_requirement,
        reminder_enabled=payload.reminder_enabled,
        status="open",
        version=1,
        created_at_epoch=now,
        updated_at_epoch=now,
    )
    db.add(task)
    db.flush()
    # 创建与审计在同一事务；task 先 flush 以便关系和修订引用稳定 ID。
    change_set = ChangeSet(
        id=change_set_id or str(uuid4()),
        user_id=user_id,
        source_document_id=source_document_id,
        kind="operations",
        status="confirmed",
        summary=f"{'Agent' if actor_type == 'agent' else '用户'}创建任务：{task.title}"[:1000],
        bundle_json=None,
        operations_json=[{"type": "created", "object_type": "task", "object_id": task.id, "applied_version": 1}],
        version=2,
        change_seq=business_service._next_change_seq(db, user_id),
        created_at_epoch=now,
        updated_at_epoch=now,
        confirmed_at_epoch=now,
    )
    db.add(change_set)
    business_service._revision(
        db,
        user_id=user_id,
        object_type="task",
        object_id=task.id,
        version=1,
        data=business_service._entity_data(task, "task"),
        source_document_id=source_document_id,
        actor_type=actor_type,
        actor_id=actor_id,
        reason="创建独立任务",
        now=now,
    )
    if notice_id:
        link_entities_in_session(
            db,
            user_id=user_id,
            payload=LinkInput(from_type="notice", from_id=notice_id, to_type="task", to_id=task.id, relation="task_source"),
            source_document_id=source_document_id,
            created_at_epoch=now,
        )
    if series_id:
        link_entities_in_session(
            db,
            user_id=user_id,
            payload=LinkInput(from_type="task", from_id=task.id, to_type="course", to_id=series_id, relation="task_course"),
            source_document_id=source_document_id,
            created_at_epoch=now,
        )
    if occurrence_id:
        link_entities_in_session(
            db,
            user_id=user_id,
            payload=LinkInput(
                from_type="task",
                from_id=task.id,
                to_type="course_occurrence",
                to_id=occurrence_id,
                relation="task_course_occurrence",
            ),
            source_document_id=source_document_id,
            created_at_epoch=now,
        )
    operation_service.record_operation(
        db,
        user_id=user_id,
        actor_type=actor_type,
        actor_id=actor_id or user_id,
        action="task.create",
        object_type="task",
        object_id=task.id,
        change_set_id=change_set.id,
        source_document_id=source_document_id,
        summary=change_set.summary,
        after=business_service._entity_data(task, "task"),
        undoable=True,
        created_at_epoch=now,
    )
    return task, change_set


def create_task(
    user_id: str,
    payload: TaskCreateInput,
    *,
    actor_type: str = "agent",
    actor_id: str | None = None,
    source_document_id: str | None = None,
) -> dict[str, Any]:
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        task, change_set = create_task_in_session(
            db,
            user_id=user_id,
            payload=payload,
            actor_type=actor_type,
            actor_id=actor_id,
            source_document_id=source_document_id,
        )
    with DbSession(_engine()) as db:
        task = db.get(Task, task.id)
        result = {"task": _task_context(db, task), "change_set": business_service.change_set_public(change_set)}
    from . import reminder_service

    try:
        reminder_service.sync_task_reminders(user_id, task_id=task.id)
    except Exception:
        # 提醒属于旁路服务，不能因邮件设置异常回滚已完成的任务写入。
        pass
    return result


def update_task_in_session(
    db: DbSession,
    *,
    user_id: str,
    task_id: str,
    payload: TaskPatchInput,
    actor_type: str,
    actor_id: str | None,
    source_document_id: str | None = None,
    now: int | None = None,
) -> tuple[Task, ChangeSet]:
    now = now or operation_service.now_epoch()
    task = _owned_entity(db, "task", task_id, user_id)
    if task.version != payload.expected_version or task.archived_at_epoch is not None:
        raise ApiProblem("version_conflict", "任务已变化，请重新读取后再更新。", 409)
    values = payload.model_dump(mode="json", exclude_unset=True, exclude={"expected_version"})
    if not values:
        raise ApiProblem("empty_patch", "至少需要修改一个任务字段。", 422)
    if payload.course_series_id:
        _owned_entity(db, "course", str(payload.course_series_id), user_id)
    resolved_series_id = None
    if payload.course_occurrence_id:
        occurrence = _owned_entity(db, "course_occurrence", str(payload.course_occurrence_id), user_id)
        if payload.course_series_id and occurrence.course_series_id != str(payload.course_series_id):
            raise ApiProblem("invalid_course_reference", "课程实例不属于所选课程系列。", 422)
        resolved_series_id = str(payload.course_series_id) if payload.course_series_id else occurrence.course_series_id
    elif payload.course_series_id:
        resolved_series_id = str(payload.course_series_id)
    before = business_service._entity_data(task, "task")
    # 所有实际赋值都必须发生在旧快照之后，尤其是从课程实例推导的系列 ID。
    if resolved_series_id is not None:
        task.course_series_id = resolved_series_id
    if "title" in values:
        task.title = clean_title(values["title"], task.title)
    if "description" in values:
        task.description = values["description"]
    if "task_kind" in values:
        task.task_kind = values["task_kind"]
    if "priority" in values:
        task.priority = values["priority"]
    if "deadline" in values:
        task.due_json = values["deadline"]
        task.due_at_epoch = business_service.deadline_sort_epoch(values["deadline"])
    if "original_requirement" in values:
        task.original_requirement = values["original_requirement"]
    if "reminder_enabled" in values:
        task.reminder_enabled = bool(values["reminder_enabled"])
    if payload.course_occurrence_id:
        task.course_occurrence_id = str(payload.course_occurrence_id)
    task.version += 1
    task.updated_at_epoch = now
    after = business_service._entity_data(task, "task")
    operation = {
        "type": "snapshot",
        "object_type": "task",
        "object_id": task.id,
        "before": deepcopy(before),
        "applied_version": task.version,
    }
    change_set = ChangeSet(
        id=str(uuid4()),
        user_id=user_id,
        source_document_id=source_document_id or task.source_document_id,
        kind="operations",
        status="confirmed",
        summary=f"{'Agent' if actor_type == 'agent' else '用户'}更新任务：{task.title}"[:1000],
        bundle_json=None,
        operations_json=[operation],
        version=2,
        change_seq=business_service._next_change_seq(db, user_id),
        created_at_epoch=now,
        updated_at_epoch=now,
        confirmed_at_epoch=now,
    )
    db.add(change_set)
    business_service._revision(
        db,
        user_id=user_id,
        object_type="task",
        object_id=task.id,
        version=task.version,
        data=after,
        source_document_id=source_document_id or task.source_document_id,
        actor_type=actor_type,
        actor_id=actor_id,
        reason=change_set.summary,
        now=now,
    )
    link_operations: list[dict[str, Any]] = []
    if payload.course_series_id:
        link_entities_in_session(
            db,
            user_id=user_id,
            payload=LinkInput(
                from_type="task",
                from_id=task.id,
                to_type="course",
                to_id=str(payload.course_series_id),
                relation="task_course",
            ),
            source_document_id=source_document_id or task.source_document_id,
            created_at_epoch=now,
            operations=link_operations,
        )
    if payload.course_occurrence_id:
        link_entities_in_session(
            db,
            user_id=user_id,
            payload=LinkInput(
                from_type="task",
                from_id=task.id,
                to_type="course_occurrence",
                to_id=str(payload.course_occurrence_id),
                relation="task_course_occurrence",
            ),
            source_document_id=source_document_id or task.source_document_id,
            created_at_epoch=now,
            operations=link_operations,
        )
    if source_document_id:
        source_notice = db.scalar(
            select(Notice).where(
                Notice.user_id == user_id,
                Notice.source_document_id == source_document_id,
            ).order_by(Notice.created_at_epoch.desc()).limit(1)
        )
        if source_notice is not None:
            link_entities_in_session(
                db,
                user_id=user_id,
                payload=LinkInput(
                    from_type="notice",
                    from_id=source_notice.id,
                    to_type="task",
                    to_id=task.id,
                    relation="task_update_source",
                ),
                source_document_id=source_document_id,
                created_at_epoch=now,
                operations=link_operations,
            )
    if link_operations:
        change_set.operations_json = [operation, *link_operations]
    operation_service.record_operation(
        db,
        user_id=user_id,
        actor_type=actor_type,
        actor_id=actor_id or user_id,
        action="task.update",
        object_type="task",
        object_id=task.id,
        change_set_id=change_set.id,
        source_document_id=source_document_id or task.source_document_id,
        summary=change_set.summary,
        before=before,
        after=after,
        undoable=True,
        created_at_epoch=now,
    )
    return task, change_set


def update_task(
    user_id: str,
    task_id: str,
    payload: TaskPatchInput,
    *,
    actor_type: str = "agent",
    actor_id: str | None = None,
    source_document_id: str | None = None,
) -> dict[str, Any]:
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        task, change_set = update_task_in_session(
            db,
            user_id=user_id,
            task_id=task_id,
            payload=payload,
            actor_type=actor_type,
            actor_id=actor_id,
            source_document_id=source_document_id,
        )
    with DbSession(_engine()) as db:
        task = db.get(Task, task.id)
        result = {"task": _task_context(db, task), "change_set": business_service.change_set_public(change_set)}
    from . import reminder_service

    try:
        reminder_service.sync_task_reminders(user_id, task_id=task.id)
    except Exception:
        pass
    return result


def merge_tasks(
    user_id: str,
    *,
    source_task_id: str,
    target_task_id: str,
    source_expected_version: int,
    target_expected_version: int,
    actor_type: str = "agent",
    actor_id: str | None = None,
) -> dict[str, Any]:
    """把重复任务并入保留任务；合并期限、优先级、课程上下文和来源关系。"""
    from . import operation_service

    now = operation_service.now_epoch()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        source = _owned_entity(db, "task", source_task_id, user_id)
        target = _owned_entity(db, "task", target_task_id, user_id)
        if source.id == target.id:
            raise ApiProblem("invalid_merge", "不能把任务合并到自身。", 422)
        if source.version != source_expected_version or target.version != target_expected_version:
            raise ApiProblem("version_conflict", "任务已变化，请重新读取后再合并。", 409)
        if source.status != "open" or target.status != "open":
            raise ApiProblem("task_not_mergeable", "只有两个未完成任务可以合并。", 409)
        source_before = business_service._entity_data(source, "task")
        target_before = business_service._entity_data(target, "task")
        # 保留更早的内部期限；同日期时用户可见的 due_json 仍由保留任务决定。
        if target.due_at_epoch is None or (source.due_at_epoch is not None and source.due_at_epoch < target.due_at_epoch):
            target.due_json = deepcopy(source.due_json)
            target.due_at_epoch = source.due_at_epoch
        priority_order = {"low": 1, "normal": 2, "high": 3, "urgent": 4}
        if priority_order.get(source.priority, 0) > priority_order.get(target.priority, 0):
            target.priority = source.priority
        target.course_series_id = target.course_series_id or source.course_series_id
        target.course_occurrence_id = target.course_occurrence_id or source.course_occurrence_id
        if source.description and source.description not in (target.description or ""):
            target.description = "\n".join(filter(None, [target.description, source.description]))[:5000]
        target.version += 1
        target.updated_at_epoch = now
        source.version += 1
        source.updated_at_epoch = now
        source.status = "cancelled"
        operations = [
            {
                "type": "snapshot",
                "object_type": "task",
                "object_id": target.id,
                "before": target_before,
                "applied_version": target.version,
            },
            {
                "type": "snapshot",
                "object_type": "task",
                "object_id": source.id,
                "before": source_before,
                "applied_version": source.version,
            },
        ]
        change_set = ChangeSet(
            id=str(uuid4()),
            user_id=user_id,
            source_document_id=target.source_document_id or source.source_document_id,
            kind="operations",
            status="confirmed",
            summary=f"{'Agent' if actor_type == 'agent' else '用户'}合并重复任务：{source.title} → {target.title}"[:1000],
            bundle_json=None,
            operations_json=operations,
            version=2,
            change_seq=business_service._next_change_seq(db, user_id),
            created_at_epoch=now,
            updated_at_epoch=now,
            confirmed_at_epoch=now,
        )
        db.add(change_set)
        for task in (target, source):
            business_service._revision(
                db,
                user_id=user_id,
                object_type="task",
                object_id=task.id,
                version=task.version,
                data=business_service._entity_data(task, "task"),
                source_document_id=task.source_document_id,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=change_set.summary,
                now=now,
            )
        source_links = db.scalars(
            select(BusinessLink).where(
                BusinessLink.user_id == user_id,
                BusinessLink.status == "active",
                (
                    ((BusinessLink.from_type == "task") & (BusinessLink.from_id == source.id))
                    | ((BusinessLink.to_type == "task") & (BusinessLink.to_id == source.id))
                ),
            )
        ).all()
        for link in source_links:
            other_type = link.to_type if link.from_type == "task" else link.from_type
            other_id = link.to_id if link.from_type == "task" else link.from_id
            if other_type in {"notice", "course", "course_occurrence"}:
                link_entities_in_session(
                    db,
                    user_id=user_id,
                    payload=LinkInput(
                        from_type=other_type,
                        from_id=other_id,
                        to_type="task",
                        to_id=target.id,
                        relation=link.relation,
                    ),
                    source_document_id=link.source_document_id,
                    created_at_epoch=now,
                )
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id or user_id,
            action="task.merge",
            object_type="task",
            object_id=target.id,
            change_set_id=change_set.id,
            source_document_id=target.source_document_id or source.source_document_id,
            summary=change_set.summary,
            before={"source": source_before, "target": target_before},
            after={
                "source": business_service._entity_data(source, "task"),
                "target": business_service._entity_data(target, "task"),
            },
            undoable=True,
            created_at_epoch=now,
        )
        result = {
            "target": _task_context(db, target),
            "source": _task_context(db, source),
            "change_set": business_service.change_set_public(change_set),
        }
    try:
        from . import reminder_service

        reminder_service.sync_task_reminders(user_id)
    except Exception:
        pass
    return result


def update_notice(
    user_id: str,
    notice_id: str,
    payload: NoticePatchInput,
    *,
    actor_type: str = "agent",
    actor_id: str | None = None,
) -> dict[str, Any]:
    """修正通知标题、重要性或信息完整性，并保留可撤销版本。"""
    from . import operation_service

    now = operation_service.now_epoch()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        notice = _owned_entity(db, "notice", notice_id, user_id)
        if notice.version != payload.expected_version or notice.archived_at_epoch is not None:
            raise ApiProblem("version_conflict", "通知已变化，请重新读取。", 409)
        values = payload.model_dump(exclude={"expected_version"}, exclude_unset=True)
        if not values:
            raise ApiProblem("empty_patch", "至少需要修改一个通知字段。", 422)
        before = business_service._entity_data(notice, "notice")
        for key, value in values.items():
            setattr(notice, key, value)
        notice.version += 1
        notice.updated_at_epoch = now
        operations = [
            {
                "type": "snapshot",
                "object_type": "notice",
                "object_id": notice.id,
                "before": before,
                "applied_version": notice.version,
            }
        ]
        change_set = ChangeSet(
            id=str(uuid4()),
            user_id=user_id,
            source_document_id=notice.source_document_id,
            kind="operations",
            status="confirmed",
            summary=f"{'Agent' if actor_type == 'agent' else '用户'}修正通知：{notice.title}"[:1000],
            bundle_json=None,
            operations_json=operations,
            version=2,
            change_seq=business_service._next_change_seq(db, user_id),
            created_at_epoch=now,
            updated_at_epoch=now,
            confirmed_at_epoch=now,
        )
        db.add(change_set)
        business_service._revision(
            db,
            user_id=user_id,
            object_type="notice",
            object_id=notice.id,
            version=notice.version,
            data=business_service._entity_data(notice, "notice"),
            source_document_id=notice.source_document_id,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=change_set.summary,
            now=now,
        )
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id or user_id,
            action="notice.update",
            object_type="notice",
            object_id=notice.id,
            change_set_id=change_set.id,
            source_document_id=notice.source_document_id,
            summary=change_set.summary,
            before=before,
            after=business_service._entity_data(notice, "notice"),
            undoable=True,
            created_at_epoch=now,
        )
        result = {
            "notice": business_service.notice_public(notice),
            "change_set": business_service.change_set_public(change_set),
        }
    return result


def search_tasks(
    user_id: str,
    *,
    query: str = "",
    status: str = "open",
    limit: int = 20,
) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        statement = select(Task).where(Task.user_id == user_id, Task.archived_at_epoch.is_(None))
        if status != "all":
            statement = statement.where(Task.status == status)
        if query.strip():
            pattern = f"%{query.strip()[:200]}%"
            statement = statement.where(or_(Task.title.ilike(pattern), Task.description.ilike(pattern)))
        rows = db.scalars(
            statement.order_by(
                Task.priority.in_(("urgent", "high")).desc(),
                Task.due_at_epoch.is_(None),
                Task.due_at_epoch,
                Task.updated_at_epoch.desc(),
            ).limit(max(1, min(limit, 50)))
        ).all()
        return {"items": [_task_context(db, row) for row in rows], "limit": min(limit, 50)}


def task_detail(user_id: str, task_id: str) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        task = _owned_entity(db, "task", task_id, user_id)
        event = db.get(Event, task.event_id) if task.event_id else None
        return {
            "task": _task_context(db, task),
            "event": business_service.event_public(event) if event else None,
            "links": links_for_entity(user_id, "task", task_id),
            "operations": operation_service.list_operations(
                user_id, limit=20, object_type="task", object_id=task_id
            )["items"],
        }


def search_courses(
    user_id: str,
    *,
    query: str = "",
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """按名称、教师、地点和日期检索课程实例，供 Agent 在修改前消歧。"""
    today = date.today()
    start_date = start_date or today
    end_date = end_date or (start_date + timedelta(days=62))
    if end_date < start_date or (end_date - start_date).days > 62:
        raise ApiProblem("invalid_date_range", "课程查询最多覆盖 63 天。", 422)
    with DbSession(_engine()) as db:
        statement = (
            select(CourseOccurrence, CourseSeries)
            .join(CourseSeries, CourseSeries.id == CourseOccurrence.course_series_id)
            .where(
                CourseOccurrence.user_id == user_id,
                CourseOccurrence.local_date >= start_date.isoformat(),
                CourseOccurrence.local_date <= end_date.isoformat(),
                CourseOccurrence.status.in_(("scheduled", "rescheduled")),
                CourseSeries.status == "active",
            )
        )
        if query.strip():
            term = f"%{query.strip()[:200]}%"
            statement = statement.where(
                or_(
                    CourseSeries.display_name.ilike(term),
                    CourseSeries.title.ilike(term),
                    CourseSeries.room.ilike(term),
                    CourseOccurrence.room_override.ilike(term),
                )
            )
        rows = db.execute(
            statement.order_by(CourseOccurrence.local_date, CourseOccurrence.display_start).limit(min(limit, 50))
        ).all()
        return {
            "items": [
                {
                    "occurrence_id": occurrence.id,
                    "course_series_id": series.id,
                    "title": series.display_name,
                    "teachers": series.teachers_json,
                    "room": occurrence.room_override or series.room,
                    "local_date": occurrence.local_date,
                    "start": occurrence.display_start,
                    "end": occurrence.display_end,
                    "week_number": occurrence.week_number,
                    "status": occurrence.status,
                    "version": occurrence.version,
                }
                for occurrence, series in rows
            ],
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }


def course_detail(user_id: str, occurrence_id: str) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        occurrence = _owned_entity(db, "course_occurrence", occurrence_id, user_id)
        series = _owned_entity(db, "course", occurrence.course_series_id, user_id)
        # 单次变化与整门课程维护都会影响用户理解，详情页同时展示两层审计。
        occurrence_operations = operation_service.list_operations(
            user_id, limit=20, object_type="course_occurrence", object_id=occurrence_id
        )["items"]
        series_operations = operation_service.list_operations(
            user_id, limit=20, object_type="course_series", object_id=series.id
        )["items"]
        operations = sorted(
            {item["id"]: item for item in occurrence_operations + series_operations}.values(),
            key=lambda item: item["created_at_epoch"],
            reverse=True,
        )[:20]
        occurrence_links = links_for_entity(user_id, "course_occurrence", occurrence_id)
        series_links = links_for_entity(user_id, "course", series.id)
        links = list({item["id"]: item for item in occurrence_links + series_links}.values())
        return {
            "occurrence": {
                "id": occurrence.id,
                "course_series_id": series.id,
                "title": series.display_name,
                "teachers": series.teachers_json,
                "room": occurrence.room_override or series.room,
                "local_date": occurrence.local_date,
                "start": occurrence.display_start,
                "end": occurrence.display_end,
                "week_number": occurrence.week_number,
                "status": occurrence.status,
                "version": occurrence.version,
            },
            "series": {
                "id": series.id,
                "term_id": series.term_id,
                "title": series.title,
                "display_name": series.display_name,
                "teachers": series.teachers_json,
                "weekday": series.weekday,
                "periods": series.periods_json,
                "weeks": series.week_set_json,
                "start_time": series.start_time,
                "end_time": series.end_time,
                "room": series.room,
                "status": series.status,
                "version": series.version,
            },
            "links": links,
            "operations": operations,
        }


def upcoming_deadlines(user_id: str, *, days: int = 14, limit: int = 30) -> dict[str, Any]:
    now = operation_service.now_epoch()
    boundary = now + max(1, min(days, 63)) * 86400
    with DbSession(_engine()) as db:
        rows = db.scalars(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == "open",
                Task.archived_at_epoch.is_(None),
                Task.due_at_epoch.is_not(None),
                Task.due_at_epoch <= boundary,
            )
            .order_by(Task.due_at_epoch)
            .limit(max(1, min(limit, 100)))
        ).all()
        return {
            "items": [_task_context(db, row) for row in rows],
            "overdue": sum(1 for row in rows if (row.due_at_epoch or boundary) < now),
        }


def _validate_source_evidence(db: DbSession, user_id: str, source_id: str, references: list) -> None:
    """自动通知写入必须引用当前来源的精确片段。"""
    segments = db.scalars(
        select(SourceSegment).where(
            SourceSegment.user_id == user_id,
            SourceSegment.source_document_id == source_id,
        )
    ).all()
    texts = {item.id: item.text for item in segments}
    for reference in references:
        segment_id = str(reference.get("segment_id", "") if isinstance(reference, dict) else getattr(reference, "segment_id", ""))
        quote = str(reference.get("quote", "") if isinstance(reference, dict) else getattr(reference, "quote", ""))
        if not quote or quote not in texts.get(segment_id, ""):
            raise ApiProblem("invalid_evidence", "证据必须引用当前通知中的精确文本。", 422)


def apply_notification_changes(
    user_id: str,
    *,
    source_id: str,
    plan: NotificationChangePlan,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """执行通知驱动的跨模块计划；失败时尝试补偿，并如实保存未恢复部分。"""
    from . import operation_service, reminder_service, timetable_service
    from .planning_guard import digest

    execution_id = str(uuid5(NAMESPACE_URL, f"notification-execution:{user_id}:{source_id}:{digest(plan.model_dump(mode='json'))}"))

    def record_step(step, status, **details):
        """分步服务尚未统一事务时，保存开始、完成与补偿结果供中断后核对。"""
        with DbSession(_engine()) as journal, journal.begin():
            operation_service.record_operation(journal, user_id=user_id, actor_type="agent", actor_id=actor_id,
                action="notification.plan.step", object_type="source_document", object_id=source_id,
                source_document_id=source_id, summary=f"跨模块步骤：{step} · {status}",
                after={"execution_id": execution_id, "step": step, **details}, status=status)

    applied_courses: list[dict[str, Any]] = []
    applied_task_changes: list[str] = []
    with DbSession(_engine()) as db:
        source = db.scalar(
            select(SourceDocument).where(SourceDocument.id == source_id, SourceDocument.user_id == user_id)
        )
        change = db.scalar(
            select(ChangeSet).where(
                ChangeSet.user_id == user_id,
                ChangeSet.source_document_id == source_id,
                # 跨模块写入还会创建课程例外等操作变更集，不能把最新操作误当通知候选。
                ChangeSet.kind == "normalize",
                ChangeSet.undone_at_epoch.is_(None),
            )
            .order_by(ChangeSet.created_at_epoch.desc()).limit(1)
        )
        if source is None or change is None:
            raise ApiProblem("not_found", "自动处理来源不存在。", 404)
        if business_service.change_unapplied(change) and change.version != 1:
            raise ApiProblem("candidate_changed", "通知候选已被用户处理或修改，自动维护停止。", 409)
        # 通知可已直接入库，后续明确指令仍能维护课程/任务；记录起始版本防并发覆盖。
        starting_change_version = change.version
        original_change_id = change.id
        notice = db.scalar(
            select(Notice).where(Notice.user_id == user_id, Notice.source_document_id == source_id)
            .order_by(Notice.created_at_epoch.desc()).limit(1)
        )
        for course_plan in plan.course_changes:
            _validate_source_evidence(db, user_id, source_id, course_plan.evidence)
        for task_plan in plan.task_changes:
            _validate_source_evidence(db, user_id, source_id, task_plan.evidence)

    with DbSession(_engine()) as journal, journal.begin():
        if journal.get(OperationLog, execution_id):
            # 业务写入与回执之间仍有崩溃窗口，宁可转核对也不盲目重放整组副作用。
            raise ApiProblem("execution_requires_review", "此组合操作曾开始执行，请核对步骤回执后继续。", 409)
        marker = operation_service.record_operation(journal, user_id=user_id, actor_type="agent", actor_id=actor_id,
            action="notification.plan.started", object_type="source_document", object_id=source_id,
            source_document_id=source_id, summary="开始执行跨模块操作，分步结果持续保存。", after={"execution_id": execution_id}, status="started")
        marker.id = execution_id

    try:
        for course_plan in plan.course_changes:
            record_step(f"course:{course_plan.occurrence_id}", "started")
            change_payload = course_plan.change.model_copy(
                update={"notice_id": notice.id if notice else None}
            )
            result = timetable_service.apply_course_exception(
                user_id,
                str(course_plan.occurrence_id),
                change_payload,
                actor_type="agent",
                actor_id=actor_id,
                source_document_id=source_id,
                evidence=[item.model_dump(mode="json") for item in course_plan.evidence],
            )
            applied_courses.append(result)
            record_step(f"course:{course_plan.occurrence_id}", "applied", exception_id=result["exception"]["id"])
        for task_index, task_plan in enumerate(plan.task_changes):
            record_step(f"task:{task_index}", "started")
            if task_plan.action == "create":
                assert task_plan.create is not None
                create_payload = task_plan.create.model_copy(
                    update={"notice_id": task_plan.create.notice_id or (notice.id if notice else None)}
                )
                result = create_task(
                    user_id,
                    create_payload,
                    actor_type="agent",
                    actor_id=actor_id,
                    source_document_id=source_id,
                )
                applied_task_changes.append(result["change_set"]["id"])
            elif task_plan.action == "update":
                assert task_plan.patch is not None and task_plan.task_id is not None
                result = update_task(
                    user_id,
                    str(task_plan.task_id),
                    task_plan.patch,
                    actor_type="agent",
                    actor_id=actor_id,
                    source_document_id=source_id,
                )
                applied_task_changes.append(result["change_set"]["id"])
            else:
                assert task_plan.task_id is not None and task_plan.expected_version is not None
                result = business_service.set_task_status(
                    user_id,
                    actor_id or user_id,
                    str(task_plan.task_id),
                    task_plan.expected_version,
                    "completed" if task_plan.action == "complete" else "open",
                    actor_type="agent",
                )
                if result.get("change_set"):
                    applied_task_changes.append(result["change_set"]["id"])
            record_step(f"task:{task_index}", "applied", change_set_id=applied_task_changes[-1] if applied_task_changes else None)
    except Exception as error:
        rolled_back: list[str] = []
        rollback_failures: list[dict] = []
        for change_set_id in reversed(applied_task_changes):
            try:
                business_service.undo_change_set(user_id, user_id, change_set_id)
                rolled_back.append(f"task:{change_set_id}")
            except Exception as rollback_error:
                rollback_failures.append({"object": f"task:{change_set_id}", "error": getattr(rollback_error, "code", type(rollback_error).__name__)})
        for course_result in reversed(applied_courses):
            try:
                exception = course_result["exception"]
                timetable_service.undo_course_exception(
                    user_id,
                    exception["id"],
                    exception["version"],
                )
                rolled_back.append(f"course:{exception['id']}")
            except Exception as rollback_error:
                rollback_failures.append({"object": f"course:{exception['id']}", "error": getattr(rollback_error, "code", type(rollback_error).__name__)})
        with DbSession(_engine()) as db, db.begin():
            source = db.scalar(
                select(SourceDocument).where(SourceDocument.id == source_id, SourceDocument.user_id == user_id)
            )
            operation_service.record_operation(
                db,
                user_id=user_id,
                actor_type="agent",
                actor_id=actor_id,
                action="notification.plan.rollback",
                object_type="source_document",
                object_id=source_id,
                source_document_id=source_id,
                summary="跨模块变更部分保留，请核对补偿失败。" if rollback_failures else "跨模块自动变更未完整应用，已回滚可恢复部分。",
                after={"rolled_back": rolled_back, "rollback_failures": rollback_failures,
                    "execution_id": execution_id, "error": str(getattr(error, "code", type(error).__name__))},
                status="partial" if rollback_failures else "rolled_back",
            )
        if isinstance(error, ApiProblem) or rollback_failures:
            raise ApiProblem(
                "cross_module_partial_failure" if rollback_failures else "cross_module_rollback",
                "部分变更未能撤销，请查看操作记录并人工核对。" if rollback_failures else "跨模块变更未能完整应用，已回滚可恢复部分。",
                409,
                {"rolled_back": rolled_back, "rollback_failures": rollback_failures, "cause": getattr(error, "code", type(error).__name__)},
            ) from None
        raise

    now = operation_service.now_epoch()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        source = db.scalar(
            select(SourceDocument).where(SourceDocument.id == source_id, SourceDocument.user_id == user_id)
        )
        change = db.scalar(
            select(ChangeSet).where(
                ChangeSet.user_id == user_id,
                ChangeSet.source_document_id == source_id,
                # 失败补偿和撤销同样会生成变更集；这里只能认通知归一化候选。
                ChangeSet.kind == "normalize",
                ChangeSet.id == original_change_id,
                ChangeSet.undone_at_epoch.is_(None),
            )
            .order_by(ChangeSet.created_at_epoch.desc()).limit(1)
        )
        if source is None or change is None or change.version != starting_change_version:
            raise ApiProblem("candidate_changed", "通知候选在处理期间被修改，跨模块结果保留供核对。", 409)
        if business_service.change_unapplied(change):
            change.change_seq = business_service._next_change_seq(db, user_id)
            change.status = None
            change.version += 1
            change.confirmed_at_epoch = now
            change.updated_at_epoch = now
            change.summary = f"Agent 跨模块维护：{plan.summary}"[:1000]
        # 跨模块处理也必须留下 Notice 本体，不能只在收件箱里保留一个已确认候选。
        from ..schemas.business import NormalizedBundle

        bundle = NormalizedBundle.model_validate(change.bundle_json or {"schema_version": "1.0", "notices": [], "events": [], "schedule_entries": [], "change_candidates": [], "warnings": []})
        created_notices: list[Notice] = []
        for candidate in bundle.notices:
            existing = db.scalar(
                select(Notice).where(
                    Notice.user_id == user_id,
                    Notice.source_document_id == source_id,
                    Notice.title == candidate.title,
                ).limit(1)
            )
            if existing is not None:
                created_notices.append(existing)
                continue
            notice_item = Notice(
                id=str(uuid4()),
                user_id=user_id,
                source_document_id=source_id,
                title=candidate.title,
                importance="high" if applied_courses else "normal",
                last_change_set_id=change.id,
                status="active",
                version=1,
                created_at_epoch=now,
                updated_at_epoch=now,
            )
            db.add(notice_item)
            db.flush()
            business_service._revision(
                db,
                user_id=user_id,
                object_type="notice",
                object_id=notice_item.id,
                version=1,
                data=business_service._entity_data(notice_item, "notice"),
                source_document_id=source_id,
                actor_type="agent",
                actor_id=actor_id,
                reason="跨模块自动维护通知",
                now=now,
            )
            operation_service.record_operation(
                db,
                user_id=user_id,
                actor_type="agent",
                actor_id=actor_id,
                action="notice.create",
                object_type="notice",
                object_id=notice_item.id,
                change_set_id=change.id,
                source_document_id=source_id,
                summary=f"从通知创建业务通知：{notice_item.title}"[:1000],
                after=business_service._entity_data(notice_item, "notice"),
                undoable=False,
                created_at_epoch=now,
            )
            created_notices.append(notice_item)
        for notice_item in created_notices:
            for course_result in applied_courses:
                link_entities_in_session(
                    db,
                    user_id=user_id,
                    payload=LinkInput(
                        from_type="notice",
                        from_id=notice_item.id,
                        to_type="course_occurrence",
                        to_id=course_result["occurrence"]["id"],
                        relation="course_change",
                    ),
                    source_document_id=source_id,
                    created_at_epoch=now,
                )
            for subordinate_change_id in applied_task_changes:
                subordinate = db.get(ChangeSet, subordinate_change_id)
                if subordinate is None:
                    continue
                for operation in subordinate.operations_json:
                    if operation.get("object_type") == "task" and operation.get("object_id"):
                        link_entities_in_session(
                            db,
                            user_id=user_id,
                            payload=LinkInput(
                                from_type="notice",
                                from_id=notice_item.id,
                                to_type="task",
                                to_id=str(operation["object_id"]),
                                relation="task_update_source",
                            ),
                            source_document_id=source_id,
                            created_at_epoch=now,
                        )
        source.analysis_state = "processed"
        source.updated_at_epoch = now
        marker = db.get(OperationLog, execution_id)
        if marker:
            marker.status = "applied"
            marker.after_json = {"execution_id": execution_id, "task_change_sets": applied_task_changes,
                "course_exceptions": [item["exception"]["id"] for item in applied_courses]}
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type="agent",
            actor_id=actor_id,
            action="notification.plan.apply",
            object_type="source_document",
            object_id=source_id,
            change_set_id=change.id,
            source_document_id=source_id,
            summary=plan.summary,
            after={
                "reason": plan.reason,
                "courses": [item["exception"]["id"] for item in applied_courses],
                "task_change_sets": applied_task_changes,
            },
            undoable=False,
            created_at_epoch=now,
        )
        result_change = business_service.change_set_public(change)
        result_notice_id = created_notices[0].id if created_notices else None
    if applied_task_changes:
        try:
            reminder_service.sync_task_reminders(user_id, task_id=None)
        except Exception:
            pass
    try:
        from . import mail_service

        flag_name = "course_change" if applied_courses else "important_notice"
        mail_service.queue_change_alert(
            user_id,
            subject=f"通知管理 · {plan.summary}"[:300],
            body_text="\n".join(
                [
                    f"Agent 已处理：{plan.summary}",
                    "",
                    f"判断依据：{plan.reason}",
                    f"课程调整：{len(applied_courses)} 项",
                    f"任务变更：{len(applied_task_changes)} 项",
                    "",
                    "所有变更均保留原始通知与可撤销操作记录。请登录通知管理核对最新状态。",
                ]
            ),
            source_id=source_id,
            change_set_id=result_change["id"],
            flag_name=flag_name,
        )
    except Exception:
        # 邮件故障不影响已经完成的业务写入。
        pass
    return {
        "status": "handled",
        "source_id": source_id,
        "summary": plan.summary,
        "reason": plan.reason,
        "course_changes": applied_courses,
        "task_change_set_ids": applied_task_changes,
        "change_set": result_change,
        "notice_id": result_notice_id,
    }
