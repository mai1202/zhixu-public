"""有限 Agent 工具循环：查询真实日程，写入只生成需确认的 ChangeSet。"""

from datetime import date, datetime, time, timedelta
import json
import re
import time as clock
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..jobs.queue import enqueue_job
from ..models import AgentAttachment, AgentConversation, AgentPreference, AgentTurn, ChangeSet, Job, ScheduleEntry
from ..schemas.agent import AgentIntent, AgentTurnInput
from ..schemas.timetable import ScheduleEntryInput
from . import agent_memory_service, business_service, timetable_service

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _engine():
    return current_app.extensions["notification_db_engine"]


def _schedule_turn_index(user_id: str, turn_id: str | None) -> None:
    """向量通道可用时把对话轮次纳入语义召回；失败不影响对话发送。"""
    if not turn_id:
        return
    try:
        from .retrieval_service import schedule_vector_index
        schedule_vector_index(user_id, "conversation", turn_id)
    except Exception:
        current_app.logger.warning("对话向量索引调度失败 turn_id=%s", turn_id)


def submit_turn(user_id: str, payload: AgentTurnInput) -> dict[str, Any]:
    """保存用户输入并入持久队列，客户端可用 run_id 查询或取消任务。"""
    engine = _engine()
    now = int(clock.time())
    conversation_id = str(payload.conversation_id) if payload.conversation_id else None
    idempotency_key = str(payload.idempotency_key)
    from .agent_material_service import material_info
    attachments = [material_info(user_id, str(identifier)) for identifier in payload.attachment_ids]
    if any(item["status"] != "ready" for item in attachments):
        raise ApiProblem("attachment_not_ready", "图片仍在识别或识别失败，请等附件显示就绪后发送。", 409)

    # 重复提交时沿用原 job 的 conversation_id，使同一幂等键输入哈希稳定。
    with DbSession(engine) as db:
        previous_job = db.scalar(
            select(Job).where(
                Job.user_id == user_id,
                Job.kind == "agent.run",
                Job.idempotency_key == idempotency_key,
            )
        )
        if previous_job is not None and conversation_id is None:
            conversation_id = str(previous_job.payload_json.get("conversation_id", ""))

    if conversation_id:
        with DbSession(engine) as db, db.begin():
            conversation = db.scalar(
                select(AgentConversation)
                .where(AgentConversation.id == conversation_id, AgentConversation.user_id == user_id)
                .with_for_update()
            )
            if conversation is None:
                raise ApiProblem("not_found", "未找到该对话。", 404)
            if conversation.status != "active":
                raise ApiProblem("conversation_archived", "该对话已归档，不能继续发送。", 409)
    else:
        # 先建立独立固定的主助手；用户新聊天和定时子对话均为普通会话。
        from .main_assistant_service import main_conversation
        main_conversation(user_id)
        conversation_id = str(uuid4())
        with DbSession(engine) as db, db.begin():
            db.add(
                AgentConversation(
                    id=conversation_id,
                    user_id=user_id,
                    title=payload.message[:120],
                    status="active",
                    version=1,
                    created_at_epoch=now,
                    updated_at_epoch=now,
                    archived_at_epoch=None,
                )
            )
            # 新聊天只保存自己的消息，不改变后台通知和主动服务的归属。
            db.flush()

    job_payload = {
        "conversation_id": conversation_id,
        "message": payload.message,
        "profile_id": str(payload.profile_id) if payload.profile_id else None,
        "thinking_level": payload.thinking_level,
        "idempotency_key": idempotency_key,
        "attachment_ids": [str(identifier) for identifier in payload.attachment_ids],
        "page_context": payload.page_context,
    }
    job = enqueue_job(
        engine,
        user_id=user_id,
        kind="agent.run",
        payload=job_payload,
        idempotency_key=idempotency_key,
        max_attempts=3,
        priority=90,
    )
    # 记忆兜底不阻塞对话：识别失败时仍正常入队，由模型在运行中继续判断。
    try:
        agent_memory_service.auto_remember_from_message(user_id, conversation_id, payload.message)
    except Exception:
        pass
    new_turn_id: str | None = None
    with DbSession(engine) as db, db.begin():
        conversation = db.scalar(
            select(AgentConversation)
            .where(AgentConversation.id == conversation_id, AgentConversation.user_id == user_id)
            .with_for_update()
        )
        if conversation is None:
            raise ApiProblem("not_found", "未找到该对话。", 404)
        existing_turn = db.scalar(
            select(AgentTurn).where(
                AgentTurn.conversation_id == conversation_id,
                AgentTurn.run_id == job.id,
                AgentTurn.role == "user",
            )
        )
        if existing_turn is None:
            sequence = conversation.version
            turn = AgentTurn(
                id=str(uuid4()),
                user_id=user_id,
                conversation_id=conversation_id,
                run_id=job.id,
                sequence=sequence,
                role="user",
                content=payload.message,
                metadata_json={"attachments": [{key: item[key] for key in ("id", "filename", "image_url", "total_characters")} for item in attachments]},
                created_at_epoch=now,
            )
            db.add(turn)
            new_turn_id = turn.id
            conversation.version += 1
            conversation.updated_at_epoch = now
    _schedule_turn_index(user_id, new_turn_id)
    return {"conversation_id": conversation_id, "run_id": job.id, "status": job.status}


def _conversation_public(conversation: AgentConversation, *, is_main: bool = False) -> dict[str, Any]:
    """统一对话摘要字段，归档状态由 owner 接口返回给页面。"""
    return {
        "id": conversation.id,
        "is_main": is_main,
        "title": conversation.title,
        "status": conversation.status,
        "version": conversation.version,
        "updated_at_epoch": conversation.updated_at_epoch,
        "archived_at_epoch": conversation.archived_at_epoch,
    }


def list_conversations(user_id: str, *, limit: int = 50, status: str = "active") -> dict[str, Any]:
    """按状态列出当前用户的对话；归档对话保留原文但默认不混入历史列表。"""
    if status not in {"active", "archived"}:
        raise ApiProblem("invalid_conversation_status", "对话状态筛选值无效。", 422)
    with DbSession(_engine()) as db:
        preference = db.get(AgentPreference, user_id)
        main_id = preference.main_conversation_id if preference else None
        order_column = AgentConversation.archived_at_epoch if status == "archived" else AgentConversation.updated_at_epoch
        rows = db.scalars(
            select(AgentConversation)
            .where(AgentConversation.user_id == user_id, AgentConversation.status == status)
            .order_by(order_column.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
    return {"items": [_conversation_public(row, is_main=row.id == main_id) for row in sorted(rows, key=lambda row: row.id != main_id)]}


def archive_conversation(user_id: str, conversation_id: str, expected_version: int) -> dict[str, Any]:
    """归档普通聊天，不删除消息、工具记录或长期记忆；固定主助手保留。"""
    now = int(clock.time())
    with DbSession(_engine()) as db, db.begin():
        conversation = db.scalar(
            select(AgentConversation)
            .where(
                AgentConversation.id == conversation_id,
                AgentConversation.user_id == user_id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise ApiProblem("not_found", "未找到该对话。", 404)
        preference = db.get(AgentPreference, user_id)
        if preference and preference.main_conversation_id == conversation_id:
            raise ApiProblem('main_conversation_fixed', '主助手固定保留；其他聊天可以归档。', 409)
        if conversation.version != expected_version:
            raise ApiProblem("version_conflict", "对话已变化，请刷新后重试。", 409)
        if conversation.status == "archived":
            return _conversation_public(conversation)
        # 运行中的对话先要求等待结束，避免归档后仍追加新的助手回复。
        active_job = db.scalar(
            select(Job.id).where(
                Job.user_id == user_id,
                Job.kind == "agent.run",
                Job.payload_json["conversation_id"].as_string() == conversation_id,
                Job.status.in_(("queued", "retry_wait", "leased", "running")),
            ).limit(1)
        )
        if active_job is not None:
            raise ApiProblem("conversation_busy", "本轮助手仍在处理，请完成或停止后再归档。", 409)
        conversation.status = "archived"
        conversation.archived_at_epoch = now
        conversation.updated_at_epoch = now
        conversation.version += 1
        preference = db.get(AgentPreference, user_id)
        if preference is not None and preference.main_conversation_id == conversation_id:
            preference.main_conversation_id = None
        db.flush()
        return _conversation_public(conversation)


def restore_conversation(user_id: str, conversation_id: str, expected_version: int) -> dict[str, Any]:
    """恢复本人普通归档聊天，不改变固定主助手指针。"""
    now = int(clock.time())
    with DbSession(_engine()) as db, db.begin():
        conversation = db.scalar(
            select(AgentConversation)
            .where(
                AgentConversation.id == conversation_id,
                AgentConversation.user_id == user_id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise ApiProblem("not_found", "未找到该对话。", 404)
        if conversation.version != expected_version:
            raise ApiProblem("version_conflict", "对话已变化，请刷新后重试。", 409)
        if conversation.status == "active":
            return _conversation_public(conversation)
        conversation.status = "active"
        conversation.archived_at_epoch = None
        conversation.updated_at_epoch = now
        conversation.version += 1
        db.flush()
        return _conversation_public(conversation)


def get_conversation(user_id: str, conversation_id: str, after_sequence: int | None = None) -> dict[str, Any]:
    """按 owner 取回对话；带 after_sequence 时只返回新增轮次。"""
    with DbSession(_engine()) as db:
        conversation = db.scalar(
            select(AgentConversation).where(
                AgentConversation.id == conversation_id,
                AgentConversation.user_id == user_id,
            )
        )
        if conversation is None:
            raise ApiProblem("not_found", "未找到该对话。", 404)
        turns = db.scalars(
            select(AgentTurn)
            .where(AgentTurn.conversation_id == conversation.id, AgentTurn.user_id == user_id)
            .order_by(AgentTurn.sequence)
        ).all()
        preference = db.get(AgentPreference, user_id)
        is_main = bool(preference and preference.main_conversation_id == conversation_id)
        active_run_id = db.scalar(select(Job.id).where(Job.user_id == user_id, Job.kind == "agent.run",
                                      Job.payload_json["conversation_id"].as_string() == conversation_id,
                                      Job.status.in_(("queued", "retry_wait", "leased", "running"))).order_by(Job.created_at_epoch).limit(1))
        # 刷新后仍提供失败运行的重试入口；成功或取消的旧运行不持续提示。
        last_failed_run_id = db.scalar(select(Job.id).where(
            Job.user_id == user_id,
            Job.kind == "agent.run",
            Job.payload_json["conversation_id"].as_string() == conversation_id,
            Job.status.in_(("failed", "expired")),
        ).order_by(Job.created_at_epoch.desc()).limit(1))
        # 变更状态读取当前值，避免刷新后又显示旧快照中的“待确认”。
        change_ids = [turn.metadata_json["change_set"]["id"] for turn in turns if isinstance(turn.metadata_json.get("change_set"), dict)]
        live_changes = {change.id: business_service.change_set_public(change) for change in db.scalars(select(ChangeSet).where(ChangeSet.user_id == user_id, ChangeSet.id.in_(change_ids))).all()}
    latest_sequence = max((turn.sequence for turn in turns), default=0)
    visible_turns = turns if after_sequence is None else [turn for turn in turns if turn.sequence > after_sequence]
    return {
        "active_run_id": active_run_id,
        "last_failed_run_id": last_failed_run_id,
        "latest_sequence": latest_sequence,
        "reset_required": after_sequence is not None and after_sequence > latest_sequence,
        "change_sets": live_changes,
        "conversation": {
            "id": conversation.id,
            "is_main": is_main,
            "title": conversation.title,
            "status": conversation.status,
            "version": conversation.version,
            "updated_at_epoch": conversation.updated_at_epoch,
            "archived_at_epoch": conversation.archived_at_epoch,
            "memory": conversation.memory_json,
        },
        "turns": [
            {
                "id": turn.id,
                "run_id": turn.run_id,
                "sequence": turn.sequence,
                "role": turn.role,
                "content": turn.content,
                "metadata": {**turn.metadata_json, **({"change_set": live_changes[turn.metadata_json["change_set"]["id"]]} if isinstance(turn.metadata_json.get("change_set"), dict) and turn.metadata_json["change_set"]["id"] in live_changes else {})},
                "created_at_epoch": turn.created_at_epoch,
            }
            for turn in visible_turns
        ],
    }


def get_run(user_id: str, run_id: str) -> dict[str, Any]:
    """按 owner 查询持久运行状态和最终结果。"""
    with DbSession(_engine()) as db:
        job = db.scalar(select(Job).where(Job.id == run_id, Job.user_id == user_id, Job.kind == "agent.run"))
    if job is None:
        raise ApiProblem("not_found", "未找到该 Agent 运行。", 404)
    return {
        "id": job.id,
        "conversation_id": job.payload_json.get("conversation_id"),
        "status": job.status,
        "result": job.result_json,
        "error_code": job.error_code,
        "attempt": job.attempt,
        "created_at_epoch": job.created_at_epoch,
        "updated_at_epoch": job.updated_at_epoch,
    }


def _turn_public(turn: AgentTurn) -> dict[str, Any]:
    return {
        "id": turn.id,
        "run_id": turn.run_id,
        "sequence": turn.sequence,
        "role": turn.role,
        "content": turn.content,
        "metadata": turn.metadata_json,
        "created_at_epoch": turn.created_at_epoch,
    }


def _append_assistant_turn(
    db: DbSession,
    *,
    user_id: str,
    conversation: AgentConversation,
    run_id: str,
    content: str,
    metadata: dict[str, Any],
    now: int,
) -> AgentTurn:
    """在同一事务中保存助手结果和会话版本。"""
    turn = AgentTurn(
        id=str(uuid4()),
        user_id=user_id,
        conversation_id=conversation.id,
        run_id=run_id,
        sequence=conversation.version,
        role="assistant",
        content=content,
        metadata_json=metadata,
        created_at_epoch=now,
    )
    db.add(turn)
    conversation.version += 1
    conversation.updated_at_epoch = now
    return turn


def _matching_schedule_entry(
    db: DbSession,
    user_id: str,
    title: str,
    *,
    status: str = "active",
) -> list[ScheduleEntry]:
    """只按用户可见标题精确匹配，不采用模型提供的任意对象 ID。"""
    entries = db.scalars(
        select(ScheduleEntry)
        .where(ScheduleEntry.user_id == user_id, ScheduleEntry.status == status)
        .order_by(ScheduleEntry.local_date, ScheduleEntry.start_at)
    ).all()
    return [entry for entry in entries if entry.title.strip().casefold() == title.strip().casefold()]


def _format_agenda(user_id: str, intent: AgentIntent, *, free_slots: bool) -> tuple[str, dict[str, Any]]:
    today = datetime.now(SHANGHAI).date()
    start_date = intent.start_date or today
    end_date = intent.end_date or start_date
    data = timetable_service.list_schedule(user_id, start_date, end_date)
    if free_slots:
        if start_date != end_date:
            return "查空闲时间一次只看一天。请指定具体日期。", {"action": "clarify"}
        zone = ZoneInfo(intent.timezone)
        lower = datetime.combine(start_date, time(8, 0), tzinfo=zone)
        upper = datetime.combine(start_date, time(22, 0), tzinfo=zone)
        occupied: list[tuple[datetime, datetime]] = []
        for item in data["items"]:
            for segment in item.get("busy_segments", []):
                busy_start = datetime.fromisoformat(segment["start_at"])
                busy_end = datetime.fromisoformat(segment["end_at"])
                if busy_start.date() == start_date:
                    occupied.append((max(lower, busy_start), min(upper, busy_end)))
        occupied.sort(key=lambda item: item[0])
        merged: list[tuple[datetime, datetime]] = []
        for busy_start, busy_end in occupied:
            if busy_end <= busy_start:
                continue
            if merged and busy_start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], busy_end))
            else:
                merged.append((busy_start, busy_end))
        gaps: list[tuple[datetime, datetime]] = []
        cursor = lower
        for busy_start, busy_end in merged:
            if busy_start - cursor >= timedelta(minutes=30):
                gaps.append((cursor, busy_start))
            cursor = max(cursor, busy_end)
        if upper - cursor >= timedelta(minutes=30):
            gaps.append((cursor, upper))
        slots = [{"start_at": start.isoformat(), "end_at": end.isoformat()} for start, end in gaps]
        if not slots:
            answer = f"{start_date.isoformat()} 在 08:00–22:00 范围内没有 30 分钟以上的空档。"
        else:
            answer = f"{start_date.isoformat()} 的 08:00–22:00 范围内，按现有课程和忙碌日程计算的空档（未加缓冲）：" + "；".join(
                f"{datetime.fromisoformat(slot['start_at']).strftime('%H:%M')}–{datetime.fromisoformat(slot['end_at']).strftime('%H:%M')}"
                for slot in slots
            )
        return answer, {"action": "free_slots", "date": start_date.isoformat(), "slots": slots, "buffer_minutes": 0}

    items = [item for item in data["items"] if start_date.isoformat() <= item.get("local_date", "") <= end_date.isoformat()]
    deadlines = [item for item in data["deadlines"] if start_date.isoformat() <= item["local_date"] <= end_date.isoformat()]
    if not items and not deadlines:
        answer = f"{start_date.isoformat()} 至 {end_date.isoformat()} 没有已登记课程、日程或待办截止。"
    else:
        lines = []
        for item in items:
            period = f" {item['start_at']}–{item['end_at']}" if item.get("start_at") and item.get("end_at") else ""
            lines.append(f"{item.get('local_date', '')}{period} · {item['title']}")
        for item in deadlines:
            lines.append(f"{item['local_date']} 截止 · {item['title']}（精度：{item['precision']}）")
        answer = f"{start_date.isoformat()} 至 {end_date.isoformat()} 的记录：\n" + "\n".join(lines)
    return answer, {"action": "agenda", "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "schedule": items, "deadlines": deadlines}


def _propose_schedule_change(
    db: DbSession,
    *,
    user_id: str,
    conversation_id: str,
    run_id: str,
    intent: AgentIntent,
    now: int,
    change_set_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """根据有限意图读取真实对象并建立需确认的 ChangeSet。"""
    operation: dict[str, Any]
    summary: str
    # Pi 重试同一工具调用时复用预览，不生成重复候选。
    if change_set_id:
        previous = db.scalar(select(ChangeSet).where(ChangeSet.id == change_set_id, ChangeSet.user_id == user_id))
        if previous:
            return "该工具调用已生成变更预览。", {"action": intent.action, "change_set": business_service.change_set_public(previous)}
    if intent.action == "create_schedule":
        payload = ScheduleEntryInput(
            title=intent.title or "",
            kind=intent.kind or "busy_block",
            local_date=intent.local_date,
            start_at=intent.start_at,
            end_at=intent.end_at,
            timezone=intent.timezone,
            source_event_id=None,
            allow_conflict=False,
        )
        conflicts = timetable_service.preview_schedule_entry_conflicts(user_id, payload)
        if conflicts:
            titles = "、".join(sorted({item["title"] for item in conflicts}))
            return f"这个时段与已有安排冲突（{titles}）。我没有创建候选；请提供其他时间。", {"action": intent.action, "conflicts": conflicts}
        entry = payload.model_dump(mode="json")
        operation = {"type": "create_schedule_entry", "entry": entry}
        summary = f"新增个人日程：{payload.title}"
    elif intent.action == "update_schedule":
        matches = _matching_schedule_entry(db, user_id, intent.target_title or "")
        if len(matches) != 1:
            candidates = [{"title": item.title, "local_date": item.local_date, "start_at": item.start_at} for item in matches]
            if not matches:
                return "没有找到标题完全匹配的活动。请提供现有日程的准确标题。", {"action": intent.action, "candidates": []}
            return "有多个同名日程，我无法判断要修改哪一个。请补充日期。", {"action": intent.action, "candidates": candidates}
        current = matches[0]
        payload = ScheduleEntryInput(
            title=intent.title or current.title,
            kind=intent.kind or current.kind,
            local_date=intent.local_date,
            start_at=intent.start_at,
            end_at=intent.end_at,
            timezone=intent.timezone or current.timezone,
            source_event_id=current.source_event_id,
            allow_conflict=False,
        )
        conflicts = timetable_service.preview_schedule_entry_conflicts(user_id, payload, exclude_entry_id=current.id)
        if conflicts:
            titles = "、".join(sorted({item["title"] for item in conflicts}))
            return f"修改后的时段与已有安排冲突（{titles}）。我没有建立变更候选；请提供其他时间。", {"action": intent.action, "conflicts": conflicts}
        operation = {
            "type": "update_schedule_entry",
            "entry_id": current.id,
            "expected_version": current.version,
            "entry": payload.model_dump(mode="json"),
        }
        summary = f"修改个人日程：{current.title} → {payload.title}"
    elif intent.action == "archive_schedule":
        matches = _matching_schedule_entry(db, user_id, intent.target_title or "")
        if len(matches) != 1:
            candidates = [{"title": item.title, "local_date": item.local_date, "start_at": item.start_at} for item in matches]
            if not matches:
                return "没有找到标题完全匹配的活动。请提供现有日程的准确标题。", {"action": intent.action, "candidates": []}
            return "有多个同名日程，我无法判断要归档哪一个。请补充日期。", {"action": intent.action, "candidates": candidates}
        current = matches[0]
        operation = {
            "type": "archive_schedule_entry",
            "entry_id": current.id,
            "expected_version": current.version,
            "preview": {
                "title": current.title,
                "local_date": current.local_date,
                "start_at": current.start_at,
                "end_at": current.end_at,
            },
        }
        summary = f"归档个人日程：{current.title}"
    else:
        matches = _matching_schedule_entry(
            db,
            user_id,
            intent.target_title or "",
            status="archived",
        )
        if len(matches) != 1:
            candidates = [
                {"title": item.title, "local_date": item.local_date, "start_at": item.start_at}
                for item in matches
            ]
            if not matches:
                return "没有找到已归档且标题完全匹配的活动。请提供准确标题。", {"action": intent.action, "candidates": []}
            return "有多个同名归档日程，我无法判断要恢复哪一个。请先在工作台按日期选择。", {"action": intent.action, "candidates": candidates}
        current = matches[0]
        payload = ScheduleEntryInput(
            title=current.title,
            kind=current.kind,
            local_date=date.fromisoformat(current.local_date),
            start_at=datetime.fromisoformat(current.start_at) if current.start_at else None,
            end_at=datetime.fromisoformat(current.end_at) if current.end_at else None,
            timezone=current.timezone,
            source_event_id=current.source_event_id,
            allow_conflict=False,
        )
        conflicts = timetable_service.preview_schedule_entry_conflicts(
            user_id,
            payload,
            exclude_entry_id=current.id,
        )
        if conflicts:
            return "恢复这段日程会与现有安排冲突，请先调整原时段或明确允许重叠。", {
                "action": intent.action,
                "conflicts": conflicts,
            }
        operation = {
            "type": "restore_schedule_entry",
            "entry_id": current.id,
            "expected_version": current.version,
            "preview": {
                "title": current.title,
                "local_date": current.local_date,
                "start_at": current.start_at,
                "end_at": current.end_at,
            },
        }
        summary = f"恢复个人日程：{current.title}"

    change_set = ChangeSet(
        id=change_set_id or str(uuid4()),
        user_id=user_id,
        source_document_id=None,
        kind="agent_schedule",
        status="proposed",
        summary=summary,
        bundle_json={"origin_run_id": run_id, "preview": operation},
        operations_json=[operation],
        version=1,
        created_at_epoch=now,
        updated_at_epoch=now,
        confirmed_at_epoch=None,
        undone_at_epoch=None,
    )
    db.add(change_set)
    change = business_service.change_set_public(change_set)
    if intent.action == "create_schedule":
        answer = f"我准备创建“{intent.title}”，时间为 {intent.local_date}。确认后才会写入日程。"
    elif intent.action == "update_schedule":
        answer = f"我准备修改“{intent.target_title}”的日程。确认后才会更新。"
    elif intent.action == "archive_schedule":
        answer = f"我准备归档“{intent.target_title}”。这会软归档且可撤销，确认后才会生效。"
    else:
        answer = f"我准备恢复“{intent.target_title}”这段已归档日程。确认后才会重新出现在日历中。"
    return answer, {"action": intent.action, "change_set": change}


def handle_agent_job(claim) -> dict[str, Any]:
    """worker 的固定 Agent 入口；工具集合不含 SQL、shell、微信或任意 URL。"""
    # 执行、持久会话、流式工具循环与自动压缩统一交由 Pi SDK。
    from .pi_bridge_service import run_claim
    return run_claim(claim)
