"""用户级长期记忆：跨对话保存偏好、约束、作息和目标。"""

import re
import time
from typing import Any
from uuid import uuid4

from flask import current_app
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import ApiProblem
from ..models import AgentMemory

# 分类只用于展示和提示，不参与权限判断。
MEMORY_CATEGORIES = {"preference", "constraint", "routine", "goal", "other"}
MAX_MEMORIES = 100
STABLE_MARKERS = re.compile(r"记住|以后|每次|通常|一般|习惯|喜欢|不喜欢|固定|每周|每天|工作日|周末")
RECURRING_MARKERS = re.compile(r"每天|每周|每学期|工作日|周末|周[一二三四五六日天]")
TRANSIENT_MARKERS = re.compile(r"今天|明天|后天|今晚|明晚|这周|本周|下周|待会|临时")
SENSITIVE_MARKERS = re.compile(r"密码|密钥|验证码|身份证|护照|学号|银行卡|信用卡|手机号|电话|联系方式|账号|住址|住哪里")


def _engine():
    return current_app.extensions["notification_db_engine"]


def _schedule_memory_index(user_id: str, memory_id: str | None) -> None:
    """向量通道可用时把长期记忆纳入语义召回；失败不影响记忆保存。"""
    if not memory_id:
        return
    try:
        from .retrieval_service import schedule_vector_index
        schedule_vector_index(user_id, "memory", memory_id)
    except Exception:
        current_app.logger.warning("长期记忆向量索引调度失败 memory_id=%s", memory_id)


def _normalize(content: str) -> str:
    """折叠空白并忽略大小写，避免同一事实被重复保存。"""
    return " ".join(content.split()).casefold()[:1000]


def memory_public(memory: AgentMemory) -> dict[str, Any]:
    """只返回当前账户自己的记忆字段，不暴露内部规范化列。"""
    return {
        "id": memory.id,
        "category": memory.category,
        "content": memory.content,
        "source_conversation_id": memory.source_conversation_id,
        "created_at_epoch": memory.created_at_epoch,
        "updated_at_epoch": memory.updated_at_epoch,
    }


def list_memories(user_id: str, *, limit: int = 50) -> dict[str, Any]:
    """按最近更新返回有限条目，供页面管理或注入本轮提示。"""
    with Session(_engine()) as db:
        rows = db.scalars(
            select(AgentMemory)
            .where(AgentMemory.user_id == user_id)
            .order_by(AgentMemory.updated_at_epoch.desc())
            .limit(max(1, min(limit, MAX_MEMORIES)))
        ).all()
    return {"items": [memory_public(row) for row in rows]}


def remember_memory(
    user_id: str,
    *,
    conversation_id: str | None,
    content: str,
    category: str = "other",
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """保存或刷新一条长期记忆；相同内容只保留一条。"""
    # 长期事实保持短句，避免大量记忆挤占每轮上下文预算。
    cleaned = " ".join(str(content).split()).strip()[:600]
    if not cleaned:
        raise ApiProblem("memory_content_required", "长期记忆内容不能为空。", 422)
    chosen_category = category if category in MEMORY_CATEGORIES else "other"
    normalized = _normalize(cleaned)
    now = int(time.time()) if now_epoch is None else now_epoch
    result: dict[str, Any] | None = None
    saved_id: str | None = None
    with Session(_engine()) as db, db.begin():
        existing = db.scalar(
            select(AgentMemory).where(
                AgentMemory.user_id == user_id,
                AgentMemory.normalized_content == normalized,
            )
        )
        if existing is not None:
            # 重复提醒只刷新来源和时间，不复制条目，也不破坏原始创建时间。
            existing.category = chosen_category
            existing.source_conversation_id = conversation_id
            existing.updated_at_epoch = now
            db.flush()
            saved_id = existing.id
            result = memory_public(existing)
        else:
            count = db.scalar(
                select(func.count()).select_from(AgentMemory).where(AgentMemory.user_id == user_id)
            ) or 0
            if count >= MAX_MEMORIES:
                raise ApiProblem(
                    "memory_limit_reached",
                    "长期记忆已达到 100 条，请先在助手设置中删除不再需要的内容。",
                    409,
                )
            memory = AgentMemory(
                id=str(uuid4()),
                user_id=user_id,
                source_conversation_id=conversation_id,
                category=chosen_category,
                content=cleaned,
                normalized_content=normalized,
                created_at_epoch=now,
                updated_at_epoch=now,
            )
            db.add(memory)
            db.flush()
            saved_id = memory.id
            result = memory_public(memory)
    _schedule_memory_index(user_id, saved_id)
    assert result is not None
    return result


def forget_memory(user_id: str, memory_id: str) -> dict[str, Any]:
    """仅允许账户本人按可见 ID 删除长期记忆。"""
    with Session(_engine()) as db, db.begin():
        memory = db.scalar(
            select(AgentMemory).where(
                AgentMemory.id == memory_id,
                AgentMemory.user_id == user_id,
            )
        )
        if memory is None:
            raise ApiProblem("not_found", "未找到该长期记忆。", 404)
        public = memory_public(memory)
        db.delete(memory)
    try:
        from .retrieval_service import remove_source
        remove_source(user_id, "memory", memory_id)
    except Exception:
        current_app.logger.warning("长期记忆向量索引删除失败 memory_id=%s", memory_id)
    return {"status": "deleted", "memory": public}


def prompt_entries(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """为 Pi 生成受限的记忆列表；正文长度已在写入时限制。"""
    return list_memories(user_id, limit=limit)["items"]


def auto_remember_from_message(user_id: str, conversation_id: str, message: str) -> dict[str, Any] | None:
    """保守识别用户消息中的长期信息，作为模型主动记忆的本地兜底。

    只处理稳定偏好或规律；一次性安排、对单次日程的修改以及密码、证件号等
    敏感凭证不写入，避免把聊天中的临时上下文误当长期事实。
    """
    cleaned = " ".join(message.split()).strip()
    if not 8 <= len(cleaned) <= 600:
        return None
    if not STABLE_MARKERS.search(cleaned) or SENSITIVE_MARKERS.search(cleaned):
        return None
    if TRANSIENT_MARKERS.search(cleaned) and not RECURRING_MARKERS.search(cleaned):
        return None
    # 去掉口语化的“请记住”命令，只保留事实本身。
    cleaned = re.sub(r"^(请|帮我)?记住[，,：:\s]*", "", cleaned).strip()
    if len(cleaned) < 6:
        return None
    if RECURRING_MARKERS.search(cleaned) or re.search(r"作息|早上|晚上|午休", cleaned):
        category = "routine"
    elif re.search(r"不要|不能|避免|限制", cleaned):
        category = "constraint"
    elif re.search(r"目标|希望|计划", cleaned):
        category = "goal"
    else:
        category = "preference"
    return remember_memory(
        user_id,
        conversation_id=conversation_id,
        content=cleaned,
        category=category,
    )
