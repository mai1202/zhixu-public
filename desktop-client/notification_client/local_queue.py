"""保存待上传批次与独立采集/确认游标的加密本地队列。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator

from .secure_store import SecretProtector


class QueueFullError(RuntimeError):
    """本地队列达到上限时停止继续采集，避免静默丢弃未确认数据。"""


@dataclass(frozen=True)
class QueuedBatch:
    """一条已加密持久化的上传批次。"""

    batch_id: str
    window_start_epoch: int
    window_end_epoch: int
    complete: bool
    failed_pages: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    attempts: int


class LocalQueue:
    """使用 SQLite 原子保留未确认批次，正文在写盘前由 DPAPI 加密。"""

    def __init__(self, path: str | Path, protector: SecretProtector, *, max_bytes: int = 50 * 1024 * 1024):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.protector = protector
        self.max_bytes = max_bytes
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS queued_batches (
                    batch_id TEXT PRIMARY KEY,
                    window_start_epoch INTEGER NOT NULL,
                    window_end_epoch INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    failed_pages_blob BLOB NOT NULL,
                    payload_blob BLOB NOT NULL,
                    acknowledgements_blob BLOB,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    created_at_epoch INTEGER NOT NULL,
                    acknowledged_at_epoch INTEGER
                );
                CREATE INDEX IF NOT EXISTS ix_queued_batches_status_created
                    ON queued_batches(status, created_at_epoch);
                CREATE TABLE IF NOT EXISTS cursors (
                    chat_id TEXT PRIMARY KEY,
                    collected_until_epoch INTEGER NOT NULL DEFAULT 0,
                    acknowledged_until_epoch INTEGER NOT NULL DEFAULT 0,
                    updated_at_epoch INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS queued_attachments (
                    attachment_key TEXT PRIMARY KEY,
                    chat_id TEXT,
                    kind TEXT,
                    payload_blob BLOB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    created_at_epoch INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deferred_attachment_reads (
                    attachment_key TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reference_blob BLOB NOT NULL,
                    created_at_epoch INTEGER NOT NULL,
                    next_attempt_epoch INTEGER NOT NULL
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(queued_batches)").fetchall()}
            if "acknowledgements_blob" not in columns:
                db.execute("ALTER TABLE queued_batches ADD COLUMN acknowledgements_blob BLOB")
            attachment_columns = {row['name'] for row in db.execute('PRAGMA table_info(queued_attachments)')}
            if 'chat_id' not in attachment_columns or 'kind' not in attachment_columns:
                # 兼容本地旧队列；仅迁移范围索引，文件内容继续保留在加密字段中。
                for name in ('chat_id', 'kind'):
                    if name not in attachment_columns:
                        db.execute(f'ALTER TABLE queued_attachments ADD COLUMN {name} TEXT')
                for row in db.execute('SELECT attachment_key, payload_blob FROM queued_attachments').fetchall():
                    payload = json.loads(self.protector.unprotect(row['payload_blob']).decode('utf-8'))
                    db.execute('UPDATE queued_attachments SET chat_id = ?, kind = ? WHERE attachment_key = ?',
                        (payload.get('chat_id'), payload.get('kind'), row['attachment_key']))
                db.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """每次操作建立短连接，避免在托盘和同步线程间共享句柄。"""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _canonical(value: dict[str, Any]) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def enqueue(self, payload: dict[str, Any]) -> None:
        """新批次先写磁盘再尝试上传；相同批次只接受完全相同内容。"""
        batch_id = str(payload["batch_id"])
        plain = self._canonical(payload)
        protected = self.protector.protect(plain)
        failed = self.protector.protect(self._canonical({"failed_pages": payload.get("failed_pages", [])}))
        with self._connect() as db:
            existing = db.execute(
                "SELECT payload_blob FROM queued_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if existing is not None:
                if self.protector.unprotect(existing["payload_blob"]) != plain:
                    raise ValueError("本地批次标识已对应不同内容")
                return
            used = db.execute(
                "SELECT COALESCE(SUM(length(payload_blob) + length(failed_pages_blob)), 0) FROM queued_batches WHERE status = 'pending'"
            ).fetchone()[0]
            used += db.execute("SELECT COALESCE(SUM(length(payload_blob)), 0) FROM queued_attachments WHERE status = 'pending'").fetchone()[0]
            if used + len(protected) + len(failed) > self.max_bytes:
                raise QueueFullError("本地待上传队列已满；已暂停采集以保留未确认数据。")
            db.execute(
                "INSERT INTO queued_batches (batch_id, window_start_epoch, window_end_epoch, complete, failed_pages_blob, payload_blob, created_at_epoch) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    int(payload["window_start_epoch"]),
                    int(payload["window_end_epoch"]),
                    int(bool(payload["complete"])),
                    failed,
                    protected,
                    int(time.time()),
                ),
            )
            db.commit()

    def pending(self, limit: int = 20) -> list[QueuedBatch]:
        """按创建顺序读取有限数量的待上传批次。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM queued_batches WHERE status = 'pending' ORDER BY created_at_epoch, batch_id LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        result: list[QueuedBatch] = []
        for row in rows:
            payload = json.loads(self.protector.unprotect(row["payload_blob"]).decode("utf-8"))
            result.append(
                QueuedBatch(
                    batch_id=row["batch_id"],
                    window_start_epoch=row["window_start_epoch"],
                    window_end_epoch=row["window_end_epoch"],
                    complete=bool(row["complete"]),
                    failed_pages=payload.get("failed_pages", []),
                    messages=payload.get("messages", []),
                    attempts=row["attempts"],
                )
            )
        return result

    def mark_attempt(self, batch_id: str, error_code: str | None = None) -> None:
        """上传失败只增加尝试计数，不删除或跳过该批次。"""
        with self._connect() as db:
            db.execute(
                "UPDATE queued_batches SET attempts = attempts + 1, last_error_code = ? WHERE batch_id = ? AND status = 'pending'",
                (error_code, batch_id),
            )
            db.commit()

    def acknowledge(self, batch_id: str, acknowledgements: list[dict[str, Any]]) -> None:
        """只有服务器逐条回执覆盖批内所有来源键后，才确认本地批次。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_blob FROM queued_batches WHERE batch_id = ? AND status = 'pending'", (batch_id,)
            ).fetchone()
            if row is None:
                return
            payload = json.loads(self.protector.unprotect(row["payload_blob"]).decode("utf-8"))
            expected = {item["source_key"] for item in payload.get("messages", [])}
            received = {item.get("source_key") for item in acknowledgements if item.get("status") in {"accepted", "duplicate", "rejected"}}
            if expected != received:
                raise ValueError("服务器逐条回执不完整，保留批次等待重试")
            db.execute(
                "UPDATE queued_batches SET status = 'acknowledged', acknowledgements_blob = ?, acknowledged_at_epoch = ?, attempts = attempts + 1, last_error_code = NULL WHERE batch_id = ?",
                (
                    self.protector.protect(self._canonical({"acks": acknowledgements})),
                    int(time.time()),
                    batch_id,
                ),
            )
            if bool(payload.get("complete")):
                for chat_id in {item.get("chat_id") for item in payload.get("messages", [])}:
                    if not chat_id:
                        continue
                    db.execute(
                        "INSERT INTO cursors(chat_id, collected_until_epoch, acknowledged_until_epoch, updated_at_epoch) VALUES (?, 0, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET acknowledged_until_epoch = MAX(acknowledged_until_epoch, excluded.acknowledged_until_epoch), updated_at_epoch = excluded.updated_at_epoch",
                        (chat_id, int(payload["window_end_epoch"]), int(time.time())),
                    )
            db.commit()

    def save_collection_cursor(self, chat_id: str, collected_until_epoch: int) -> None:
        """采集游标只在该会话所有分页读取完成后推进。"""
        now = int(time.time())
        with self._connect() as db:
            db.execute(
                "INSERT INTO cursors(chat_id, collected_until_epoch, acknowledged_until_epoch, updated_at_epoch) VALUES (?, ?, 0, ?) ON CONFLICT(chat_id) DO UPDATE SET collected_until_epoch = MAX(collected_until_epoch, excluded.collected_until_epoch), updated_at_epoch = excluded.updated_at_epoch",
                (chat_id, collected_until_epoch, now),
            )
            db.commit()

    def save_acknowledged_cursor(self, chat_id: str, acknowledged_until_epoch: int) -> None:
        """上传游标独立于采集游标，避免失败批次造成数据缺口。"""
        now = int(time.time())
        with self._connect() as db:
            db.execute(
                "INSERT INTO cursors(chat_id, collected_until_epoch, acknowledged_until_epoch, updated_at_epoch) VALUES (?, 0, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET acknowledged_until_epoch = MAX(acknowledged_until_epoch, excluded.acknowledged_until_epoch), updated_at_epoch = excluded.updated_at_epoch",
                (chat_id, acknowledged_until_epoch, now),
            )
            db.commit()

    def cursor(self, chat_id: str) -> tuple[int, int]:
        """返回已采集与已确认上传的独立游标。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT collected_until_epoch, acknowledged_until_epoch FROM cursors WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def queue_stats(self) -> dict[str, int]:
        """返回队列数量和未确认加密字节数，不暴露正文。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(payload_blob) + length(failed_pages_blob)), 0), (SELECT COUNT(*) FROM queued_batches WHERE status = 'acknowledged') FROM queued_batches WHERE status = 'pending'"
            ).fetchone()
            acknowledgements = db.execute(
                "SELECT acknowledgements_blob FROM queued_batches WHERE status = 'acknowledged' AND acknowledgements_blob IS NOT NULL"
            ).fetchall()
            attachment_stats = db.execute("SELECT COUNT(*), COALESCE(SUM(length(payload_blob)), 0) FROM queued_attachments WHERE status = 'pending'").fetchone()
        rejected_items = 0
        for item in acknowledgements:
            try:
                payload = json.loads(self.protector.unprotect(item[0]).decode("utf-8"))
                rejected_items += sum(1 for ack in payload.get("acks", []) if ack.get("status") == "rejected")
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                continue
        return {
            "pending_batches": int(row[0]),
            "pending_bytes": int(row[1]),
            "acknowledged_batches": int(row[2]),
            "rejected_items": rejected_items,
            "pending_attachments": int(attachment_stats[0]),
            "attachment_pending_bytes": int(attachment_stats[1]),
        }

    def enqueue_attachment(self, payload):
        """附件先加密持久化，文本 ACK 与附件 ACK 独立；重复采集不会重复排队。"""
        key = payload['attachment_key']
        protected = self.protector.protect(self._canonical(payload))
        with self._connect() as db:
            existing = db.execute('SELECT payload_blob FROM queued_attachments WHERE attachment_key = ?', (key,)).fetchone()
            if existing:
                previous = json.loads(self.protector.unprotect(existing[0]).decode('utf-8'))
                if previous.get('content_hash') and payload.get('content_hash') and previous['content_hash'] != payload['content_hash']:
                    raise ValueError('同一附件标识的内容发生变化')
                # 未下载附件后来可读时，替换旧状态并重新进入上传队列。
                if previous.get('unavailable_reason') and payload.get('data_base64'):
                    used = db.execute("SELECT COALESCE(SUM(length(payload_blob)), 0) FROM queued_attachments WHERE status = 'pending' AND attachment_key != ?", (key,)).fetchone()[0]
                    used += db.execute("SELECT COALESCE(SUM(length(payload_blob) + length(failed_pages_blob)), 0) FROM queued_batches WHERE status = 'pending'").fetchone()[0]
                    if used + len(protected) > self.max_bytes:
                        raise QueueFullError('附件队列已满，保留待上传内容。')
                    db.execute("UPDATE queued_attachments SET payload_blob = ?, status = 'pending', last_error_code = NULL WHERE attachment_key = ?", (protected, key))
                    db.commit()
                return
            used = db.execute("SELECT COALESCE(SUM(length(payload_blob)), 0) FROM queued_attachments WHERE status = 'pending'").fetchone()[0]
            batches = db.execute("SELECT COALESCE(SUM(length(payload_blob) + length(failed_pages_blob)), 0) FROM queued_batches WHERE status = 'pending'").fetchone()[0]
            if used + batches + len(protected) > self.max_bytes:
                raise QueueFullError('附件队列已满，保留待上传内容并暂停采集。')
            db.execute('INSERT INTO queued_attachments (attachment_key, chat_id, kind, payload_blob, created_at_epoch) VALUES (?, ?, ?, ?, ?)',
                (key, payload.get('chat_id'), payload.get('kind'), protected, int(time.time())))
            db.commit()

    def pending_attachments(self, limit=5, *, allowed_chats=None, kinds=None):
        # 在 LIMIT 前限定当前授权，避免前几项已排除群的附件挡住其他群；失败项按次数轮转。
        filters, arguments = ["status = 'pending'"], []
        for field, values in (('chat_id', allowed_chats), ('kind', kinds)):
            if values is not None:
                if not values:
                    return []
                filters.append(f"{field} IN ({','.join('?' for _ in values)})")
                arguments.extend(values)
        with self._connect() as db:
            rows = db.execute(f"SELECT payload_blob FROM queued_attachments WHERE {' AND '.join(filters)} ORDER BY attempts, created_at_epoch, attachment_key LIMIT ?",
                (*arguments, max(1, min(limit, 20)))).fetchall()
        return [json.loads(self.protector.unprotect(row[0]).decode('utf-8')) for row in rows]

    def remember_attachment_source(self, payload, record):
        """尚未下载的精确索引在本机加密保留七天，不随文本 ACK 丢失；条数与体积均有上限。"""
        if payload.get('unavailable_reason') != 'not_downloaded' or not record.get('candidate_paths'):
            return
        protected = self.protector.protect(self._canonical({'record': record, 'source_key': payload['source_key'], 'chat_id': payload['chat_id']}))
        now = int(time.time())
        with self._connect() as db:
            db.execute('DELETE FROM deferred_attachment_reads WHERE created_at_epoch < ?', (now - 7 * 86400,))
            if db.execute('SELECT 1 FROM deferred_attachment_reads WHERE attachment_key = ?', (payload['attachment_key'],)).fetchone():
                db.commit()
                return
            count, size = db.execute('SELECT COUNT(*), COALESCE(SUM(length(reference_blob)), 0) FROM deferred_attachment_reads').fetchone()
            if count >= 1000 or size + len(protected) > min(self.max_bytes, 1024 * 1024):
                raise QueueFullError('待下载附件索引已满；请先处理已有附件或稍后重试。')
            db.execute('INSERT INTO deferred_attachment_reads VALUES (?, ?, ?, ?, ?, ?)',
                (payload['attachment_key'], payload['chat_id'], payload['kind'], protected, now, now))
            db.commit()

    def deferred_attachment_sources(self, allowed_chats, kinds, limit=5):
        """只返回当前仍授权的群和附件类型，按下次尝试时间轮转，避免未下载文件阻塞其他项。"""
        if not allowed_chats or not kinds:
            return []
        now = int(time.time())
        chat_marks, kind_marks = ','.join('?' for _ in allowed_chats), ','.join('?' for _ in kinds)
        with self._connect() as db:
            db.execute('DELETE FROM deferred_attachment_reads WHERE created_at_epoch < ?', (now - 7 * 86400,))
            rows = db.execute(f'SELECT attachment_key, reference_blob FROM deferred_attachment_reads WHERE chat_id IN ({chat_marks}) AND kind IN ({kind_marks}) AND next_attempt_epoch <= ? ORDER BY next_attempt_epoch, created_at_epoch, attachment_key LIMIT ?',
                (*allowed_chats, *kinds, now, max(1, min(limit, 20)))).fetchall()
            for row in rows:
                db.execute('UPDATE deferred_attachment_reads SET next_attempt_epoch = ? WHERE attachment_key = ?', (now + 60, row['attachment_key']))
            db.commit()
        return [json.loads(self.protector.unprotect(row['reference_blob']).decode('utf-8')) for row in rows]

    def forget_attachment_source(self, key):
        """实际文件已经进入持久上传队列后，清除包含本机路径的恢复索引。"""
        with self._connect() as db:
            db.execute('DELETE FROM deferred_attachment_reads WHERE attachment_key = ?', (key,))
            db.commit()

    def acknowledge_attachment(self, key, response):
        """只有同键明确收据才能确认上传；服务器解析失败仍可在网页重试。"""
        if response.get('attachment_key') != key or response.get('status') not in {'queued', 'ready', 'converted', 'unavailable', 'failed'}:
            raise ValueError('附件回执不完整，保留本地队列')
        with self._connect() as db:
            row = db.execute('SELECT payload_blob FROM queued_attachments WHERE attachment_key = ?', (key,)).fetchone()
            if row is None:
                return
            payload = json.loads(self.protector.unprotect(row[0]).decode('utf-8'))
            # 收到持久化回执后移除本机队列中的文件正文，只留防重与恢复摘要。
            payload.pop('data_base64', None)
            protected = self.protector.protect(self._canonical(payload))
            db.execute("UPDATE queued_attachments SET status = 'acknowledged', payload_blob = ?, attempts = attempts + 1, last_error_code = NULL WHERE attachment_key = ?", (protected, key))
            db.commit()

    def mark_attachment_attempt(self, key, code):
        with self._connect() as db:
            db.execute('UPDATE queued_attachments SET attempts = attempts + 1, last_error_code = ? WHERE attachment_key = ?', (code[:80], key))
            db.commit()
