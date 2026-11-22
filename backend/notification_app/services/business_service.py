from copy import deepcopy
from datetime import datetime, time, timezone
import hashlib
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from pydantic import ValidationError
from flask import current_app

from ..errors import ApiProblem
from ..models import (
    BusinessLink,
    ChangeSet,
    CourseException,
    CourseOccurrence,
    CourseSeries,
    Event,
    Notice,
    OcrArtifact,
    OperationLog,
    Revision,
    ScheduleEntry,
    SourceDocument,
    SourceSegment,
    Task,
    User,
)
from ..rules.text import normalize_text, split_segments
from ..text_sanitize import clean_display_text, clean_title
from ..schemas.business import (
    ChangeSetUpdateInput,
    CreateSourceInput,
    ManualEventInput,
    NormalizedBundle,
)
from ..schemas.timetable import ScheduleEntryInput, UpdateScheduleEntryInput
from .provider_service import (
    ProviderFailure,
    extract_normalized_bundle,
    profile_execution_target,
    profile_fallback_policy,
)
from .timetable_service import (
    archive_schedule_entry_in_session,
    create_schedule_entry_in_session,
    restore_course_series_in_session,
    restore_schedule_entry_in_session,
    schedule_entry_public,
    update_schedule_entry_in_session,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
_MODEL_BY_TYPE = {
    "notice": Notice,
    "event": Event,
    "task": Task,
    "schedule_entry": ScheduleEntry,
    "course_occurrence": CourseOccurrence,
    "course_series": CourseSeries,
}


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _next_change_seq(db: DbSession, user_id: str) -> int:
    """在业务事务内分配用户级单调序号，日报游标不会依赖秒级时间戳。"""
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise ApiProblem("not_found", "未找到该账户。", 404)
    user.change_sequence += 1
    return user.change_sequence


def _snapshot(entity: Any, object_type: str) -> dict[str, Any]:
    """只快照可受控修改的业务字段，避免序列化 ORM 内部状态。"""
    if object_type == "notice":
        names = (
            "title", "importance", "status", "version",
            "updated_at_epoch", "archived_at_epoch",
        )
    elif object_type == "event":
        names = (
            "source_document_id", "title", "actor", "stage", "scope", "deadline_json", "time_range_json",
            "materials_json", "submission", "eligibility", "evidence_json", "unknowns_json",
            "identity_json", "field_policies_json",
            "status", "version", "updated_at_epoch", "archived_at_epoch",
        )
    elif object_type == "schedule_entry":
        names = (
            "source_event_id", "title", "kind", "local_date", "start_at", "end_at", "timezone",
            "status", "version", "updated_at_epoch", "archived_at_epoch",
        )
    elif object_type == "course_series":
        # 常规课程只保存可重新物化为实例的规则字段；单次变化仍记录在课程例外中。
        names = (
            "source_import_id", "term_id", "identity_key", "title", "display_name",
            "teachers_json", "room", "week_set_json", "weekday", "periods_json",
            "start_time", "end_time",
            "raw_text", "status", "version", "updated_at_epoch",
        )
    elif object_type == "course_occurrence":
        names = (
            "local_date", "display_start", "display_end", "busy_segments_json", "room_override",
            "status", "version",
        )
    else:
        names = (
            "source_document_id", "notice_id", "course_series_id", "course_occurrence_id",
            "title", "description", "task_kind", "priority", "due_json", "due_at_epoch",
            "original_requirement", "reminder_enabled", "status", "version", "updated_at_epoch",
            "completed_at_epoch", "archived_at_epoch",
        )
    return {name: deepcopy(getattr(entity, name)) for name in names}


def _revision(
    db: DbSession,
    *,
    user_id: str,
    object_type: str,
    object_id: str,
    version: int,
    data: dict[str, Any],
    source_document_id: str | None,
    actor_type: str,
    actor_id: str | None,
    reason: str,
    now: int,
) -> None:
    """新增只追加修订，记录人工确认、规则初建或撤销动作。"""
    db.add(
        Revision(
            id=str(uuid4()),
            user_id=user_id,
            object_type=object_type,
            object_id=object_id,
            version=version,
            data_json=data,
            source_document_id=source_document_id,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            created_at_epoch=now,
        )
    )


def _entity_data(entity: Any, object_type: str) -> dict[str, Any]:
    """生成去掉 owner 与数据库字段的修订快照。"""
    return _snapshot(entity, object_type)


def deadline_sort_epoch(deadline: dict[str, Any] | None, *, timezone_name: str = "Asia/Shanghai") -> int | None:
    """把截止精度转换为仅供排序/提醒使用的边界，不改变对用户展示的精度。"""
    if not deadline:
        return None
    utc_value = deadline.get("utc")
    if utc_value:
        try:
            return int(datetime.fromisoformat(str(utc_value).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    local_date = deadline.get("local_date")
    if not local_date:
        return None
    try:
        # 日期精度只代表“这一天内”；内部提醒边界取当地 08:00，向用户仍只展示日期。
        return int(datetime.combine(datetime.fromisoformat(str(local_date)).date(), time(hour=8), tzinfo=ZoneInfo(timezone_name)).timestamp())
    except (ValueError, KeyError):
        return None


def _manual_bundle(
    source_id: str,
    source_title: str,
    segment_id: str,
    form: ManualEventInput,
) -> dict[str, Any]:
    """手工表单也写入同一 NormalizedBundle，且明确没有原文证据引用。"""
    deadline = form.structured_deadline.model_dump(mode="json") if form.structured_deadline else None
    if deadline is None and form.deadline_date is not None:
        local_time = form.deadline_time
        local_dt = None
        if local_time:
            hour, minute = (int(part) for part in local_time.split(":"))
            local_dt = datetime.combine(form.deadline_date, time(hour, minute), tzinfo=SHANGHAI)
        deadline = {
            "raw": f"人工填写：{form.deadline_date.isoformat()}"
            + (f" {local_time}" if local_time else ""),
            "local_date": form.deadline_date.isoformat(),
            "local_time": f"{local_time}:00" if local_time else None,
            "timezone": "Asia/Shanghai",
            "precision": "minute" if local_time else "day",
            "utc": local_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if local_dt else None,
            "basis": "用户在手工表单中填写。",
        }
    _ = segment_id
    bundle = {
        "schema_version": "1.0",
        "notices": [{"temp_id": "manual-notice", "title": source_title, "source_ids": [source_id]}],
        "events": [
            {
                "temp_id": "manual-event",
                "notice_temp_id": "manual-notice",
                "title": form.title,
                "actor": form.actor,
                "stage": form.stage,
                "scope": form.scope,
                "deadline": deadline,
                "time_range": None,
                "materials": form.materials,
                "submission": form.submission,
                "eligibility": "unknown",
                "evidence": {},
                "unknowns": ["此项由用户手工录入，没有通知原文证据。"],
            }
        ],
        "schedule_entries": [],
        "change_candidates": [],
        "warnings": ["手工录入字段由用户提供，不代表通知官方事实。"],
    }
    return NormalizedBundle.model_validate(bundle).model_dump(mode="json")


def _verify_bundle_evidence(
    db: DbSession,
    *,
    user_id: str,
    source: SourceDocument,
    bundle: NormalizedBundle,
) -> None:
    """只接受当前来源片段中的精确引文，阻止跨用户或伪造定位。"""
    if any(source_id != source.id for notice in bundle.notices for source_id in notice.source_ids):
        raise ApiProblem("invalid_source_reference", "候选包含无权访问的来源。", 422)
    notice_ids = {notice.temp_id for notice in bundle.notices}
    event_ids = {event.temp_id for event in bundle.events}
    if any(event.notice_temp_id not in notice_ids for event in bundle.events):
        raise ApiProblem("invalid_notice_reference", "候选事项关联了不存在的通知。", 422)
    if any(entry.source_event_temp_id not in event_ids for entry in bundle.schedule_entries if entry.source_event_temp_id):
        raise ApiProblem("invalid_event_reference", "候选安排关联了不存在的事项。", 422)
    if bundle.schedule_entries or bundle.change_candidates:
        raise ApiProblem(
            "unsupported_candidate_operation",
            "当前版本只支持通知和事项候选；安排与局部变更须在对应功能完成后处理。",
            422,
        )

    segment_ids = {
        segment.id: segment.text
        for segment in db.scalars(
            select(SourceSegment).where(
                SourceSegment.user_id == user_id,
                SourceSegment.source_document_id == source.id,
            )
        ).all()
    }
    for event in bundle.events:
        for references in event.evidence.values():
            for reference in references:
                segment_text = segment_ids.get(reference.segment_id)
                if segment_text is None or reference.quote not in segment_text:
                    raise ApiProblem(
                        "invalid_evidence",
                        "证据必须引用当前材料中的精确文本。",
                        422,
                    )
    for candidate in bundle.change_candidates:
        if candidate.target_event_temp_id not in event_ids:
            raise ApiProblem("invalid_event_reference", "候选变更关联了不存在的事项。", 422)
        for reference in candidate.evidence:
            segment_text = segment_ids.get(reference.segment_id)
            if segment_text is None or reference.quote not in segment_text:
                raise ApiProblem("invalid_evidence", "证据必须引用当前材料中的精确文本。", 422)


def submission_fingerprint(payload: CreateSourceInput) -> str:
    """只记录规范化请求摘要，不把原文或密钥放进幂等键。"""
    import json
    return hashlib.sha256(json.dumps(payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def create_source(
    user_id: str,
    payload: CreateSourceInput,
    *,
    explicit_segments: list[str] | None = None,
    source_type_override: str | None = None,
    external_key: str | None = None,
    submission_hash: str | None = None,
    queue_assistant: bool = True,
) -> dict[str, Any]:
    """保存原始材料并直接应用有效结果；托管模型处理时由后台事务完成写入。"""
    from . import operation_service

    now = _now()
    source_id = str(uuid4())
    source_kind = source_type_override or payload.source_type
    if external_key is not None:
        if len(external_key) > 200 or not external_key:
            raise ApiProblem("external_key_invalid", "来源幂等键格式无效。", 422)
        with DbSession(_engine()) as db:
            existing_source = db.scalar(
                select(SourceDocument).where(
                    SourceDocument.user_id == user_id,
                    SourceDocument.source_type == source_kind,
                    SourceDocument.external_key == external_key,
                )
            )
            if existing_source is not None:
                if submission_hash is not None and existing_source.submission_hash != submission_hash:
                    raise ApiProblem("idempotency_conflict", "同一次提交的内容已改变，请作为新材料提交。", 409)
                existing_change = db.scalar(
                    select(ChangeSet)
                    .where(
                        ChangeSet.user_id == user_id,
                        ChangeSet.source_document_id == existing_source.id,
                    )
                    .order_by(ChangeSet.created_at_epoch.desc())
                    .limit(1)
                )
                if existing_change is not None:
                    with DbSession(_engine()) as db, db.begin():
                        operation_service.record_operation(
                            db,
                            user_id=user_id,
                            actor_type="system",
                            actor_id=None,
                            action="source.deduplicate",
                            object_type="source_document",
                            object_id=existing_source.id,
                            summary="同一来源幂等键重复提交，沿用已有材料与候选。",
                            source_document_id=existing_source.id,
                        )
                    return {
                        "source": source_public(existing_source),
                        "change_set": change_set_public(existing_change),
                    }
    if payload.source_type == "text":
        original_text = str(payload.text)
        if explicit_segments is None:
            parts = split_segments(original_text)
        else:
            # OCR 每一行保持独立稳定片段，后续图框证据可精确回链到原行。
            parts = []
            cursor = 0
            for item_text in explicit_segments:
                if item_text.strip():
                    parts.append((item_text, cursor, cursor + len(item_text)))
                cursor += len(item_text) + 1
        title = clean_title(
            payload.title or next((part for part, _start, _end in parts if part.strip()), "未命名通知")
        )
    else:
        form = payload.manual_event
        assert form is not None
        title = clean_title(payload.title or form.title)
        details = [f"手工录入：{form.title}"]
        for label, value in (("责任主体", form.actor), ("阶段", form.stage), ("范围", form.scope)):
            if value:
                details.append(f"{label}：{value}")
        if form.deadline_date:
            details.append(f"截止日期：{form.deadline_date.isoformat()}")
        details.extend(f"材料：{item}" for item in form.materials)
        if form.submission:
            details.append(f"提交渠道：{form.submission}")
        original_text = "\n".join(details)
        parts = split_segments(original_text)

    if len(original_text.encode("utf-8")) > 500_000:
        raise ApiProblem("content_too_large", "文本内容超过 500 KB。", 413)
    source = SourceDocument(
        id=source_id,
        user_id=user_id,
        source_type=source_kind,
        title=title,
        original_text=original_text,
        content_hash=hashlib.sha256(original_text.encode("utf-8")).hexdigest(),
        external_key=external_key,
        submission_hash=submission_hash,
        analysis_state="received",
        analysis_mode=payload.analysis_mode,
        provider_profile_id=payload.provider_profile_id,
        created_at_epoch=now,
        updated_at_epoch=now,
    )
    segment_pairs = [(str(uuid4()), text) for text, _start, _end in parts]
    local_inference_request: dict[str, Any] | None = None
    if payload.source_type == "text":
        bundle = normalize_text(
            original_text,
            source_id,
            [(segment_id, text) for (segment_id, text), (_text, _start, _end) in zip(segment_pairs, parts)],
        )
        if payload.analysis_mode in {"llm", "auto"}:
            if payload.provider_profile_id:
                fallback_policy = "none"
                execution_target = "server"
                try:
                    fallback_policy = profile_fallback_policy(user_id, payload.provider_profile_id)
                    execution_target = profile_execution_target(user_id, payload.provider_profile_id)
                except ApiProblem:
                    pass
                if execution_target == "device":
                    if fallback_policy == "rules":
                        fallback_bundle = NormalizedBundle.model_validate(bundle)
                        fallback_bundle.warnings.append("本机模型分析已排队；当前规则候选会在设备完成后更新。")
                    else:
                        fallback_bundle = NormalizedBundle.model_validate(
                            {
                                "schema_version": "1.0",
                                "notices": [{"temp_id": "source-only-notice", "title": title, "source_ids": [source_id]}],
                                "events": [],
                                "schedule_entries": [],
                                "change_candidates": [],
                                "warnings": ["本机模型分析已排队；原材料已保留，等待设备返回解析结果。"],
                            }
                        )
                    bundle = fallback_bundle.model_dump(mode="json")
                    source.analysis_state = "processing"
                    local_inference_request = {
                        "profile_id": payload.provider_profile_id,
                        "source_id": source_id,
                        "source_text": original_text,
                        "segments": [{"id": segment_id, "text": text} for segment_id, text in segment_pairs],
                    }
                else:
                    try:
                        model_bundle = extract_normalized_bundle(
                            user_id,
                            payload.provider_profile_id,
                            source_id=source_id,
                            source_text=original_text,
                            segments=[{"id": segment_id, "text": text} for segment_id, text in segment_pairs],
                        )
                        bundle = model_bundle.model_dump(mode="json")
                    except (ApiProblem, ProviderFailure) as error:
                        if fallback_policy == "rules":
                            fallback_bundle = NormalizedBundle.model_validate(bundle)
                            fallback_bundle.warnings.append(
                                f"模型候选未通过处理（{getattr(error, 'code', 'provider_error')}）；已保留材料并改用本地规则候选。"
                            )
                        else:
                            fallback_bundle = NormalizedBundle.model_validate(
                                {
                                    "schema_version": "1.0",
                                    "notices": [{"temp_id": "source-only-notice", "title": title, "source_ids": [source_id]}],
                                    "events": [],
                                    "schedule_entries": [],
                                    "change_candidates": [],
                                    "warnings": [
                                        f"模型候选未通过处理（{getattr(error, 'code', 'provider_error')}）；原材料已保留，未提取到的字段暂不写入。"
                                    ],
                                }
                            )
                        bundle = fallback_bundle.model_dump(mode="json")
            else:
                fallback_bundle = NormalizedBundle.model_validate(bundle)
                fallback_bundle.warnings.append("没有选择模型配置；此材料使用本地规则候选。")
                bundle = fallback_bundle.model_dump(mode="json")
    else:
        assert payload.manual_event is not None
        bundle = _manual_bundle(source_id, title, segment_pairs[0][0], payload.manual_event)

    change_id = str(uuid4())
    change_set = ChangeSet(
        id=change_id,
        user_id=user_id,
        source_document_id=source_id,
        kind="normalize",
        status=None,
        summary=f"接收通知：{title}"[:1000],
        bundle_json=bundle,
        operations_json=[],
        version=1,
        created_at_epoch=now,
        updated_at_epoch=now,
    )
    engine = _engine()
    from .main_assistant_service import get_preferences
    preference = get_preferences(user_id)
    background_maintenance = bool(queue_assistant and preference["profile_id"] and preference["automatic_notifications"])
    try:
        with DbSession(engine, expire_on_commit=False) as db, db.begin():
            db.add(source)
            for ordinal, ((segment_id, text), (_original, start, end)) in enumerate(zip(segment_pairs, parts)):
                db.add(
                    SourceSegment(
                        id=segment_id,
                        user_id=user_id,
                        source_document_id=source_id,
                        ordinal=ordinal,
                        text=text,
                        start_offset=start,
                        end_offset=end,
                        version=1,
                        created_at_epoch=now,
                    )
                )
            _verify_bundle_evidence(
                db,
                user_id=user_id,
                source=source,
                bundle=NormalizedBundle.model_validate(bundle),
            )
            db.add(change_set)
            db.flush()
            if source.analysis_state != "processing" and not background_maintenance:
                _apply_in_session(db, user_id, user_id, change_set, now, actor_type="system")
    except IntegrityError:
        if external_key is None:
            raise
        with DbSession(engine) as db:
            existing_source = db.scalar(
                select(SourceDocument).where(
                    SourceDocument.user_id == user_id,
                    SourceDocument.source_type == source_kind,
                    SourceDocument.external_key == external_key,
                )
            )
            existing_change = (
                db.scalar(
                    select(ChangeSet)
                    .where(ChangeSet.user_id == user_id, ChangeSet.source_document_id == existing_source.id)
                    .order_by(ChangeSet.created_at_epoch.desc())
                    .limit(1)
                )
                if existing_source is not None
                else None
            )
            if existing_source is None or existing_change is None:
                raise
            if submission_hash is not None and existing_source.submission_hash != submission_hash:
                raise ApiProblem("idempotency_conflict", "同一次提交的内容已改变，请作为新材料提交。", 409)
            with DbSession(engine) as db, db.begin():
                operation_service.record_operation(
                    db,
                    user_id=user_id,
                    actor_type="system",
                    actor_id=None,
                    action="source.deduplicate",
                    object_type="source_document",
                    object_id=existing_source.id,
                    summary="并发重复提交被唯一键合并，沿用已有材料与候选。",
                    source_document_id=existing_source.id,
                )
            return {
                "source": source_public(existing_source),
                "change_set": change_set_public(existing_change),
            }
    local_job_id = None
    if local_inference_request is not None:
        try:
            from .provider_service import queue_local_extraction

            local_job_id = queue_local_extraction(
                user_id,
                local_inference_request["profile_id"],
                source_id=source_id,
                change_set_id=change_id,
                expected_change_set_version=1,
                source_text=local_inference_request["source_text"],
                segments=local_inference_request["segments"],
            )
        except ApiProblem as error:
            # 若配置在排队窗口被禁用或删除，保留已写入原文并开放规则候选核验。
            with DbSession(engine, expire_on_commit=False) as db, db.begin():
                current_source = db.scalar(select(SourceDocument).where(SourceDocument.id == source_id, SourceDocument.user_id == user_id))
                current_change = db.scalar(select(ChangeSet).where(ChangeSet.id == change_id, ChangeSet.user_id == user_id))
                if current_source is not None and current_change is not None:
                    current_source.analysis_state = "processed"
                    current_source.updated_at_epoch = _now()
                    current_bundle = NormalizedBundle.model_validate(current_change.bundle_json)
                    current_bundle.warnings.append(f"本机模型任务未能排队（{error.code}）；请核验现有候选。")
                    current_change.bundle_json = current_bundle.model_dump(mode="json")
                    current_change.updated_at_epoch = current_source.updated_at_epoch
            source.analysis_state = "processed"
            source.updated_at_epoch = _now()
            change_set.bundle_json = current_change.bundle_json if current_change is not None else bundle
            change_set.updated_at_epoch = source.updated_at_epoch
    result = {
        "source": source_public(source),
        "change_set": change_set_public(change_set),
    }
    if local_job_id is not None:
        result["analysis_job_id"] = local_job_id
    # 主助手独立排队，不阻塞本机批次 ACK；所有原文来源都可整理，同一来源只登记一个工作。
    from .main_assistant_service import queue_notification
    if queue_assistant:
        queue_notification(user_id, source_id, change_id)
    # 向量通道开启时把新通知纳入语义召回；未配置端点时保持关键词路径。
    try:
        from .retrieval_service import schedule_vector_index
        schedule_vector_index(user_id, "notification", source_id)
    except Exception:
        current_app.logger.warning("通知向量索引调度失败 source_id=%s", source_id)
    return result


def _engine():
    from flask import current_app

    return current_app.extensions["notification_db_engine"]


def source_public(source: SourceDocument) -> dict[str, Any]:
    return {
        "id": source.id,
        "source_type": source.source_type,
        # 升级前写入的微信原文标题可能带方括号占位符，读取时再清理一次。
        "title": clean_title(source.title),
        "original_text": source.original_text,
        "analysis_state": source.analysis_state,
        "analysis_mode": source.analysis_mode,
        "provider_profile_id": source.provider_profile_id,
        "created_at_epoch": source.created_at_epoch,
    }


def change_set_public(change_set: ChangeSet) -> dict[str, Any]:
    result = {
        "id": change_set.id,
        "kind": change_set.kind,
        "status": change_set.status,
        "summary": change_set.summary,
        "source_document_id": change_set.source_document_id,
        "bundle": change_set.bundle_json,
        "operations": change_set.operations_json,
        "version": change_set.version,
        "created_at_epoch": change_set.created_at_epoch,
        "confirmed_at_epoch": change_set.confirmed_at_epoch,
        "undone_at_epoch": change_set.undone_at_epoch,
    }
    if is_notification_change(change_set):
        result.pop("status", None)
        result["applied"] = change_applied(change_set)
        result["applied_at_epoch"] = result.pop("confirmed_at_epoch", None)
    return result


def _owned(db: DbSession, model, object_id: str, user_id: str, code: str = "not_found"):
    entity = db.scalar(select(model).where(model.id == object_id, model.user_id == user_id))
    if entity is None:
        raise ApiProblem(code, "未找到该记录。", 404)
    return entity


def update_change_set(
    user_id: str,
    change_set_id: str,
    payload: ChangeSetUpdateInput,
) -> dict[str, Any]:
    """允许用户先修正候选包，再以版本号确认本次修改。"""
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        change_set = _owned(db, ChangeSet, change_set_id, user_id)
        if is_notification_change(change_set):
            raise ApiProblem("notification_review_removed", "通知候选编辑和人工确认已取消，请直接修改通知或事项。", 410)
        if change_set.status != "proposed" or change_set.version != payload.expected_version:
            raise ApiProblem("version_conflict", "候选已变化，请刷新后重新核验。", 409)
        if change_set.kind != "normalize" or change_set.source_document_id is None:
            raise ApiProblem("change_not_editable", "该变更集不能编辑候选包。", 409)
        source = _owned(db, SourceDocument, change_set.source_document_id, user_id)
        _verify_bundle_evidence(db, user_id=user_id, source=source, bundle=payload.bundle)
        change_set.bundle_json = payload.bundle.model_dump(mode="json")
        change_set.version += 1
        change_set.updated_at_epoch = now
    return change_set_public(change_set)


def _record_created(
    db: DbSession,
    *,
    user_id: str,
    source: SourceDocument,
    bundle: NormalizedBundle,
    actor_id: str,
    now: int,
    actor_type: str = "user",
) -> list[dict[str, Any]]:
    """在同一事务创建通知、事项、待办和首版修订。"""
    notices_by_temp: dict[str, Notice] = {}
    operations: list[dict[str, Any]] = []
    for candidate in bundle.notices:
        notice = Notice(
            id=str(uuid4()),
            user_id=user_id,
            source_document_id=source.id,
            title=clean_title(candidate.title),
            importance="normal",
            status="active",
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
        )
        db.add(notice)
        notices_by_temp[candidate.temp_id] = notice
        _revision(
            db,
            user_id=user_id,
            object_type="notice",
            object_id=notice.id,
            version=1,
            data=_entity_data(notice, "notice"),
            source_document_id=source.id,
            actor_type=actor_type,
            actor_id=actor_id,
            reason="直接登记通知",
            now=now,
        )
        operations.append({"type": "created", "object_type": "notice", "object_id": notice.id, "applied_version": 1})

    # 通知是事项的外键父表，先落库以保证多方言 ORM flush 顺序明确。
    db.flush()

    for candidate in bundle.events:
        notice = notices_by_temp.get(candidate.notice_temp_id or "")
        event = Event(
            id=str(uuid4()),
            user_id=user_id,
            notice_id=notice.id if notice else None,
            source_document_id=source.id,
            title=clean_title(candidate.title, "待核验事项"),
            actor=candidate.actor,
            stage=candidate.stage,
            scope=candidate.scope,
            deadline_json=candidate.deadline.model_dump(mode="json") if candidate.deadline else None,
            time_range_json=candidate.time_range.model_dump(mode="json") if candidate.time_range else None,
            materials_json=candidate.materials,
            submission=candidate.submission,
            eligibility=candidate.eligibility,
            evidence_json={key: [ref.model_dump(mode="json") for ref in refs] for key, refs in candidate.evidence.items()},
            unknowns_json=candidate.unknowns,
            status="open",
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
        )
        db.add(event)
        _revision(
            db,
            user_id=user_id,
            object_type="event",
            object_id=event.id,
            version=1,
            data=_entity_data(event, "event"),
            source_document_id=source.id,
            actor_type=actor_type,
            actor_id=actor_id,
            reason="直接登记事项",
            now=now,
        )
        operations.append({"type": "created", "object_type": "event", "object_id": event.id, "applied_version": 1})

        task = Task(
            id=str(uuid4()),
            user_id=user_id,
            event_id=event.id,
            source_document_id=source.id,
            notice_id=notice.id if notice else None,
            title=event.title,
            task_kind="assignment",
            priority="normal",
            due_json=deepcopy(event.deadline_json),
            due_at_epoch=deadline_sort_epoch(event.deadline_json),
            original_requirement=None,
            reminder_enabled=True,
            status="open",
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
        )
        db.add(task)
        _revision(
            db,
            user_id=user_id,
            object_type="task",
            object_id=task.id,
            version=1,
            data=_entity_data(task, "task"),
            source_document_id=source.id,
            actor_type=actor_type,
            actor_id=actor_id,
            reason="根据事项创建待办",
            now=now,
        )
        operations.append({"type": "created", "object_type": "task", "object_id": task.id, "applied_version": 1})
    return operations


def is_notification_change(change):
    """来源绑定的通知写入只保留审计与执行时间，不维护核对状态。"""
    return change.kind in {"normalize", "requirements"} or bool(change.source_document_id)


def change_applied(change):
    return bool(change.confirmed_at_epoch is not None and change.undone_at_epoch is None)


def change_unapplied(change):
    return change.confirmed_at_epoch is None and change.undone_at_epoch is None and (is_notification_change(change) or change.status == "proposed")


def apply_change_set(user_id, actor_id, change_set_id, expected_version, *, actor_type="user", bundle_override=None):
    """直接执行受控操作，重复请求只返回同一写入回执。"""
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        change_set = _owned(db, ChangeSet, change_set_id, user_id)
        if change_applied(change_set):
            return change_set_public(change_set)
        if not change_unapplied(change_set) or change_set.version != expected_version:
            raise ApiProblem("version_conflict", "操作已变化或已撤销，请重新读取。", 409)
        _apply_in_session(db, user_id, actor_id, change_set, now, actor_type=actor_type, bundle_override=bundle_override)
        return change_set_public(change_set)


def confirm_change_set(user_id, actor_id, change_set_id, expected_version, *, actor_type="user", bundle_override=None):
    """兼容独立日程的确认接口；通知不再要求人工确认，旧请求只读取已完成回执。"""
    with DbSession(_engine()) as db:
        change = _owned(db, ChangeSet, change_set_id, user_id)
        if is_notification_change(change):
            if change_applied(change): return change_set_public(change)
            raise ApiProblem("notification_review_removed", "通知人工确认流程已取消。", 410)
    return apply_change_set(user_id, actor_id, change_set_id, expected_version, actor_type=actor_type, bundle_override=bundle_override)


def _apply_in_session(db, user_id, actor_id, change_set, now, *, actor_type="system", bundle_override=None):
    """应用与原始通知入库共用事务，防止部分写入或重复登记。"""
    from . import operation_service
    change_set.change_seq = _next_change_seq(db, user_id)
    if change_set.kind == "normalize":
        if change_set.source_document_id is None or change_set.bundle_json is None:
            raise ApiProblem("invalid_change_set", "候选缺少来源或结构化数据。", 409)
        source = _owned(db, SourceDocument, change_set.source_document_id, user_id)
        if source.analysis_state == "processing":
            raise ApiProblem("source_analysis_pending", "材料仍在本机分析，等待解析结果。", 409)
        # 自动整理的候选与确认在同一事务应用，避免崩溃留下半次自动更新。
        bundle = bundle_override or NormalizedBundle.model_validate(change_set.bundle_json)
        if bundle.events and change_set.version == 1:
            # 来源已经分项维护过既有事项时，原始一次性候选不能再直接生成重复事项。
            resolved = db.scalars(select(ChangeSet).where(ChangeSet.user_id == user_id,
                ChangeSet.source_document_id == source.id, ChangeSet.kind == "requirements",
                ChangeSet.confirmed_at_epoch.is_not(None))).all()
            if resolved:
                raise ApiProblem("requirement_already_resolved", "此来源已有分项处理记录，本次停止重复登记。", 409,
                    {"requirement_change_ids": [item.id for item in resolved]})
        _verify_bundle_evidence(db, user_id=user_id, source=source, bundle=bundle)
        if bundle_override is not None:
            change_set.bundle_json = bundle.model_dump(mode="json")
        change_set.operations_json = _record_created(
            db,
            user_id=user_id,
            source=source,
            bundle=bundle,
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
        )
        # 通知详情可以直接定位到本次可撤销的操作，而不必扫描全部变更集。
        for operation in change_set.operations_json:
            if operation["object_type"] == "notice":
                notice = db.scalar(
                    select(Notice).where(
                        Notice.id == operation["object_id"],
                        Notice.user_id == user_id,
                    )
                )
                if notice is not None:
                    notice.last_change_set_id = change_set.id
        source.analysis_state = "processed"
        source.updated_at_epoch = now
    else:
        change_set.operations_json = _apply_proposed_operations(
            db,
            user_id=user_id,
            actor_id=actor_id,
            change_set=change_set,
            now=now,
            actor_type=actor_type,
        )
    for operation in change_set.operations_json:
        object_type = operation.get("object_type")
        model = _MODEL_BY_TYPE.get(object_type)
        object_id = operation.get("object_id")
        if model is None or not object_id:
            continue
        entity = db.scalar(select(model).where(model.id == object_id, model.user_id == user_id))
        if entity is None:
            continue
        after = _entity_data(entity, object_type)
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=f"{object_type}.{operation.get('type', 'update')}",
            object_type=object_type,
            object_id=object_id,
            change_set_id=change_set.id,
            source_document_id=getattr(entity, "source_document_id", change_set.source_document_id),
            summary=change_set.summary,
            after=after,
            undoable=True,
            created_at_epoch=now,
        )
    change_set.status = None if is_notification_change(change_set) else "confirmed"
    change_set.confirmed_at_epoch = now
    change_set.version += 1
    change_set.updated_at_epoch = now


def reject_change_set(user_id: str, change_set_id: str, expected_version: int) -> dict[str, Any]:
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        change_set = _owned(db, ChangeSet, change_set_id, user_id)
        if not change_unapplied(change_set) or change_set.version != expected_version:
            raise ApiProblem("version_conflict", "变更集已处理或版本过期。", 409)
        change_set.change_seq = _next_change_seq(db, user_id)
        change_set.status = None if is_notification_change(change_set) else "rejected"
        if is_notification_change(change_set): change_set.confirmed_at_epoch = now
        change_set.version += 1
        change_set.updated_at_epoch = now
        if change_set.source_document_id:
            source = _owned(db, SourceDocument, change_set.source_document_id, user_id)
            source.analysis_state = "processed"
            source.updated_at_epoch = now
    return change_set_public(change_set)


def reopen_change_set(user_id, change_set_id, expected_version):
    """旧核对入口退役；重新处理使用通知原文与普通编辑流程。"""
    with DbSession(_engine()) as db: _owned(db, ChangeSet, change_set_id, user_id)
    raise ApiProblem("notification_review_removed", "通知核对流程已取消，请直接处理通知。", 410)


def create_archive_change_set(user_id: str, actor_id: str, notice_id: str) -> dict[str, Any]:
    """把归档做成可预览且可撤销的变更集。"""
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        notice = _owned(db, Notice, notice_id, user_id)
        if notice.archived_at_epoch is not None:
            raise ApiProblem("already_archived", "通知已经归档。", 409)
        change_set = ChangeSet(
            id=str(uuid4()),
            user_id=user_id,
            source_document_id=notice.source_document_id,
            kind="operations",
            status=None,
            summary=f"归档通知：{notice.title[:400]}",
            bundle_json=None,
            operations_json=[
                {"type": "archive_notice", "notice_id": notice.id, "expected_version": notice.version}
            ],
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
        )
        db.add(change_set)
        db.flush()
        _apply_in_session(db, user_id, actor_id, change_set, now, actor_type="user")
    _ = actor_id
    return change_set_public(change_set)


def create_event_patch_change_set(
    user_id: str,
    event_id: str,
    expected_version: int,
    patch: dict[str, Any],
    *, change_set_id: str | None = None, source_document_id: str | None = None, actor_type: str = "user",
) -> dict[str, Any]:
    """校验归属、版本与证据后直接修正通知事项，在同一事务保存撤销回执。"""
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        if change_set_id:
            previous = db.scalar(select(ChangeSet).where(ChangeSet.id == change_set_id, ChangeSet.user_id == user_id))
            if previous:
                return change_set_public(previous)
        event = _owned(db, Event, event_id, user_id)
        if event.version != expected_version or event.archived_at_epoch is not None:
            raise ApiProblem("version_conflict", "事项已变化，请刷新后重试。", 409)
        if not patch:
            raise ApiProblem("empty_patch", "至少需要修改一个字段。", 422)
        if "evidence" in patch:
            source = _owned(db, SourceDocument, source_document_id or event.source_document_id, user_id)
            evidence_only = NormalizedBundle.model_validate(
                {
                    "schema_version": "1.0",
                    # 证据核验也遵守完整包的通知关联约束，临时通知不写入数据库。
                    "notices": [{"temp_id": "patch-check-notice", "title": "核验证据", "source_ids": [source.id]}],
                    "events": [
                        {
                            "temp_id": "patch-check",
                            "notice_temp_id": "patch-check-notice",
                            "title": "核验证据",
                            "actor": None,
                            "stage": None,
                            "scope": None,
                            "deadline": None,
                            "time_range": None,
                            "materials": [],
                            "submission": None,
                            "eligibility": "unknown",
                            "evidence": patch["evidence"],
                            "unknowns": [],
                        }
                    ],
                    "schedule_entries": [],
                    "change_candidates": [],
                    "warnings": [],
                }
            )
            _verify_bundle_evidence(db, user_id=user_id, source=source, bundle=evidence_only)
        change_set = ChangeSet(
            id=change_set_id or str(uuid4()),
            user_id=user_id,
            source_document_id=source_document_id or event.source_document_id,
            kind="operations",
            status=None,
            summary=f"修正事项：{event.title[:450]}",
            bundle_json=None,
            operations_json=[
                {
                    "type": "patch_event",
                    "event_id": event.id,
                    "expected_version": expected_version,
                    "patch": patch,
                    "source_document_id": source_document_id or event.source_document_id,
                }
            ],
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
        )
        db.add(change_set)
        db.flush()
        # 修改通知事项没有预览/确认中间态；复用既有投影、审计和撤销逻辑。
        _apply_in_session(db, user_id, user_id, change_set, now, actor_type=actor_type)
    return change_set_public(change_set)


def _operation_snapshot(
    db: DbSession,
    *,
    user_id: str,
    actor_id: str,
    object_type: str,
    entity: Any,
    reason: str,
    source_id: str | None,
    now: int,
) -> dict[str, Any]:
    before = _snapshot(entity, object_type)
    entity.version += 1
    entity.updated_at_epoch = now
    _ = db, user_id, actor_id, reason, source_id
    return {
        "type": "snapshot",
        "object_type": object_type,
        "object_id": entity.id,
        "before": before,
        "applied_version": entity.version,
    }


def _apply_proposed_operations(
    db: DbSession,
    *,
    user_id: str,
    actor_id: str,
    change_set: ChangeSet,
    now: int,
    actor_type: str = "user",
) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for operation in change_set.operations_json:
        operation_type = operation.get("type")
        if operation_type == "archive_notice":
            notice = _owned(db, Notice, operation.get("notice_id", ""), user_id)
            if notice.version != operation.get("expected_version") or notice.archived_at_epoch is not None:
                raise ApiProblem("version_conflict", "通知已变化，请重新预览归档。", 409)
            record = _operation_snapshot(
                db,
                user_id=user_id,
                actor_id=actor_id,
                object_type="notice",
                entity=notice,
                reason="用户归档通知",
                source_id=notice.source_document_id,
                now=now,
            )
            notice.status = "archived"
            notice.archived_at_epoch = now
            # 修订快照反映最终状态，确保历史与当前版本一致。
            _revision(
                db,
                user_id=user_id,
                object_type="notice",
                object_id=notice.id,
                version=notice.version,
                data=_entity_data(notice, "notice"),
                source_document_id=notice.source_document_id,
                actor_type=actor_type,
                actor_id=actor_id,
                reason="用户归档通知",
                now=now,
            )
            applied.append(record)
        elif operation_type == "patch_event":
            event = _owned(db, Event, operation.get("event_id", ""), user_id)
            if event.version != operation.get("expected_version") or event.archived_at_epoch is not None:
                raise ApiProblem("version_conflict", "事项已变化，请重新预览修正。", 409)
            patch = operation.get("patch")
            if not isinstance(patch, dict):
                raise ApiProblem("invalid_change_set", "事项修正数据无效。", 409)
            source = _owned(db, SourceDocument, operation.get("source_document_id") or event.source_document_id, user_id)
            if "evidence" in patch:
                # 确认时重新验证引用，来源片段在确认前仍然需要保持归属。
                references = patch["evidence"]
                for grouped in references.values():
                    for reference in grouped:
                        segment = db.scalar(
                            select(SourceSegment).where(
                                SourceSegment.id == reference["segment_id"],
                                SourceSegment.user_id == user_id,
                                SourceSegment.source_document_id == source.id,
                            )
                        )
                        if segment is None or reference["quote"] not in segment.text:
                            raise ApiProblem("invalid_evidence", "证据必须引用当前材料中的精确文本。", 422)
            record = _operation_snapshot(
                db,
                user_id=user_id,
                actor_id=actor_id,
                object_type="event",
                entity=event,
                reason="用户修正事项",
                source_id=event.source_document_id,
                now=now,
            )
            # 新通知更新的来源也纳入快照，撤销时恢复旧来源与旧字段。
            event.source_document_id = source.id
            if actor_type == "user":
                # 手工修正是字段级个人覆盖；用户可在当前要求面板重新开启跟随。
                protected = {key: False for key in patch if key in {"deadline", "time_range", "materials", "submission"}}
                event.field_policies_json = {**event.field_policies_json, **protected}
            for key, value in patch.items():
                setattr(event, {"deadline": "deadline_json", "time_range": "time_range_json", "materials": "materials_json", "evidence": "evidence_json", "unknowns": "unknowns_json"}.get(key, key), value)
            _revision(
                db,
                user_id=user_id,
                object_type="event",
                object_id=event.id,
                version=event.version,
                data=_entity_data(event, "event"),
                source_document_id=source.id,
                actor_type=actor_type,
                actor_id=actor_id,
                reason="用户修正事项",
                now=now,
            )
            for task in db.scalars(select(Task).where(Task.user_id == user_id, Task.event_id == event.id, Task.status == "open")).all():
                task_record = _operation_snapshot(
                    db,
                    user_id=user_id,
                    actor_id=actor_id,
                    object_type="task",
                    entity=task,
                    reason="同步待办的开放事项修正",
                    source_id=source.id,
                    now=now,
                )
                task.title = event.title
                task.due_json = deepcopy(event.deadline_json)
                _revision(
                    db,
                    user_id=user_id,
                    object_type="task",
                    object_id=task.id,
                    version=task.version,
                    data=_entity_data(task, "task"),
                    source_document_id=source.id,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    reason="同步待办的开放事项修正",
                    now=now,
                )
                applied.append(task_record)
            applied.append(record)
        elif operation_type == "create_schedule_entry":
            try:
                payload = ScheduleEntryInput.model_validate(operation.get("entry"))
            except ValidationError:
                raise ApiProblem("invalid_change_set", "Agent 日程候选格式无效。", 409) from None
            entry, _conflicts = create_schedule_entry_in_session(db, user_id, payload, now_epoch=now)
            _revision(
                db,
                user_id=user_id,
                object_type="schedule_entry",
                object_id=entry.id,
                version=entry.version,
                data=_entity_data(entry, "schedule_entry"),
                source_document_id=None,
                actor_type=actor_type,
                actor_id=actor_id,
                reason="确认 Agent 新增个人日程",
                now=now,
            )
            applied.append(
                {"type": "created", "object_type": "schedule_entry", "object_id": entry.id, "applied_version": entry.version}
            )
        elif operation_type == "update_schedule_entry":
            try:
                entry_payload = operation.get("entry")
                expected_version = operation.get("expected_version")
                payload = UpdateScheduleEntryInput.model_validate(
                    {**entry_payload, "expected_version": expected_version}
                )
            except (TypeError, ValidationError):
                raise ApiProblem("invalid_change_set", "Agent 日程更改候选格式无效。", 409) from None
            entry = _owned(db, ScheduleEntry, operation.get("entry_id", ""), user_id)
            if entry.version != expected_version:
                raise ApiProblem("version_conflict", "日程已变化，请重新预览更改。", 409)
            before = _snapshot(entry, "schedule_entry")
            update_schedule_entry_in_session(
                db,
                user_id,
                entry.id,
                payload,
                now_epoch=now,
            )
            record = {
                "type": "snapshot",
                "object_type": "schedule_entry",
                "object_id": entry.id,
                "before": before,
                "applied_version": entry.version,
            }
            _revision(
                db,
                user_id=user_id,
                object_type="schedule_entry",
                object_id=entry.id,
                version=entry.version,
                data=_entity_data(entry, "schedule_entry"),
                source_document_id=None,
                actor_type=actor_type,
                actor_id=actor_id,
                reason="确认 Agent 修改个人日程",
                now=now,
            )
            applied.append(record)
        elif operation_type == "archive_schedule_entry":
            entry = _owned(db, ScheduleEntry, operation.get("entry_id", ""), user_id)
            expected_version = operation.get("expected_version")
            if entry.version != expected_version:
                raise ApiProblem("version_conflict", "日程已变化，请重新预览归档。", 409)
            before = _snapshot(entry, "schedule_entry")
            archive_schedule_entry_in_session(
                db,
                user_id,
                entry.id,
                expected_version,
                now_epoch=now,
            )
            record = {
                "type": "snapshot",
                "object_type": "schedule_entry",
                "object_id": entry.id,
                "before": before,
                "applied_version": entry.version,
            }
            _revision(
                db,
                user_id=user_id,
                object_type="schedule_entry",
                object_id=entry.id,
                version=entry.version,
                data=_entity_data(entry, "schedule_entry"),
                source_document_id=None,
                actor_type=actor_type,
                actor_id=actor_id,
                reason="确认 Agent 归档个人日程",
                now=now,
            )
            applied.append(record)
        elif operation_type == "restore_schedule_entry":
            entry = _owned(db, ScheduleEntry, operation.get("entry_id", ""), user_id)
            expected_version = operation.get("expected_version")
            if entry.version != expected_version:
                raise ApiProblem("version_conflict", "日程已变化，请重新预览恢复。", 409)
            before = _snapshot(entry, "schedule_entry")
            restore_schedule_entry_in_session(
                db,
                user_id,
                entry.id,
                expected_version,
                now_epoch=now,
            )
            record = {
                "type": "snapshot",
                "object_type": "schedule_entry",
                "object_id": entry.id,
                "before": before,
                "applied_version": entry.version,
            }
            _revision(
                db,
                user_id=user_id,
                object_type="schedule_entry",
                object_id=entry.id,
                version=entry.version,
                data=_entity_data(entry, "schedule_entry"),
                source_document_id=None,
                actor_type=actor_type,
                actor_id=actor_id,
                reason="确认 Agent 恢复个人日程",
                now=now,
            )
            applied.append(record)
        else:
            raise ApiProblem("invalid_change_set", "变更集操作类型不受支持。", 409)
    return applied


def undo_change_set(user_id: str, actor_id: str, change_set_id: str) -> dict[str, Any]:
    """按逆序撤销已确认变更；任何后续版本都会阻止覆盖新数据。"""
    from . import operation_service

    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        change_set = _owned(db, ChangeSet, change_set_id, user_id)
        if not change_applied(change_set):
            raise ApiProblem("change_not_undoable", "当前变更集不能撤销。", 409)
        if change_set.kind in {"requirements", "planning"}:
            # 新类型沿用公开撤销入口，仍在当前业务事务内执行。
            from .requirement_service import undo_in_session
            return undo_in_session(db, change_set, user_id)
        change_set.change_seq = _next_change_seq(db, user_id)
        for operation in reversed(change_set.operations_json):
            if operation.get("type") == "business_link_snapshot":
                # 关系与事务字段在同一事务内撤销；后续修改过的关系必须阻止整次回滚。
                link = _owned(db, BusinessLink, operation.get("object_id", ""), user_id)
                if link.version != operation.get("applied_version"):
                    raise ApiProblem("version_conflict", "关联关系在此变更后又有更新，不能安全撤销。", 409)
                before = {
                    "status": link.status,
                    "evidence_json": deepcopy(link.evidence_json),
                    "source_document_id": link.source_document_id,
                }
                previous = operation.get("before")
                if previous is None:
                    # 新建关系保留审计记录，仅撤销有效状态，不删除历史记录。
                    link.status = "revoked"
                else:
                    for field in ("status", "evidence_json", "source_document_id"):
                        setattr(link, field, deepcopy(previous[field]))
                link.version += 1
                link.updated_at_epoch = now
                operation_service.record_operation(
                    db, user_id=user_id, actor_type="user", actor_id=actor_id,
                    action="entity.link.undo", object_type="business_link", object_id=link.id,
                    change_set_id=change_set.id, source_document_id=change_set.source_document_id,
                    summary="撤销变更集中的关联关系", before=before,
                    after={"status": link.status, "evidence_json": deepcopy(link.evidence_json),
                           "source_document_id": link.source_document_id},
                    undoable=False, created_at_epoch=now,
                )
                continue
            if operation.get("type") == "course_series_created":
                series = _owned(db, CourseSeries, operation.get("object_id", ""), user_id)
                if series.version != operation.get("applied_version"):
                    raise ApiProblem("version_conflict", "课程系列在此变更后又有更新，不能安全撤销。", 409)
                before = _snapshot(series, "course_series")
                series.status = "archived"
                series.version += 1
                series.updated_at_epoch = now
                db.execute(
                    CourseOccurrence.__table__.update()
                    .where(
                        CourseOccurrence.user_id == user_id,
                        CourseOccurrence.course_series_id == series.id,
                        CourseOccurrence.status.in_(("scheduled", "rescheduled")),
                    )
                    .values(status="superseded", version=CourseOccurrence.version + 1)
                )
                _revision(
                    db,
                    user_id=user_id,
                    object_type="course_series",
                    object_id=series.id,
                    version=series.version,
                    data=_entity_data(series, "course_series"),
                    source_document_id=change_set.source_document_id,
                    actor_type="user",
                    actor_id=actor_id,
                    reason="撤销新增课程系列",
                    now=now,
                )
                operation_service.record_operation(
                    db,
                    user_id=user_id,
                    actor_type="user",
                    actor_id=actor_id,
                    action="course.undo",
                    object_type="course_series",
                    object_id=series.id,
                    change_set_id=change_set.id,
                    source_document_id=change_set.source_document_id,
                    summary=f"撤销新增课程：{series.display_name}",
                    before=before,
                    after=_entity_data(series, "course_series"),
                    undoable=False,
                    created_at_epoch=now,
                )
                continue
            if operation.get("type") == "course_series_snapshot":
                series = _owned(db, CourseSeries, operation.get("object_id", ""), user_id)
                if series.version != operation.get("applied_version"):
                    raise ApiProblem("version_conflict", "课程系列在此变更后又有更新，不能安全撤销。", 409)
                before = _snapshot(series, "course_series")
                restore_course_series_in_session(
                    db,
                    user_id=user_id,
                    series=series,
                    before=deepcopy(operation.get("before") or {}),
                    now=now,
                )
                _revision(
                    db,
                    user_id=user_id,
                    object_type="course_series",
                    object_id=series.id,
                    version=series.version,
                    data=_entity_data(series, "course_series"),
                    source_document_id=change_set.source_document_id,
                    actor_type="user",
                    actor_id=actor_id,
                    reason="撤销课程系列修改",
                    now=now,
                )
                operation_service.record_operation(
                    db,
                    user_id=user_id,
                    actor_type="user",
                    actor_id=actor_id,
                    action="course.undo",
                    object_type="course_series",
                    object_id=series.id,
                    change_set_id=change_set.id,
                    source_document_id=change_set.source_document_id,
                    summary=f"撤销课程系列修改：{series.display_name}",
                    before=before,
                    after=_entity_data(series, "course_series"),
                    undoable=False,
                    created_at_epoch=now,
                )
                continue
            if operation.get("type") == "course_exception_created":
                exception = _owned(db, CourseException, operation.get("object_id", ""), user_id)
                if exception.status != "active":
                    raise ApiProblem("version_conflict", "课程例外已撤销或变化。", 409)
                occurrence = _owned(db, CourseOccurrence, exception.occurrence_id, user_id)
                original = exception.original_json
                occurrence.local_date = original["local_date"]
                occurrence.display_start = original["display_start"]
                occurrence.display_end = original["display_end"]
                occurrence.busy_segments_json = deepcopy(original["busy_segments"])
                occurrence.room_override = original.get("room_override")
                occurrence.status = original["status"]
                occurrence.version += 1
                exception.status = "reverted"
                exception.version += 1
                operation_service.record_operation(
                    db,
                    user_id=user_id,
                    actor_type="user",
                    actor_id=actor_id,
                    action="course.undo",
                    object_type="course_occurrence",
                    object_id=occurrence.id,
                    change_set_id=change_set.id,
                    source_document_id=exception.source_document_id,
                    summary="撤销课程单次例外",
                    before=exception.replacement_json or {},
                    after=original,
                    undoable=False,
                    created_at_epoch=now,
                )
                continue
            object_type = operation["object_type"]
            model = _MODEL_BY_TYPE.get(object_type)
            if model is None:
                raise ApiProblem("invalid_change_set", "变更集包含不支持的对象。", 409)
            entity = _owned(db, model, operation["object_id"], user_id)
            if entity.version != operation["applied_version"]:
                raise ApiProblem("version_conflict", "对象在此变更后又有更新，不能安全撤销。", 409)
            if operation["type"] == "created":
                if object_type == "task" and entity.status != "open":
                    raise ApiProblem("version_conflict", "待办已完成或变化，不能撤销此变更集。", 409)
                before = _snapshot(entity, object_type)
                if object_type == "task":
                    entity.status = "cancelled"
                elif object_type == "course_occurrence":
                    # 临时补课撤销后保留实例与审计，不伪装成常规课程。
                    entity.status = "cancelled"
                else:
                    entity.status = "archived"
                    entity.archived_at_epoch = now
                entity.version += 1
                if hasattr(entity, "updated_at_epoch"):
                    entity.updated_at_epoch = now
                _revision(
                    db,
                    user_id=user_id,
                    object_type=object_type,
                    object_id=entity.id,
                    version=entity.version,
                    data=_entity_data(entity, object_type),
                    source_document_id=getattr(entity, "source_document_id", change_set.source_document_id),
                    actor_type="user",
                    actor_id=actor_id,
                    reason="撤销创建变更",
                    now=now,
                )
                _ = before
            elif operation["type"] == "snapshot":
                before = operation["before"]
                for key, value in before.items():
                    if key != "version":
                        setattr(entity, key, deepcopy(value))
                entity.version += 1
                if hasattr(entity, "updated_at_epoch"):
                    entity.updated_at_epoch = now
                _revision(
                    db,
                    user_id=user_id,
                    object_type=object_type,
                    object_id=entity.id,
                    version=entity.version,
                    data=_entity_data(entity, object_type),
                    source_document_id=getattr(entity, "source_document_id", change_set.source_document_id),
                    actor_type="user",
                    actor_id=actor_id,
                    reason="撤销变更集",
                    now=now,
                )
            else:
                raise ApiProblem("invalid_change_set", "变更集撤销记录无效。", 409)
        change_set.status = None if is_notification_change(change_set) else "undone"
        change_set.undone_at_epoch = now
        change_set.version += 1
        change_set.updated_at_epoch = now
    return change_set_public(change_set)


def set_task_status(
    user_id: str,
    actor_id: str,
    task_id: str,
    expected_version: int,
    status: str,
    *,
    actor_type: str = "user",
) -> dict[str, Any]:
    """在版本保护下完成或重新打开待办，并记录为可撤销变更集。"""
    from . import operation_service

    if status not in {"open", "completed", "cancelled"}:
        raise ValueError("unsupported task status")
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        task = _owned(db, Task, task_id, user_id)
        if task.version != expected_version or task.archived_at_epoch is not None:
            raise ApiProblem("version_conflict", "待办已变化，请刷新后重试。", 409)
        if task.status == status:
            return {"task": task_public(task), "change_set": None}
        change_set = ChangeSet(
            id=str(uuid4()),
            user_id=user_id,
            source_document_id=None,
            kind="operations",
            status="confirmed",
            summary="完成待办" if status == "completed" else "重新打开待办",
            bundle_json=None,
            operations_json=[],
            version=2,
            change_seq=_next_change_seq(db, user_id),
            created_at_epoch=now,
            updated_at_epoch=now,
            confirmed_at_epoch=now,
        )
        record = _operation_snapshot(
            db,
            user_id=user_id,
            actor_id=actor_id,
            object_type="task",
            entity=task,
            reason=change_set.summary,
            source_id=None,
            now=now,
        )
        task.status = status
        task.completed_at_epoch = now if status == "completed" else None
        _revision(
            db,
            user_id=user_id,
            object_type="task",
            object_id=task.id,
            version=task.version,
            data=_entity_data(task, "task"),
            source_document_id=None,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=change_set.summary,
            now=now,
        )
        change_set.operations_json = [record]
        db.add(change_set)
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=f"task.{status}",
            object_type="task",
            object_id=task.id,
            change_set_id=change_set.id,
            summary=change_set.summary,
            before=record["before"],
            after=_entity_data(task, "task"),
            undoable=True,
            created_at_epoch=now,
        )
    return {"task": task_public(task), "change_set": change_set_public(change_set)}


def notice_public(notice: Notice) -> dict[str, Any]:
    return {
        "id": notice.id,
        "source_document_id": notice.source_document_id,
        "title": clean_title(notice.title),
        "importance": notice.importance,
        "last_change_set_id": notice.last_change_set_id,
        "status": notice.status,
        "version": notice.version,
        "created_at_epoch": notice.created_at_epoch,
        "updated_at_epoch": notice.updated_at_epoch,
    }


def event_public(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "notice_id": event.notice_id,
        "source_document_id": event.source_document_id,
        "title": event.title,
        "actor": event.actor,
        "stage": event.stage,
        "scope": event.scope,
        "deadline": event.deadline_json,
        "time_range": event.time_range_json,
        "materials": event.materials_json,
        "submission": event.submission,
        "eligibility": event.eligibility,
        "evidence": event.evidence_json,
        "unknowns": event.unknowns_json,
        "status": event.status,
        "version": event.version,
        "updated_at_epoch": event.updated_at_epoch,
    }


def task_public(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "event_id": task.event_id,
        "source_document_id": task.source_document_id,
        "notice_id": task.notice_id,
        "course_series_id": task.course_series_id,
        "course_occurrence_id": task.course_occurrence_id,
        "title": clean_title(task.title, "未命名事务"),
        "description": clean_display_text(task.description, limit=4000) or None,
        "task_kind": task.task_kind,
        "priority": task.priority,
        "due": task.due_json,
        "original_requirement": task.original_requirement,
        "reminder_enabled": task.reminder_enabled,
        "status": task.status,
        "version": task.version,
        "updated_at_epoch": task.updated_at_epoch,
        "completed_at_epoch": task.completed_at_epoch,
    }


def list_change_sets(
    user_id: str,
    *,
    limit: int,
    offset: int,
    status: str | None,
    kind: str | None = None,
) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        query = select(ChangeSet).where(ChangeSet.user_id == user_id)
        count_query = select(func.count()).select_from(ChangeSet).where(ChangeSet.user_id == user_id)
        if status:
            query = query.where(ChangeSet.status == status)
            count_query = count_query.where(ChangeSet.status == status)
        if kind:
            # 通知收件箱只包含规范化候选，课程和日程操作不能污染待确认统计。
            query = query.where(ChangeSet.kind == kind)
            count_query = count_query.where(ChangeSet.kind == kind)
        rows = db.scalars(query.order_by(ChangeSet.created_at_epoch.desc()).limit(limit).offset(offset)).all()
        total = db.scalar(count_query) or 0
    return {"items": [change_set_public(row) for row in rows], "total": int(total), "limit": limit, "offset": offset}


def get_change_set(user_id: str, change_set_id: str) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        change_set = _owned(db, ChangeSet, change_set_id, user_id)
        source = None
        ocr_artifact = None
        segments: list[dict[str, Any]] = []
        if change_set.source_document_id:
            source = _owned(db, SourceDocument, change_set.source_document_id, user_id)
            ocr_artifact = db.scalar(
                select(OcrArtifact)
                .where(
                    OcrArtifact.user_id == user_id,
                    OcrArtifact.source_document_id == source.id,
                )
                .order_by(OcrArtifact.updated_at_epoch.desc())
            )
            segments = [
                {"id": item.id, "ordinal": item.ordinal, "text": item.text, "start_offset": item.start_offset, "end_offset": item.end_offset}
                for item in db.scalars(
                    select(SourceSegment)
                    .where(SourceSegment.user_id == user_id, SourceSegment.source_document_id == source.id)
                    .order_by(SourceSegment.ordinal)
                ).all()
            ]
        return {
            "change_set": change_set_public(change_set),
            "source": source_public(source) if source else None,
            "ocr_artifact": (
                {"id": ocr_artifact.id, "image_url": f"/api/v1/ocr/artifacts/{ocr_artifact.id}/image"}
                if ocr_artifact else None
            ),
            "segments": segments,
        }


def list_notices(
    user_id: str,
    *,
    limit: int,
    offset: int,
    status: str | None,
    query_text: str | None,
) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        query = select(Notice).where(Notice.user_id == user_id)
        count_query = select(func.count()).select_from(Notice).where(Notice.user_id == user_id)
        if status:
            query, count_query = query.where(Notice.status == status), count_query.where(Notice.status == status)
        if query_text:
            pattern = f"%{query_text[:100]}%"
            query, count_query = query.where(Notice.title.ilike(pattern)), count_query.where(Notice.title.ilike(pattern))
        rows = db.scalars(query.order_by(Notice.updated_at_epoch.desc()).limit(limit).offset(offset)).all()
        total = db.scalar(count_query) or 0
    return {"items": [notice_public(row) for row in rows], "total": int(total), "limit": limit, "offset": offset}


def get_notice(user_id: str, notice_id: str) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        notice = _owned(db, Notice, notice_id, user_id)
        source = _owned(db, SourceDocument, notice.source_document_id, user_id)
        events = db.scalars(
            select(Event).where(Event.user_id == user_id, Event.notice_id == notice.id).order_by(Event.created_at_epoch)
        ).all()
        tasks = db.scalars(
            select(Task).where(
                Task.user_id == user_id,
                Task.event_id.in_([item.id for item in events] or ["__none__"]),
            ).order_by(Task.created_at_epoch)
        ).all()
        revisions = db.scalars(
            select(Revision).where(
                Revision.user_id == user_id,
                Revision.object_type == "notice",
                Revision.object_id == notice.id,
            ).order_by(Revision.version)
        ).all()
        segments = db.scalars(
            select(SourceSegment).where(
                SourceSegment.user_id == user_id,
                SourceSegment.source_document_id == source.id,
            ).order_by(SourceSegment.ordinal)
        ).all()
        links = db.scalars(
            select(BusinessLink).where(
                BusinessLink.user_id == user_id,
                BusinessLink.status == "active",
                (
                    ((BusinessLink.from_type == "notice") & (BusinessLink.from_id == notice.id))
                    | ((BusinessLink.to_type == "notice") & (BusinessLink.to_id == notice.id))
                ),
            )
        ).all()
        linked_task_ids = {
            link.to_id if link.to_type == "task" else link.from_id
            for link in links
            if "task" in {link.from_type, link.to_type}
        }
        if linked_task_ids:
            tasks = db.scalars(
                select(Task).where(
                    Task.user_id == user_id,
                    Task.id.in_(linked_task_ids | {item.id for item in tasks}),
                ).order_by(Task.created_at_epoch)
            ).all()
        courses: list[dict[str, Any]] = []
        seen_courses: set[str] = set()
        for link in links:
            entity_type = link.to_type if link.from_type == "notice" else link.from_type
            entity_id = link.to_id if link.from_type == "notice" else link.from_id
            occurrence = db.scalar(
                select(CourseOccurrence).where(
                    CourseOccurrence.id == entity_id,
                    CourseOccurrence.user_id == user_id,
                )
            ) if entity_type == "course_occurrence" else None
            series = (
                db.scalar(
                    select(CourseSeries).where(
                        CourseSeries.id == (occurrence.course_series_id if occurrence else entity_id),
                        CourseSeries.user_id == user_id,
                    )
                )
                if entity_type in {"course", "course_occurrence"}
                else None
            )
            # 通知也会关联任务；非课程关系没有 series，必须在拼接课程键前跳过，
            # 否则任务关系会让整个通知详情接口返回 500。
            if series is None:
                continue
            course_key = f"{series.id}:{occurrence.id if occurrence else 'series'}"
            if course_key in seen_courses:
                continue
            seen_courses.add(course_key)
            courses.append(
                {
                    "course_series_id": series.id,
                    "occurrence_id": occurrence.id if occurrence else None,
                    "title": series.display_name,
                    "local_date": occurrence.local_date if occurrence else None,
                    "start": occurrence.display_start if occurrence else None,
                    "end": occurrence.display_end if occurrence else None,
                    "room": (occurrence.room_override if occurrence and occurrence.room_override else series.room),
                    "relation": link.relation,
                }
            )
        operations = db.scalars(
            select(OperationLog).where(
                OperationLog.user_id == user_id,
                OperationLog.object_id == notice.id,
            ).order_by(OperationLog.created_at_epoch.desc()).limit(30)
        ).all()
        return {
            "notice": notice_public(notice),
            "source": source_public(source),
            "ocr_artifact": (
                {
                    "id": ocr_artifact.id,
                    "image_url": f"/api/v1/ocr/artifacts/{ocr_artifact.id}/image",
                }
                if (ocr_artifact := db.scalar(
                    select(OcrArtifact)
                    .where(
                        OcrArtifact.user_id == user_id,
                        OcrArtifact.source_document_id == source.id,
                    )
                    .order_by(OcrArtifact.updated_at_epoch.desc())
                ))
                else None
            ),
            "segments": [
                {"id": item.id, "ordinal": item.ordinal, "text": item.text, "start_offset": item.start_offset, "end_offset": item.end_offset}
                for item in segments
            ],
            "events": [event_public(item) for item in events],
            "tasks": [task_public(item) for item in tasks],
            "links": [
                {
                    "id": link.id,
                    "from_type": link.from_type,
                    "from_id": link.from_id,
                    "to_type": link.to_type,
                    "to_id": link.to_id,
                    "relation": link.relation,
                    "status": link.status,
                    "version": link.version,
                }
                for link in links
            ],
            "courses": courses,
            "operations": [
                {
                    "id": item.id,
                    "actor_type": item.actor_type,
                    "action": item.action,
                    "summary": item.summary,
                    "change_set_id": item.change_set_id,
                    "created_at_epoch": item.created_at_epoch,
                }
                for item in operations
            ],
            "revisions": [
                {"version": item.version, "data": item.data_json, "reason": item.reason, "created_at_epoch": item.created_at_epoch}
                for item in revisions
            ],
        }


def list_events(user_id: str, *, limit: int, offset: int, status: str | None) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        query = select(Event).where(Event.user_id == user_id)
        count_query = select(func.count()).select_from(Event).where(Event.user_id == user_id)
        if status:
            query, count_query = query.where(Event.status == status), count_query.where(Event.status == status)
        rows = db.scalars(query.order_by(Event.updated_at_epoch.desc()).limit(limit).offset(offset)).all()
        total = db.scalar(count_query) or 0
    return {"items": [event_public(row) for row in rows], "total": int(total), "limit": limit, "offset": offset}


def list_tasks(user_id: str, *, limit: int, offset: int, status: str | None) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        query = select(Task).where(Task.user_id == user_id)
        count_query = select(func.count()).select_from(Task).where(Task.user_id == user_id)
        if status:
            query, count_query = query.where(Task.status == status), count_query.where(Task.status == status)
        rows = db.scalars(query.order_by(Task.updated_at_epoch.desc()).limit(limit).offset(offset)).all()
        total = db.scalar(count_query) or 0
        items = []
        for row in rows:
            public = task_public(row)
            notice = db.get(Notice, row.notice_id) if row.notice_id else None
            series = db.get(CourseSeries, row.course_series_id) if row.course_series_id else None
            occurrence = db.get(CourseOccurrence, row.course_occurrence_id) if row.course_occurrence_id else None
            public["notice_title"] = notice.title if notice else None
            public["course_title"] = series.display_name if series else None
            public["course_date"] = occurrence.local_date if occurrence else None
            items.append(public)
    return {"items": items, "total": int(total), "limit": limit, "offset": offset}


def handle_legacy_notification(claim):
    """升级前未处理通知由本地队列直接落库；不重新请求收费模型或等待人工确认。"""
    identifier = claim.payload["change_id"]
    with DbSession(_engine()) as db, db.begin():
        change = _owned(db, ChangeSet, identifier, claim.user_id)
        # 旧跨模块流程可能已建业务对象而尚未消费提案，升级不能重复创建。
        existing_notice = db.scalar(select(Notice.id).where(Notice.user_id == claim.user_id, Notice.source_document_id == change.source_document_id).limit(1))
        if change_unapplied(change) and existing_notice:
            change.confirmed_at_epoch = _now()
            change.updated_at_epoch = change.confirmed_at_epoch
            change.status = None
            source = _owned(db, SourceDocument, change.source_document_id, claim.user_id)
            source.analysis_state = "processed"
            return change_set_public(change)
        version = change.version
    return apply_change_set(claim.user_id, claim.user_id, identifier, version, actor_type="system")
