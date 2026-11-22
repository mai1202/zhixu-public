"""关键词与可选向量融合检索；未配置向量模型时保持关键词可用。

向量切块是原资料的派生索引，不建立第二套事实源；原文仍从原业务表读取。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from flask import current_app
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (AgentAttachment, AgentMemory, AgentTurn, KnowledgeChunk,
                      KnowledgeDocument, NotificationOrganization, SourceDocument)
from . import knowledge_service

CHUNKER_VERSION = "v1"
SAFE_KINDS = {"document", "notification", "attachment", "memory", "conversation"}


def _config(name, default=None):
    return current_app.config.get(name, default)


def _engine():
    return current_app.extensions["notification_db_engine"]


def _enabled() -> bool:
    return bool(_config("EMBEDDING_ENABLED")) and bool(_config("EMBEDDING_ENDPOINT")) and bool(_config("EMBEDDING_MODEL"))


def embedding_status() -> dict:
    """诊断页只显示配置状态和模型版本，不返回密钥。"""
    endpoint = str(_config("EMBEDDING_ENDPOINT") or "")
    safe = True
    loopback = False
    if endpoint:
        try:
            parsed = urlsplit(endpoint)
            safe = bool(parsed.scheme in {"http", "https"} and parsed.hostname
                        and not parsed.username and not parsed.password)
            loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            safe = safe and (loopback or bool(_config("EMBEDDING_ALLOW_CLOUD")))
        except ValueError:
            safe = False
    return {
        "enabled": bool(_config("EMBEDDING_ENABLED")),
        "configured": _enabled(),
        "endpoint_configured": bool(endpoint),
        "endpoint_safe": safe,
        "model": str(_config("EMBEDDING_MODEL") or ""),
        "dimensions": int(_config("EMBEDDING_DIMENSIONS") or 0),
        "allow_cloud": bool(_config("EMBEDDING_ALLOW_CLOUD")),
        "method": "hybrid" if _enabled() and safe else "keyword",
        "chunk_chars": min(600, max(120, int(_config("RETRIEVAL_CHUNK_CHARS") or 600))),
        "note": "未配置向量模型或端点不安全时继续使用关键词检索。",
    }


def _validate_endpoint(endpoint: str):
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("embedding_endpoint_invalid")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not loopback:
        raise ValueError("embedding_endpoint_requires_https")
    if not loopback and not _config("EMBEDDING_ALLOW_CLOUD"):
        raise ValueError("embedding_cloud_not_allowed")


def _embed(texts: list[str]) -> tuple[list[list[float]] | None, str | None]:
    """调用明确配置的 OpenAI 兼容 embeddings 端点；失败返回原因，不阻塞关键词检索。"""
    if not _enabled():
        return None, "embedding_not_configured"
    endpoint = str(_config("EMBEDDING_ENDPOINT")).rstrip("/")
    try:
        _validate_endpoint(endpoint)
    except ValueError as error:
        return None, str(error)
    payload = json.dumps({"model": _config("EMBEDDING_MODEL"), "input": texts}).encode()
    headers = {"Content-Type": "application/json"}
    if _config("EMBEDDING_API_KEY"):
        headers["Authorization"] = "Bearer " + str(_config("EMBEDDING_API_KEY"))
    try:
        request = Request(endpoint + "/embeddings", data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=int(_config("EMBEDDING_TIMEOUT_SECONDS", 20))) as response:
            body = json.loads(response.read(20 * 1024 * 1024))
        rows = body.get("data") or []
        vectors = [row.get("embedding") for row in rows]
        if len(vectors) != len(texts) or not all(isinstance(v, list) and v for v in vectors):
            return None, "embedding_invalid_response"
        dimensions = int(_config("EMBEDDING_DIMENSIONS") or 0)
        if dimensions and any(len(vector) != dimensions for vector in vectors):
            return None, "embedding_dimension_mismatch"
        return [[float(value) for value in vector] for vector in vectors], None
    except Exception as error:
        text = str(error).lower()
        if "timed out" in text or "timeout" in text:
            return None, "embedding_timeout"
        return None, "embedding_unavailable"


def chunk_text(text: str, *, size: int | None = None, overlap: int | None = None) -> list[str]:
    """按段落和句号切块，保留少量重叠；同一输入始终得到相同切块。"""
    size = min(600, max(120, int(size or _config("RETRIEVAL_CHUNK_CHARS", 600))))
    overlap = max(0, min(int(overlap if overlap is not None else _config("RETRIEVAL_CHUNK_OVERLAP", 80)), size // 2))
    clean = re.sub(r"\r\n?", "\n", text or "").strip()
    if not clean:
        return []
    pieces = [piece.strip() for piece in re.split(r"\n{2,}|(?<=[。！？；;])", clean) if piece.strip()]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if len(current) + len(piece) + 1 <= size:
            current = (current + "\n" + piece).strip() if current else piece
            continue
        if current:
            chunks.append(current)
        # 超长段落按硬边界切分，避免单块超过模型输入。
        start = 0
        while len(piece) - start > size:
            chunks.append(piece[start:start + size])
            start += size - overlap
        current = piece[start:]
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _source_rows(user_id: str, source_kind: str, source_id: str):
    with Session(_engine()) as db:
        if source_kind == "document":
            item = db.get(KnowledgeDocument, source_id)
            if item is None or not (item.user_id == user_id or (item.scope == "public" and item.status == "active")):
                return None
            return {"title": item.title, "content": item.content, "version": item.version,
                    "scope": item.scope, "source_url": item.source_url, "user_id": item.user_id}
        if source_kind == "notification":
            item = db.scalar(select(SourceDocument).where(SourceDocument.id == source_id,
                SourceDocument.user_id == user_id))
            if item is None:
                return None
            summary = db.get(NotificationOrganization, item.id)
            return {"title": (summary.title if summary and summary.title else item.title),
                    "content": item.original_text, "version": 1, "scope": "private",
                    "source_url": None, "user_id": user_id}
        if source_kind == "attachment":
            item = db.scalar(select(AgentAttachment).where(AgentAttachment.id == source_id,
                AgentAttachment.user_id == user_id))
            if item is None:
                return None
            return {"title": item.filename, "content": item.extracted_text or "", "version": 1,
                    "scope": "private", "source_url": None, "user_id": user_id}
        if source_kind == "memory":
            item = db.scalar(select(AgentMemory).where(AgentMemory.id == source_id,
                AgentMemory.user_id == user_id))
            if item is None:
                return None
            return {"title": f"长期记忆 · {item.category}", "content": item.content, "version": 1,
                    "scope": "private", "source_url": None, "user_id": user_id}
        if source_kind == "conversation":
            item = db.scalar(select(AgentTurn).where(AgentTurn.id == source_id,
                AgentTurn.user_id == user_id))
            if item is None:
                return None
            return {"title": ("你" if item.role == "user" else "助手") + "的对话记录",
                    "content": item.content, "version": 1, "scope": "private",
                    "source_url": None, "user_id": user_id}
    return None


def index_source(user_id: str, source_kind: str, source_id: str, *, force: bool = False) -> dict:
    """为一份资料重建切块；嵌入失败时保留切块并报告降级。"""
    if source_kind not in SAFE_KINDS:
        return {"status": "skipped", "reason": "unsupported_kind"}
    source = _source_rows(user_id, source_kind, source_id)
    if source is None:
        return {"status": "skipped", "reason": "not_found_or_unauthorized"}
    chunks = chunk_text(source["content"])
    if not chunks:
        return {"status": "skipped", "reason": "empty_content"}
    model = str(_config("EMBEDDING_MODEL") or "")
    dimensions = int(_config("EMBEDDING_DIMENSIONS") or 0)
    with Session(_engine()) as db:
        existing = db.scalars(select(KnowledgeChunk).where(
            KnowledgeChunk.user_id == source["user_id"],
            KnowledgeChunk.source_kind == source_kind,
            KnowledgeChunk.source_id == source_id,
        ).order_by(KnowledgeChunk.chunk_index)).all()
        unchanged = (not force and len(existing) == len(chunks)
                     and all(item.source_version == source["version"] for item in existing)
                     and all(item.chunker_version == CHUNKER_VERSION for item in existing)
                     and (not _enabled() or all(item.embedding_model == model and item.embedding_json is not None
                                                and (not dimensions or item.embedding_dimensions == dimensions)
                                                for item in existing)))
        if unchanged:
            return {"status": "unchanged", "chunks": len(existing), "method": "hybrid" if _enabled() else "keyword"}
    vectors, error = _embed(chunks) if _enabled() else (None, "embedding_not_configured")
    now = int(time.time())
    with Session(_engine()) as db, db.begin():
        db.execute(delete(KnowledgeChunk).where(
            KnowledgeChunk.user_id == source["user_id"],
            KnowledgeChunk.source_kind == source_kind,
            KnowledgeChunk.source_id == source_id,
        ))
        for index, chunk in enumerate(chunks):
            digest = hashlib.sha256(chunk.encode()).hexdigest()
            db.add(KnowledgeChunk(
                id=hashlib.sha256(f"{source['user_id']}:{source_kind}:{source_id}:{index}:{digest}".encode()).hexdigest(),
                user_id=source["user_id"], source_kind=source_kind, source_id=source_id,
                source_version=source["version"], scope=source["scope"], chunk_index=index,
                content=chunk, content_hash=digest,
                embedding_json=vectors[index] if vectors else None,
                embedding_model=model if vectors else None,
                embedding_dimensions=len(vectors[index]) if vectors else None,
                chunker_version=CHUNKER_VERSION, updated_at_epoch=now,
            ))
    try:
        from . import search_service
        search_service.invalidate_cache()
    except Exception:
        current_app.logger.warning("搜索缓存失效失败 source=%s", source_id)
    return {"status": "indexed", "chunks": len(chunks), "method": "hybrid" if vectors else "keyword",
            "embedding_error": error}


def remove_source(user_id: str, source_kind: str, source_id: str) -> None:
    """归档或删除资料时同步撤出衍生索引，避免旧切块继续被自动检索。"""
    with Session(_engine()) as db, db.begin():
        db.execute(delete(KnowledgeChunk).where(
            KnowledgeChunk.user_id == user_id,
            KnowledgeChunk.source_kind == source_kind,
            KnowledgeChunk.source_id == source_id,
        ))


def index_source_by_id(user_id: str, source_kind: str, source_id: str, *, force: bool = False) -> dict:
    return index_source(user_id, source_kind, source_id, force=force)


def schedule_index(user_id: str, source_kind: str, source_id: str, *, version: int = 1) -> None:
    """写入后异步建索引；没有嵌入模型时在请求内做轻量切块，失败不影响主流程。"""
    try:
        if _enabled():
            from ..jobs.queue import enqueue_job
            enqueue_job(_engine(), user_id=user_id, kind="knowledge.index",
                payload={"source_kind": source_kind, "source_id": source_id},
                idempotency_key=f"knowledge-index:{source_kind}:{source_id}:{version}", priority=80)
        else:
            index_source(user_id, source_kind, source_id)
    except Exception:
        current_app.logger.warning("知识索引更新失败 kind=%s", source_kind)


def schedule_vector_index(user_id: str, source_kind: str, source_id: str, *, version: int = 1) -> None:
    """通知、附件、记忆和对话只在向量通道可用时建索引。

    这些资料的原表已经支撑关键词检索；没有配置可用端点时不必为每一轮对话
    写入无向量的切块。文档保存仍走 schedule_index，保留按需重建的能力。
    """
    status = embedding_status()
    if not (status["configured"] and status["endpoint_safe"]):
        return
    schedule_index(user_id, source_kind, source_id, version=version)


def handle_knowledge_index_job(claim) -> dict:
    payload = claim.payload or {}
    return index_source_by_id(claim.user_id, str(payload.get("source_kind", "")),
                              str(payload.get("source_id", "")), force=bool(payload.get("force")))


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _vector_rows(user_id: str, scope: str, kind: str, query_vector: list[float], limit: int) -> list[dict]:
    with Session(_engine()) as db:
        statement = select(KnowledgeChunk).where(KnowledgeChunk.embedding_json.is_not(None),
            KnowledgeChunk.embedding_model == str(_config('EMBEDDING_MODEL') or ''))
        if scope == "public":
            statement = statement.where(KnowledgeChunk.scope == "public")
        elif scope == "private":
            statement = statement.where(KnowledgeChunk.user_id == user_id, KnowledgeChunk.scope == "private")
        else:
            statement = statement.where((KnowledgeChunk.user_id == user_id) | (KnowledgeChunk.scope == "public"))
        if kind != "all":
            statement = statement.where(KnowledgeChunk.source_kind == kind)
        rows = db.scalars(statement.limit(4000)).all()
    scored: dict[tuple, dict] = {}
    for row in rows:
        score = _cosine(query_vector, row.embedding_json or [])
        if score <= 0:
            continue
        key = (row.source_kind, row.source_id)
        current = scored.get(key)
        if current is None or score > current["score"]:
            scored[key] = {"kind": row.source_kind, "id": row.source_id, "scope": row.scope,
                           "title": None, "snippet": row.content[:450], "score": score,
                           "version": row.source_version, "href": None, "source_url": None,
                           "matched_by": ["vector"]}
    ordered = sorted(scored.values(), key=lambda item: item["score"], reverse=True)[:limit]
    return ordered


def _fill_titles(items: list[dict]) -> list[dict]:
    """向量块只带来源 ID；标题和出处从原表补齐，并剔除已归档/已删除来源。"""
    kept = []
    with Session(_engine()) as db:
        for item in items:
            if item["kind"] == "document":
                row = db.get(KnowledgeDocument, item["id"])
                if row is None or row.status != "active":
                    continue
                item.update(title=row.title, version=row.version, source_url=row.source_url,
                            href=f"/knowledge?document={row.id}", scope=row.scope,
                            metadata=row.metadata_json or {})
            elif item["kind"] == "notification":
                row = db.get(SourceDocument, item["id"])
                if row is None:
                    continue
                summary = db.get(NotificationOrganization, row.id)
                item.update(title=(summary.title if summary and summary.title else row.title)[:200],
                            href=f"/inbox?source={row.id}")
            elif item["kind"] == "attachment":
                row = db.get(AgentAttachment, item["id"])
                if row is None:
                    continue
                item.update(title=row.filename[:200], href=f"/api/v1/agent/attachments/{row.id}/download")
            elif item["kind"] == "memory":
                row = db.get(AgentMemory, item["id"])
                if row is None:
                    continue
                item.update(title=f"长期记忆 · {row.category}"[:200], version=1,
                            href="/settings/memory", scope="private")
            elif item["kind"] == "conversation":
                row = db.get(AgentTurn, item["id"])
                if row is None:
                    continue
                item.update(title=(("你" if row.role == "user" else "助手") + "的对话记录")[:200],
                            version=1, href=f"/agent?conversation={row.conversation_id}", scope="private")
            else:
                continue
            kept.append(item)
    return kept


RRF_K = 60


def _rrf(rank: int | None) -> float:
    """标准倒数排名融合分量；原始 BM25/向量分只保留在诊断字段。"""
    return 1.0 / (RRF_K + rank + 1) if rank else 0.0


def hybrid_search(user_id: str, query: str, *, scope: str = "all", kind: str = "all", limit: int = 8) -> dict:
    """关键词与向量都按各自排名换算 RRF 分，不混用原始分数的量纲。"""
    keyword = knowledge_service.search(user_id, query, scope=scope, kind=kind, limit=max(limit * 2, 10))
    items = {}
    for rank, item in enumerate(keyword["items"]):
        key = f"{item['kind']}:{item['id']}"
        items[key] = {**item, "matched_by": ["keyword"], "keyword_rank": rank + 1,
                      "keyword_score": float(item.get("score", 0)), "vector_rank": None,
                      "vector_score": None, "rrf": 0.0}
    diagnostic = {"embedding": embedding_status(), "keyword_count": len(items),
                  "vector_count": 0, "fusion": "rrf", "rrf_k": RRF_K}
    if _enabled() and query.strip():
        query_vector, error = _embed([query])
        if query_vector:
            vector_items = _fill_titles(_vector_rows(user_id, scope, kind, query_vector[0], max(limit, 10)))
            diagnostic["vector_count"] = len(vector_items)
            for rank, item in enumerate(vector_items):
                key = f"{item['kind']}:{item['id']}"
                if key in items:
                    items[key]["matched_by"].append("vector")
                    items[key]["vector_rank"] = rank + 1
                    items[key]["vector_score"] = float(item.get("score", 0))
                else:
                    item["content_hash"] = hashlib.sha256(item["snippet"].encode()).hexdigest()
                    items[key] = {**item, "matched_by": ["vector"], "keyword_rank": None,
                                  "keyword_score": None, "vector_rank": rank + 1,
                                  "vector_score": float(item.get("score", 0)), "rrf": 0.0}
        else:
            diagnostic["embedding_error"] = error
    else:
        diagnostic["embedding_error"] = "embedding_not_configured"
    for item in items.values():
        item["rrf"] = _rrf(item.get("keyword_rank")) + _rrf(item.get("vector_rank"))
        # 以 1000 为尺度便于与网页检索的相关性分数放在同一列表排序；不参与 RRF 计算。
        item["score"] = item["rrf"] * 1000
    ranked = sorted(items.values(), key=lambda item: (
        item["rrf"], 1 if item.get("vector_rank") else 0, -(item.get("keyword_rank") or 9999)
    ), reverse=True)[:limit]
    from .search_service import _citation
    return {
        "items": ranked,
        "has_more": bool(keyword.get("has_more")) or len(items) > limit,
        "method": "hybrid" if diagnostic.get("vector_count") else "keyword",
        "diagnostic": diagnostic,
        "note": "关键词与向量按排名融合召回；引用前仍须读取原文核对日期、条件和适用范围。",
        "citations": [_citation(item) for item in ranked],
    }
