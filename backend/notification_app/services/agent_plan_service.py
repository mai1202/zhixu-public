"""复杂请求的持久短计划：记录步骤、对象版本与工具回执，恢复时避免重复写入。"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from uuid import uuid4

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..models import AgentExecutionPlan

STEP_STATUSES = {"pending", "running", "done", "failed", "waiting"}
PLAN_STATUSES = {"active", "completed", "failed", "cancelled"}
RESUMABLE_STATUSES = ("active", "failed")
MAX_STEPS = 12
MAX_RECEIPTS = 50
MAX_RECEIPT_RESULT_CHARS = 12_000
RECEIPT_TTL_SECONDS = 7 * 24 * 60 * 60
NEXT_STEP_PRIORITY = {"running": 0, "failed": 1, "waiting": 2, "pending": 3}


def _engine():
    return current_app.extensions["notification_db_engine"]


def _next_step(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        step for step in steps
        if step.get("status") in NEXT_STEP_PRIORITY
    ]
    return min(
        candidates,
        key=lambda step: (NEXT_STEP_PRIORITY.get(str(step.get("status")), 99), int(step.get("index", 0))),
        default=None,
    )


def _receipt_public(receipt: dict[str, Any], *, include_result: bool) -> dict[str, Any]:
    item = {
        "tool_name": receipt.get("tool_name"),
        "input_hash": receipt.get("input_hash"),
        "tool_call_id": receipt.get("tool_call_id"),
        "created_at_epoch": receipt.get("created_at_epoch"),
    }
    if include_result:
        item["result"] = receipt.get("result")
    return item


def _public(item: AgentExecutionPlan, *, include_receipts: bool = False) -> dict[str, Any]:
    steps = [dict(step) for step in (item.steps_json or [])]
    receipts = [dict(receipt) for receipt in (item.tool_receipts_json or [])]
    completed_count = sum(1 for step in steps if step.get("status") == "done")
    next_step = _next_step(steps)
    return {
        "id": item.id,
        "conversation_id": item.conversation_id,
        "run_id": item.run_id,
        "goal": item.goal,
        "status": item.status,
        "steps": steps,
        "next_step": next_step,
        "completed_count": completed_count,
        "step_count": len(steps),
        "receipt_count": len(receipts),
        "resumable": item.status in RESUMABLE_STATUSES,
        "receipts": [
            _receipt_public(receipt, include_result=include_receipts)
            for receipt in receipts[-10:]
        ],
        "version": item.version,
        "created_at_epoch": item.created_at_epoch,
        "updated_at_epoch": item.updated_at_epoch,
    }


def _input_hash(tool_name: str, arguments: dict[str, Any]) -> str:
    """工具名和规范化参数共同决定回执身份；不保存完整参数正文。"""
    payload = json.dumps([tool_name, arguments or {}], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compact_result(result: Any) -> Any:
    """回执只保留可恢复所需的结果；超大结果截断并要求模型重新读取对象。"""
    try:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"truncated": True, "preview": str(result)[:MAX_RECEIPT_RESULT_CHARS]}
    if len(serialized) <= MAX_RECEIPT_RESULT_CHARS:
        return result
    return {"truncated": True, "preview": serialized[:MAX_RECEIPT_RESULT_CHARS]}


def start_plan(user_id: str, *, goal: str, steps: list[dict], conversation_id: str | None = None,
               run_id: str | None = None) -> dict[str, Any]:
    """同一运行重复提交返回同一计划；步骤只保存必要字段，不复制私人正文。"""
    goal = (goal or "").strip()
    if not goal or len(goal) > 1000:
        raise ApiProblem("invalid_plan", "计划目标需在 1 至 1000 字之间。", 422)
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ApiProblem("invalid_plan_steps", f"计划步骤需为 1 至 {MAX_STEPS} 项。", 422)
    clean = []
    for index, step in enumerate(steps):
        title = str((step or {}).get("title", "")).strip()[:300]
        if not title:
            raise ApiProblem("invalid_plan_step", "每个步骤都需要标题。", 422)
        clean.append({
            "index": index, "title": title, "status": "pending",
            "object_type": str(step.get("object_type"))[:32] if step.get("object_type") else None,
            "object_id": str(step.get("object_id"))[:64] if step.get("object_id") else None,
            "version": int(step["version"]) if step.get("version") is not None else None,
            "tool_receipt": None, "result_summary": None,
        })
    now = int(time.time())
    with Session(_engine()) as db, db.begin():
        existing = None
        if run_id:
            existing = db.scalar(select(AgentExecutionPlan).where(
                AgentExecutionPlan.user_id == user_id, AgentExecutionPlan.run_id == run_id).limit(1))
        if existing:
            return _public(existing, include_receipts=True)
        item = AgentExecutionPlan(
            id=str(uuid4()), user_id=user_id, conversation_id=conversation_id, run_id=run_id,
            goal=goal, status="active", steps_json=clean, tool_receipts_json=[], version=1,
            created_at_epoch=now, updated_at_epoch=now,
        )
        db.add(item)
        db.flush()
        return _public(item, include_receipts=True)


def _by_id(db: Session, user_id: str, plan_id: str) -> AgentExecutionPlan:
    item = db.scalar(select(AgentExecutionPlan).where(
        AgentExecutionPlan.id == plan_id, AgentExecutionPlan.user_id == user_id))
    if item is None:
        raise ApiProblem("not_found", "未找到该执行计划。", 404)
    return item


def get_plan(user_id: str, plan_id: str) -> dict[str, Any]:
    with Session(_engine()) as db:
        return _public(_by_id(db, user_id, plan_id), include_receipts=True)


def plan_for_run(user_id: str, run_id: str) -> dict[str, Any] | None:
    with Session(_engine()) as db:
        item = db.scalar(select(AgentExecutionPlan).where(
            AgentExecutionPlan.user_id == user_id, AgentExecutionPlan.run_id == run_id)
            .order_by(AgentExecutionPlan.updated_at_epoch.desc()).limit(1))
        return _public(item) if item else None


def active_for_conversation(user_id: str, conversation_id: str) -> dict[str, Any] | None:
    with Session(_engine()) as db:
        item = db.scalar(select(AgentExecutionPlan).where(
            AgentExecutionPlan.user_id == user_id,
            AgentExecutionPlan.conversation_id == conversation_id,
            AgentExecutionPlan.status == "active",
        ).order_by(AgentExecutionPlan.updated_at_epoch.desc()).limit(1))
        return _public(item) if item else None


def resumable_for_conversation(user_id: str, conversation_id: str) -> dict[str, Any] | None:
    """返回最近的可恢复计划；失败计划也属于恢复范围，不只 active。"""
    with Session(_engine()) as db:
        item = db.scalar(select(AgentExecutionPlan).where(
            AgentExecutionPlan.user_id == user_id,
            AgentExecutionPlan.conversation_id == conversation_id,
            AgentExecutionPlan.status.in_(RESUMABLE_STATUSES),
        ).order_by(AgentExecutionPlan.updated_at_epoch.desc()).limit(1))
        return _public(item) if item else None


def _resumable_plans(db: Session, user_id: str, conversation_id: str) -> list[AgentExecutionPlan]:
    return list(db.scalars(select(AgentExecutionPlan).where(
        AgentExecutionPlan.user_id == user_id,
        AgentExecutionPlan.conversation_id == conversation_id,
        AgentExecutionPlan.status.in_(RESUMABLE_STATUSES),
    ).order_by(AgentExecutionPlan.updated_at_epoch.desc()).limit(10)).all())


def find_replayed_receipt(
    user_id: str,
    conversation_id: str | None,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    now_epoch: int | None = None,
) -> dict[str, Any] | None:
    """查找同一会话可恢复计划中的同参数写工具回执，供 execute_tool 直接重放。"""
    if not conversation_id:
        return None
    input_hash = _input_hash(tool_name, arguments)
    now = int(time.time()) if now_epoch is None else now_epoch
    cutoff = now - RECEIPT_TTL_SECONDS
    with Session(_engine()) as db:
        for plan in _resumable_plans(db, user_id, conversation_id):
            for receipt in reversed(plan.tool_receipts_json or []):
                if (
                    receipt.get("tool_name") == tool_name
                    and receipt.get("input_hash") == input_hash
                    and int(receipt.get("created_at_epoch") or 0) >= cutoff
                    and isinstance(receipt.get("result"), dict)
                ):
                    replay = dict(receipt["result"])
                    replay["deduplicated"] = True
                    replay["deduplication_note"] = (
                        f"该写操作已在计划「{plan.goal[:80]}」的已完成步骤中执行；"
                        "已返回原工具回执，请勿重复执行，继续 next_step。"
                    )
                    replay["plan_id"] = plan.id
                    replay["already_completed_tool"] = tool_name
                    return replay
    return None


def record_tool_receipt(
    user_id: str,
    conversation_id: str | None,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_call_id: str,
    result: Any,
    now_epoch: int | None = None,
) -> dict[str, Any] | None:
    """写工具成功后把回执写入最近的可恢复计划；无计划时保持无副作用。"""
    if not conversation_id:
        return None
    input_hash = _input_hash(tool_name, arguments)
    now = int(time.time()) if now_epoch is None else now_epoch
    with Session(_engine()) as db, db.begin():
        plans = _resumable_plans(db, user_id, conversation_id)
        if not plans:
            return None
        plan = plans[0]
        receipts = [dict(receipt) for receipt in (plan.tool_receipts_json or [])]
        matching = [receipt for receipt in receipts if (
            receipt.get("tool_name") == tool_name and receipt.get("input_hash") == input_hash
        )]
        if any(int(receipt.get("created_at_epoch") or 0) >= now - RECEIPT_TTL_SECONDS for receipt in matching):
            return _public(plan, include_receipts=True)
        # 旧回执过期后允许新执行，但必须替换它；否则后续重试永远命中过期记录而再次写入。
        receipts = [receipt for receipt in receipts if receipt not in matching]
        receipts.append({
            "tool_name": tool_name,
            "input_hash": input_hash,
            "tool_call_id": str(tool_call_id)[:100],
            "result": _compact_result(result),
            "created_at_epoch": now,
        })
        plan.tool_receipts_json = receipts[-MAX_RECEIPTS:]
        plan.version += 1
        plan.updated_at_epoch = now
        db.flush()
        return _public(plan, include_receipts=True)


def update_step(user_id: str, plan_id: str, index: int, *, status: str, object_type: str | None = None,
                object_id: str | None = None, version: int | None = None,
                tool_receipt: str | None = None, result_summary: str | None = None) -> dict[str, Any]:
    """步骤状态和已执行回执持久化；全部完成后计划自动结束，失败保留待补充。"""
    if status not in STEP_STATUSES:
        raise ApiProblem("invalid_step_status", "步骤状态无效。", 422)
    with Session(_engine()) as db, db.begin():
        item = db.scalar(select(AgentExecutionPlan).where(
            AgentExecutionPlan.id == plan_id, AgentExecutionPlan.user_id == user_id).with_for_update())
        if item is None:
            raise ApiProblem("not_found", "未找到该执行计划。", 404)
        if item.status in {"completed", "cancelled"}:
            raise ApiProblem("plan_finished", "计划已结束，不能继续修改。", 409)
        steps = [dict(step) for step in (item.steps_json or [])]
        if not 0 <= index < len(steps):
            raise ApiProblem("invalid_plan_step", "步骤序号超出范围。", 422)
        step = steps[index]
        if step.get("status") == "done" and status != "done":
            raise ApiProblem("step_already_done", "已完成步骤不能回退；如对象确实变化，请新建计划。", 409)
        step.update({"status": status})
        for key, value in (("object_type", object_type), ("object_id", object_id),
                           ("version", version), ("tool_receipt", tool_receipt),
                           ("result_summary", (result_summary or "")[:500] if result_summary else None)):
            if value is not None:
                step[key] = value
        item.steps_json = steps
        item.version += 1
        item.updated_at_epoch = int(time.time())
        statuses = {entry.get("status") for entry in steps}
        if statuses <= {"done"}:
            item.status = "completed"
        elif "failed" in statuses:
            item.status = "failed"
        else:
            item.status = "active"
        db.flush()
        return _public(item, include_receipts=True)
