"""跨进程的账户级规划序列，所有 ORM 业务写入共享同一数据库锁。"""

import hashlib
import json
import time
from uuid import uuid4

from flask import current_app, has_app_context
from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session

from ..models import (CourseOccurrence, CourseSeries, Event, Job, ScheduleEntry,
    StudentIntent, StudentJourney, Task, User, UserPlanningState)


def digest(value):
    """同一输入稳定哈希，同时拒绝非标准 JSON 数值。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def lock(db, user_id):
    """SQLite 使用真实写锁，PostgreSQL 使用行更新锁，不依赖进程内互斥。"""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    with db.no_autoflush:
        db.execute(insert(UserPlanningState).values(user_id=user_id, automatic=False,
            availability_json={}, journey_ids_json=[], version=1, policy_version=1, changed_at_epoch=0).on_conflict_do_nothing())
        db.execute(update(UserPlanningState).where(UserPlanningState.user_id == user_id)
            .values(version=UserPlanningState.version).execution_options(synchronize_session=False))
        return db.scalar(select(UserPlanningState).where(UserPlanningState.user_id == user_id)
            .execution_options(populate_existing=True))


def enqueue(db, user_id, kind, payload, key, *, delay=0):
    """业务变化和后续任务同事务落盘，提交失败不会留下孤立任务。"""
    now = int(time.time())
    if kind == "planning.replan":
        # 使用滚动防抖：最后一次变化之后60秒再计算，跨分钟边界也只保留一个待执行任务。
        pending = next((item for item in db.new if isinstance(item, Job) and item.user_id == user_id
            and item.kind == kind and item.status == "queued"), None)
        if pending is None:
            with db.no_autoflush:
                pending = db.scalar(select(Job).where(Job.user_id == user_id, Job.kind == kind,
                    Job.status.in_(("queued", "retry_wait"))).order_by(Job.created_at_epoch).limit(1))
        if pending:
            pending.run_at_epoch = now + delay
            return pending.id
    existing = db.scalar(select(Job).where(Job.user_id == user_id, Job.kind == kind, Job.idempotency_key == key))
    if existing:
        if kind != "planning.replan": return existing.id
        key = f"{key}:{uuid4().hex[:12]}"
    identifier = str(uuid4())
    db.add(Job(id=identifier, user_id=user_id, kind=kind, status="queued", priority=30,
        payload_json=payload, payload_hash=digest(payload), run_at_epoch=now + delay,
        attempt=0, max_attempts=3, idempotency_key=key, input_version=1, lock_version=1,
        created_at_epoch=now, updated_at_epoch=now))
    return identifier


@event.listens_for(Session, "before_flush")
def invalidate_planning(db, flush_context, instances):
    """捕获既有页面和 Agent 的 ORM 写入，旧方案不会漏掉旁路更改。"""
    if not has_app_context():
        return
    relevant = (Event, Task, StudentJourney, StudentIntent, ScheduleEntry, CourseOccurrence, CourseSeries)
    owners = {obj.user_id for obj in db.new.union(db.dirty).union(db.deleted)
        if isinstance(obj, relevant) and (obj in db.new or obj in db.deleted or db.is_modified(obj))}
    # 合成演示可能在同一 flush 新建用户，父行尚未插入时不创建规划外键。
    owners -= {obj.id for obj in db.new if isinstance(obj, User)}
    for user_id in sorted(owners):
        state = lock(db, user_id)
        for obj in list(db.dirty):
            if not isinstance(obj, relevant) or obj.user_id != user_id or not db.is_modified(obj): continue
            # 页面可能先读版本、后等待锁；获得锁后还要重新比较数据库版本。
            history = inspect(obj).attrs.version.history
            expected = history.deleted[0] if history.deleted else obj.version
            with db.no_autoflush:
                identity_column = getattr(type(obj), "id", type(obj).user_id)
                stored = db.scalar(select(type(obj).version).where(identity_column == getattr(obj, identity_column.key)))
            if stored is not None and stored != expected:
                from ..errors import ApiProblem
                raise ApiProblem("version_conflict", "对象在等待提交期间已变化，请刷新。", 409)
        state.version += 1
        state.changed_at_epoch = int(time.time())
        if state.automatic and not db.info.get("planning_applying"):
            enqueue(db, user_id, "planning.replan", {}, f"planning:{int(time.time()) // 60}", delay=60)
