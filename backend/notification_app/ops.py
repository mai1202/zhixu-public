"""运行备份、隐私请求和本地演示所需的受控运维命令。"""

from datetime import datetime, time, timedelta, timezone
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
from time import time as epoch_time
from collections.abc import Mapping
from contextlib import closing
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as DbSession

from . import create_app
from .models import (
    AcademicTerm,
    AgentConversation,
    AgentTurn,
    Base,
    BusinessLink,
    ChangeSet,
    CourseException,
    CourseOccurrence,
    CourseSeries,
    DigestCursor,
    DigestPreference,
    EmailChallenge,
    Event,
    Job,
    JobAttempt,
    Notice,
    OperationLog,
    ScheduleEntry,
    SourceDocument,
    SourceSegment,
    Task,
    TimetableImport,
    User,
    VerifiedAddress,
    WebSession,
)
from .models.ocr import OcrArtifact
from .errors import ApiProblem
from .services.auth_service import normalize_gdufs_email, session_digest

UTC = timezone.utc
DEMO_EMAIL = "notification-demo@example.invalid"
DEMO_USER_ID = str(uuid5(NAMESPACE_URL, "notification-management/demo-user/v1"))


def _privacy_email_key(email: str) -> str:
    """按账户登录时的规范保留邮箱 local part 大小写，只转小写域名。"""
    try:
        _normalized_address, email_key = normalize_gdufs_email(email)
    except ApiProblem as error:
        raise ValueError(error.message) from None
    return email_key


def _manifest_path(destination: Path) -> Path:
    """为备份生成不含连接串的摘要清单路径。"""
    return destination.with_suffix(destination.suffix + ".manifest.json")


def _file_sha256(path: Path) -> str:
    """分块计算大备份摘要，避免将整库一次性载入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_backup_manifest(destination: Path, engine: Engine) -> dict[str, Any]:
    """记录备份摘要、数据库类型和格式版本，不保存任何连接凭证。"""
    checksum = _file_sha256(destination)
    manifest = {
        "format_version": 1,
        "created_at_epoch": int(epoch_time()),
        "database_dialect": engine.dialect.name,
        "filename": destination.name,
        "size_bytes": destination.stat().st_size,
        "sha256": checksum,
    }
    sidecar = _manifest_path(destination)
    temporary = sidecar.with_suffix(sidecar.suffix + ".part")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, sidecar)
    return manifest


def _postgres_environment(engine: Engine) -> dict[str, str]:
    """把 PostgreSQL 凭证放在子进程环境，避免出现在命令行参数和日志。"""
    url = engine.url
    environment = os.environ.copy()
    if url.host:
        environment["PGHOST"] = url.host
    if url.port:
        environment["PGPORT"] = str(url.port)
    if url.username:
        environment["PGUSER"] = url.username
    if url.password:
        environment["PGPASSWORD"] = url.password
    if url.database:
        environment["PGDATABASE"] = url.database
    for key in ("sslmode", "sslcert", "sslkey", "sslrootcert", "connect_timeout", "application_name"):
        value = url.query.get(key)
        if value is not None:
            environment[f"PG{key.upper()}"] = str(value)
    return environment


def _run_postgres_tool(command: list[str], engine: Engine, operation: str) -> None:
    """调用系统 PostgreSQL 工具；错误只报告工具退出码，不回显环境变量。"""
    try:
        subprocess.run(command, env=_postgres_environment(engine), check=True, capture_output=True)
    except FileNotFoundError:
        raise RuntimeError(f"{operation}需要安装 PostgreSQL 客户端工具。") from None
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{operation}失败，工具退出码为 {error.returncode}。") from None


def backup_database(engine: Engine, destination: str | Path, *, overwrite: bool = False) -> dict[str, Any]:
    """创建 SQLite 在线一致性备份或 PostgreSQL custom-format 备份。"""
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise FileExistsError("备份目标已存在；如确需覆盖，请使用 --overwrite。")
    manifest = _manifest_path(target)
    if manifest.exists() and not overwrite:
        raise FileExistsError("备份清单已存在；如确需覆盖，请使用 --overwrite。")
    staged = target.with_name(target.name + ".part")
    staged.unlink(missing_ok=True)
    try:
        if engine.dialect.name == "sqlite":
            source = Path(str(engine.url.database)).expanduser().resolve()
            if not source.is_file():
                raise FileNotFoundError("当前 SQLite 数据库文件不存在。")
            if source == target:
                raise ValueError("备份目标不能覆盖正在使用的数据库。")
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)")
            with closing(sqlite3.connect(source)) as source_db, closing(sqlite3.connect(staged)) as staged_db:
                source_db.backup(staged_db)
                result = staged_db.execute("PRAGMA integrity_check").fetchone()
                if result is None or result[0] != "ok":
                    raise RuntimeError("SQLite 备份完整性检查失败。")
                staged_db.commit()
        elif engine.dialect.name == "postgresql":
            if shutil.which("pg_dump") is None:
                raise RuntimeError("PostgreSQL 备份需要安装 pg_dump。")
            _run_postgres_tool(
                ["pg_dump", "--format=custom", "--no-owner", "--file", str(staged)],
                engine,
                "PostgreSQL 备份",
            )
            if not staged.is_file() or staged.stat().st_size == 0:
                raise RuntimeError("pg_dump 未产生有效备份文件。")
        else:
            raise RuntimeError("备份仅支持 SQLite 和 PostgreSQL。")
        os.replace(staged, target)
        return _write_backup_manifest(target, engine)
    finally:
        staged.unlink(missing_ok=True)


def verify_backup(backup_path: str | Path) -> dict[str, Any]:
    """校验备份清单的摘要，并检查 SQLite 备份自身的数据库完整性。"""
    path = Path(backup_path).expanduser().resolve()
    sidecar = _manifest_path(path)
    if not path.is_file() or not sidecar.is_file():
        raise FileNotFoundError("备份或配套摘要清单不存在。")
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    checksum = _file_sha256(path)
    if manifest.get("sha256") != checksum or manifest.get("size_bytes") != path.stat().st_size:
        raise ValueError("备份摘要或文件大小不匹配。")
    if manifest.get("database_dialect") == "sqlite":
        with closing(sqlite3.connect(path)) as database:
            result = database.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise ValueError("SQLite 备份完整性检查失败。")
    return manifest


def restore_database(engine: Engine, backup_path: str | Path, confirm_target: str) -> Path:
    """要求精确确认活动目标后恢复，并先创建可回滚的当前库备份。"""
    backup = Path(backup_path).expanduser().resolve()
    manifest = verify_backup(backup)
    if manifest.get("database_dialect") != engine.dialect.name:
        raise ValueError("备份数据库类型与当前 DATABASE_URL 不匹配。")
    if engine.dialect.name == "sqlite":
        target = Path(str(engine.url.database)).expanduser().resolve()
        if confirm_target != str(target):
            raise ValueError("--confirm-target 必须与当前 SQLite 数据库的绝对路径完全一致。")
        if backup == target:
            raise ValueError("恢复来源不能与活动数据库相同。")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rollback = target.with_name(f"{target.stem}.pre-restore-{stamp}{target.suffix}")
        backup_database(engine, rollback)
        staged = target.with_name(target.name + ".restore.part")
        staged.unlink(missing_ok=True)
        with closing(sqlite3.connect(backup)) as source_db, closing(sqlite3.connect(staged)) as target_db:
            source_db.backup(target_db)
            check = target_db.execute("PRAGMA integrity_check").fetchone()
            if check is None or check[0] != "ok":
                staged.unlink(missing_ok=True)
                raise RuntimeError("恢复副本完整性检查失败，活动数据库未替换。")
            target_db.commit()
        engine.dispose()
        os.replace(staged, target)
        return rollback
    if engine.dialect.name == "postgresql":
        target_name = str(engine.url.database or "")
        if confirm_target != target_name:
            raise ValueError("PostgreSQL --confirm-target 必须与 DATABASE_URL 的数据库名称完全一致。")
        if shutil.which("pg_restore") is None:
            raise RuntimeError("PostgreSQL 恢复需要安装 pg_restore。")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rollback = backup.with_name(f"{backup.stem}.pre-restore-{stamp}.dump")
        backup_database(engine, rollback)
        _run_postgres_tool(
            ["pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", target_name, str(backup)],
            engine,
            "PostgreSQL 恢復",
        )
        return rollback
    raise RuntimeError("恢复仅支持 SQLite 和 PostgreSQL。")


def _model_delete_order() -> list[type]:
    """根據外鍵依賴按子表優先排序，確保 owner 删除不留孤儿行。"""
    owned = {
        mapper.class_
        for mapper in Base.registry.mappers
        if "user_id" in mapper.class_.__table__.c and mapper.class_ is not User
    }
    tables = {model.__table__: model for model in owned}
    ordered: list[type] = []
    visiting: set[type] = set()
    visited: set[type] = set()

    def visit(model: type) -> None:
        if model in visited:
            return
        if model in visiting:
            raise RuntimeError("owner 数据外键存在环，已停止隐私删除。")
        visiting.add(model)
        for foreign_key in model.__table__.foreign_keys:
            parent = tables.get(foreign_key.column.table)
            if parent is not None:
                visit(parent)
        visiting.remove(model)
        visited.add(model)
        ordered.append(model)

    for model in owned:
        visit(model)
    # 子表在递归中先排入结果，所以此列表直接用于删除。
    return list(reversed(ordered))


def _owned_private_assets(db, user_id: str, storage_dir: Path):
    """从 owner 行取得私有素材键，补齐文档原件与解析失败的设备附件。"""
    from .models import AgentAttachment, DeviceAttachment
    private_root = (Path(current_app.config['DATA_DIR']) / 'private') if has_app_context() else storage_dir.resolve().parent
    result = []
    for item in db.scalars(select(AgentAttachment).where(AgentAttachment.user_id == user_id)):
        if Path(item.storage_key).name == item.storage_key:
            root = (private_root / 'agent').resolve()
            path = (root / item.storage_key).resolve()
            if path.parent == root:
                result.append((path, f'agent/{item.storage_key}'))
    for item in db.scalars(select(DeviceAttachment).where(DeviceAttachment.user_id == user_id)):
        root = (private_root / 'device-attachments').resolve()
        path = (root / item.id).resolve()
        if path.parent == root:
            result.append((path, f'device-attachments/{item.id}'))
    return result


def _delete_user_records(engine: Engine, user_id: str, email_key: str, storage_dir: Path) -> dict[str, int]:
    """事务性删除 owner 行，提交后再移除其私有 OCR 文件。"""
    removed_images: list[str] = []
    with DbSession(engine) as db, db.begin():
        if db.get(User, user_id) is None:
            return {"database_rows": 0, "ocr_files": 0}
        artifacts = db.scalars(select(OcrArtifact).where(OcrArtifact.user_id == user_id)).all()
        removed_images = [item.storage_key for item in artifacts if Path(item.storage_key).name == item.storage_key]
        private_assets = _owned_private_assets(db, user_id, storage_dir)

        jobs = db.scalars(select(Job.id).where(Job.user_id == user_id)).all()
        devices_table = Base.metadata.tables.get("devices")
        device_ids = db.scalars(select(devices_table.c.id).where(devices_table.c.user_id == user_id)).all() if devices_table is not None else []
        row_count = 0
        row_count += db.execute(delete(JobAttempt).where(JobAttempt.job_id.in_(jobs))).rowcount or 0
        # 设备凭证表没有 user_id，通过 owner 的设备 ID 限定删除范围。
        credentials = Base.metadata.tables.get("device_credentials")
        if credentials is not None:
            row_count += db.execute(delete(credentials).where(credentials.c.device_id.in_(device_ids))).rowcount or 0
        row_count += db.execute(delete(EmailChallenge).where(EmailChallenge.email_key == email_key)).rowcount or 0
        for model in _model_delete_order():
            row_count += db.execute(delete(model).where(model.user_id == user_id)).rowcount or 0
        row_count += db.execute(delete(User).where(User.id == user_id)).rowcount or 0

    removed_files = 0
    root = storage_dir.resolve()
    for storage_key in removed_images:
        path = (root / storage_key).resolve()
        if path.parent == root and path.is_file():
            path.unlink()
            removed_files += 1
    private_count = 0
    for path, _archive_name in private_assets:
        if path.is_file():
            path.unlink()
            private_count += 1
    return {"database_rows": row_count, "ocr_files": removed_files, 'private_files': private_count}


def export_user_data(engine: Engine, email_key: str, destination: str | Path, storage_dir: Path) -> dict[str, Any]:
    """把单一 owner 的记录与 OCR 附件打包，密钥、令牌和摘要字段脱敏。"""
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError("隐私导出目标已存在；请指定新的归档路径。")
    with DbSession(engine) as db:
        user = db.scalar(select(User).where(User.email_key == email_key))
        if user is None:
            raise LookupError("未找到该邮箱对应的账户。")
        tables: dict[str, list[dict[str, Any]]] = {}
        for mapper in Base.registry.mappers:
            model = mapper.class_
            if model is User:
                rows = [user]
            elif "user_id" in model.__table__.c:
                rows = db.scalars(select(model).where(model.user_id == user.id)).all()
            elif model is EmailChallenge:
                rows = db.scalars(select(model).where(model.email_key == user.email_key)).all()
            elif model is JobAttempt:
                job_ids = select(Job.id).where(Job.user_id == user.id)
                rows = db.scalars(select(model).where(model.job_id.in_(job_ids))).all()
            elif model.__tablename__ == "device_credentials":
                device_ids = select(Base.metadata.tables["devices"].c.id).where(
                    Base.metadata.tables["devices"].c.user_id == user.id
                )
                rows = db.execute(select(model.__table__).where(model.__table__.c.device_id.in_(device_ids))).mappings().all()
            else:
                continue
            if not rows:
                continue
            serialized: list[dict[str, Any]] = []
            for row in rows:
                if isinstance(row, Mapping):
                    data = dict(row)
                else:
                    # ORM 属性名可能与数据库列名不同；导出按真实列名保留结构，按映射属性读取。
                    data = {attribute.columns[0].key: getattr(row, attribute.key) for attribute in mapper.column_attrs}
                for key in list(data):
                    normalized = key.lower()
                    if any(marker in normalized for marker in ("ciphertext", "token", "secret", "password")):
                        data[key] = "[已省略敏感字段]"
                    elif normalized.endswith("_digest") and normalized not in {"content_digest"}:
                        data[key] = "[已省略摘要字段]"
                serialized.append(data)
            tables[model.__tablename__] = serialized
        artifacts = db.scalars(select(OcrArtifact).where(OcrArtifact.user_id == user.id)).all()
        artifact_keys = [item.storage_key for item in artifacts]
        private_assets = _owned_private_assets(db, user.id, storage_dir)

    missing_assets: list[str] = []
    included_assets: list[str] = []
    with ZipFile(target, "x", compression=ZIP_DEFLATED) as archive:
        included_private = []
        for path, archive_name in private_assets:
            if path.is_file():
                archive.write(path, archive_name)
                included_private.append(archive_name)
        for storage_key in artifact_keys:
            if Path(storage_key).name != storage_key:
                missing_assets.append(storage_key)
                continue
            asset_path = (storage_dir.resolve() / storage_key).resolve()
            if asset_path.parent != storage_dir.resolve() or not asset_path.is_file():
                missing_assets.append(storage_key)
                continue
            archive.write(asset_path, f"ocr/{storage_key}")
            included_assets.append(storage_key)
        payload = {
            "format_version": 1,
            "generated_at_epoch": int(epoch_time()),
            "user_id": user.id,
            "email": user.primary_email,
            "tables": tables,
            "ocr_files_included": included_assets,
            "ocr_files_missing": missing_assets,
            "private_files_included": included_private,
            "excluded_global_credentials": "运营级邮件 OAuth 凭证不属于个人账户导出。",
        }
        archive.writestr("data.json", json.dumps(payload, ensure_ascii=False, default=str, indent=2))
    return {"archive": str(target), "table_count": len(tables), "ocr_files": len(included_assets), "missing_ocr_files": len(missing_assets)}


def seed_demo_data(engine: Engine, storage_dir: Path) -> dict[str, int]:
    """重置专用合成账户并创建通知、任务、课程和主动服务演示样例。"""
    if current_app_environment() == "production":
        raise RuntimeError("生产环境禁止重置演示数据。")
    with DbSession(engine) as db:
        existing = db.scalar(select(User).where(User.id == DEMO_USER_ID))
        if existing is not None:
            _delete_user_records(engine, existing.id, existing.email_key, storage_dir)

    now = int(epoch_time())
    zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(UTC).astimezone(zone).date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    ids = lambda label: str(uuid5(NAMESPACE_URL, f"notification-management/demo/{label}/v1"))
    # 固定为第 7 个教学周，演示数据随真实运行日期移动，不依赖预先写死的日期。
    week1_monday = today - timedelta(days=today.weekday(), weeks=6)
    current_week = (today - week1_monday).days // 7 + 1
    term_id = ids("term")
    import_id = ids("timetable-import")
    math_series_id = ids("course-math")
    lab_series_id = ids("course-lab")
    math_occurrence_id = ids("occurrence-math")
    lab_occurrence_id = ids("occurrence-lab")
    math_source_id = ids("source-math")
    lab_source_id = ids("source-lab")
    math_notice_id = ids("notice-math")
    lab_notice_id = ids("notice-lab")
    math_event_id = ids("event-math")
    lab_event_id = ids("event-lab")
    math_task_id = ids("task-math")
    prep_task_id = ids("task-prep")
    exam_task_id = ids("task-exam")
    math_segment_id = ids("segment-math")
    lab_segment_id = ids("segment-lab")
    exception_id = ids("course-exception")
    math_change_id = ids("changeset-math")
    lab_change_id = ids("changeset-lab")
    conversation_id = ids("conversation")
    math_deadline_date = day_after
    prep_deadline_date = tomorrow
    exam_deadline_date = today + timedelta(days=4)

    def deadline(date_value, clock: str, raw: str) -> tuple[dict[str, Any], int]:
        local = datetime.combine(date_value, time.fromisoformat(clock), tzinfo=zone)
        payload = {
            "raw": raw,
            "local_date": date_value.isoformat(),
            "local_time": clock,
            "timezone": "Asia/Shanghai",
            "precision": "minute",
            "utc": local.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "basis": "合成演示数据，用于展示三端联动。",
        }
        return payload, int(local.timestamp())

    math_deadline, math_due_epoch = deadline(math_deadline_date, "20:00", f"{math_deadline_date.isoformat()} 20:00 前")
    prep_deadline, prep_due_epoch = deadline(prep_deadline_date, "18:00", f"{prep_deadline_date.isoformat()} 18:00 前")
    exam_deadline, exam_due_epoch = deadline(exam_deadline_date, "21:00", f"{exam_deadline_date.isoformat()} 晚间")
    math_start = datetime.combine(today, time(16, 10), tzinfo=zone)
    math_end = datetime.combine(today, time(17, 50), tzinfo=zone)
    lab_original_start = datetime.combine(tomorrow, time(10, 10), tzinfo=zone)
    lab_original_end = datetime.combine(tomorrow, time(11, 50), tzinfo=zone)
    lab_new_start = datetime.combine(day_after, time(19, 0), tzinfo=zone)
    lab_new_end = datetime.combine(day_after, time(20, 40), tzinfo=zone)
    math_week = (today - week1_monday).days // 7 + 1
    lab_week = (day_after - week1_monday).days // 7 + 1
    user = User(
        id=DEMO_USER_ID,
        primary_email=DEMO_EMAIL,
        email_key=DEMO_EMAIL,
        is_active=True,
        is_admin=False,
        change_sequence=1,
        verified_at_epoch=now,
        created_at_epoch=now,
    )
    schedule_rows = [
        (ids("schedule-today"), "研究方法讨论（演示）", today.isoformat(), "14:00", "15:00", "busy_block"),
        (ids("schedule-tomorrow"), "整理开题材料（演示）", tomorrow.isoformat(), "10:00", "11:30", "busy_block"),
        (ids("schedule-deadline"), "提交演示用材料", tomorrow.isoformat(), None, None, "deadline_marker"),
    ]
    with DbSession(engine) as db, db.begin():
        db.add(user)
        db.add(
            VerifiedAddress(
                id=ids("address"),
                user_id=DEMO_USER_ID,
                email=DEMO_EMAIL,
                email_key=DEMO_EMAIL,
                is_primary=True,
                verified_at_epoch=now,
            )
        )
        # 无 ORM relationship 的模型显式 flush 父行，保障 SQLite 外键顺序。
        db.flush()
        db.add(
            AcademicTerm(
                id=term_id,
                user_id=DEMO_USER_ID,
                term_code="2026-2027-1",
                week1_monday=week1_monday.isoformat(),
                week_count=18,
                timezone="Asia/Shanghai",
                period_profile_json=[
                    {"periods": [1, 2], "start_time": "08:00", "end_time": "09:40", "source_label": "演示作息"},
                    {"periods": [3, 4], "start_time": "10:10", "end_time": "11:50", "source_label": "演示作息"},
                    {"periods": [5, 6], "start_time": "12:30", "end_time": "14:10", "source_label": "演示作息"},
                    {"periods": [7, 8], "start_time": "16:10", "end_time": "17:50", "source_label": "演示作息"},
                    {"periods": [9, 10], "start_time": "19:00", "end_time": "20:40", "source_label": "演示作息"},
                ],
                anchor_source="演示数据按当前日期动态生成。",
                version=1,
                created_at_epoch=now,
                updated_at_epoch=now,
            )
        )
        db.flush()
        db.add(
            TimetableImport(
                id=import_id,
                user_id=DEMO_USER_ID,
                term_id=term_id,
                term_code="2026-2027-1",
                file_hash=hashlib.sha256(b"notification-management-demo-timetable-v2").hexdigest(),
                filename="演示课表.xlsx",
                file_format="xlsx",
                status="confirmed",
                apply_mode="merge",
                draft_json={"source": "demo"},
                diff_json={"summary": {"added": 2, "changed": 0, "removed": 0, "unchanged": 0}},
                version=2,
                created_at_epoch=now,
                updated_at_epoch=now,
                confirmed_at_epoch=now,
            )
        )
        db.add(
            SourceDocument(
                id=math_source_id,
                user_id=DEMO_USER_ID,
                source_type="demo",
                title="【合成】高等数学作业延期",
                original_text=f"【合成演示材料】高等数学第三章作业延期至 {math_deadline_date.isoformat()} 20:00。",
                content_hash=hashlib.sha256(b"notification-management-demo-math-v2").hexdigest(),
                external_key="demo-math-v2",
                analysis_state="processed",
                analysis_mode="rules",
                provider_profile_id=None,
                created_at_epoch=now,
                updated_at_epoch=now,
                archived_at_epoch=None,
            )
        )
        db.add(
            SourceDocument(
                id=lab_source_id,
                user_id=DEMO_USER_ID,
                source_type="demo",
                title="【合成】计算机组成原理实验调整",
                original_text="【合成演示材料】实验调整到后天19:00，地点改为实验楼C308；请于明天18:00前完成预习报告。",
                content_hash=hashlib.sha256(b"notification-management-demo-lab-v2").hexdigest(),
                external_key="demo-lab-v2",
                analysis_state="processed",
                analysis_mode="rules",
                provider_profile_id=None,
                created_at_epoch=now,
                updated_at_epoch=now,
                archived_at_epoch=None,
            )
        )
        db.flush()
        db.add(
            SourceSegment(
                id=math_segment_id, user_id=DEMO_USER_ID, source_document_id=math_source_id,
                ordinal=0, text=f"高等数学第三章作业延期至 {math_deadline_date.isoformat()} 20:00。",
                start_offset=8, end_offset=50,
                version=1, created_at_epoch=now,
            )
        )
        db.add(
            SourceSegment(
                id=lab_segment_id, user_id=DEMO_USER_ID, source_document_id=lab_source_id,
                ordinal=0, text="实验调整到后天19:00，地点改为实验楼C308；请于明天18:00前完成预习报告。",
                start_offset=8, end_offset=62, version=1, created_at_epoch=now,
            )
        )
        db.add(
            Notice(
                id=math_notice_id, user_id=DEMO_USER_ID, source_document_id=math_source_id,
                title="高等数学作业延期", importance="high",
                last_change_set_id=math_change_id, status="active", version=1,
                created_at_epoch=now, updated_at_epoch=now, archived_at_epoch=None,
            )
        )
        db.add(
            Notice(
                id=lab_notice_id, user_id=DEMO_USER_ID, source_document_id=lab_source_id,
                title="计算机组成原理实验调整", importance="high",
                last_change_set_id=lab_change_id, status="active", version=1,
                created_at_epoch=now, updated_at_epoch=now, archived_at_epoch=None,
            )
        )
        db.flush()
        db.add(
            Event(
                id=math_event_id, user_id=DEMO_USER_ID, notice_id=math_notice_id,
                source_document_id=math_source_id, title="提交高等数学第三章作业",
                actor="合成课程教师", stage="课程作业", scope="仅本地合成演示账户",
                deadline_json=math_deadline, time_range_json=None, materials_json=["第三章习题"],
                submission="线下提交，演示中不对应真实渠道", eligibility="unknown",
                evidence_json={"deadline": [{
                    "segment_id": math_segment_id,
                    "quote": f"高等数学第三章作业延期至 {math_deadline_date.isoformat()} 20:00",
                }]},
                unknowns_json=["合成材料时间按当前运行日动态生成"], status="open", version=1,
                created_at_epoch=now, updated_at_epoch=now, archived_at_epoch=None,
            )
        )
        db.add(
            Event(
                id=lab_event_id, user_id=DEMO_USER_ID, notice_id=lab_notice_id,
                source_document_id=lab_source_id, title="完成计算机组成原理实验预习报告",
                actor="合成实验教师", stage="实验准备", scope="仅本地合成演示账户",
                deadline_json=prep_deadline, time_range_json=None, materials_json=["预习报告"],
                submission="线下提交", eligibility="unknown",
                evidence_json={"deadline": [{"segment_id": lab_segment_id, "quote": "明天18:00前完成预习报告"}]},
                unknowns_json=["合成材料不代表真实课程要求"], status="open", version=1,
                created_at_epoch=now, updated_at_epoch=now, archived_at_epoch=None,
            )
        )
        db.flush()
        db.add(
            CourseSeries(
                id=math_series_id, user_id=DEMO_USER_ID, term_id=term_id, source_import_id=import_id,
                identity_key=hashlib.sha256(f"{DEMO_USER_ID}:math".encode()).hexdigest(),
                title="高等数学（演示）", display_name="高等数学（演示）", teachers_json=["合成张老师"],
                room="教学楼A101", week_set_json=list(range(1, 19)), weekday=today.isoweekday(),
                periods_json=[7, 8], raw_text="高等数学（演示）按当前星期生成", status="active",
                version=1, created_at_epoch=now, updated_at_epoch=now,
            )
        )
        db.add(
            CourseSeries(
                id=lab_series_id, user_id=DEMO_USER_ID, term_id=term_id, source_import_id=import_id,
                identity_key=hashlib.sha256(f"{DEMO_USER_ID}:lab".encode()).hexdigest(),
                title="计算机组成原理实验（演示）", display_name="计算机组成原理实验（演示）",
                teachers_json=["合成李老师"], room="实验楼B202", week_set_json=list(range(1, 19)),
                weekday=tomorrow.isoweekday(), periods_json=[3, 4],
                raw_text="计算机组成原理实验（演示）按当前星期生成", status="active",
                version=1, created_at_epoch=now, updated_at_epoch=now,
            )
        )
        db.flush()
        db.add(
            CourseOccurrence(
                id=math_occurrence_id, user_id=DEMO_USER_ID, term_id=term_id,
                course_series_id=math_series_id, source_import_id=import_id,
                occurrence_key=f"{math_series_id}:{today.isoformat()}", local_date=today.isoformat(),
                week_number=math_week, display_start="16:10", display_end="17:50",
                busy_segments_json=[{"start_at": math_start.isoformat(), "end_at": math_end.isoformat()}],
                room_override=None, status="scheduled", version=1,
            )
        )
        db.add(
            CourseOccurrence(
                id=lab_occurrence_id, user_id=DEMO_USER_ID, term_id=term_id,
                course_series_id=lab_series_id, source_import_id=import_id,
                occurrence_key=f"{lab_series_id}:{tomorrow.isoformat()}", local_date=day_after.isoformat(),
                week_number=lab_week, display_start="19:00", display_end="20:40",
                busy_segments_json=[{"start_at": lab_new_start.isoformat(), "end_at": lab_new_end.isoformat()}],
                room_override="实验楼C308", status="rescheduled", version=2,
            )
        )
        db.flush()
        db.add(
            Task(
                id=math_task_id, user_id=DEMO_USER_ID, event_id=math_event_id,
                source_document_id=math_source_id, notice_id=math_notice_id,
                course_series_id=math_series_id, course_occurrence_id=math_occurrence_id,
                title="完成高等数学第三章作业", description="按最新通知完成并核对提交要求。",
                task_kind="assignment", priority="high", due_json=math_deadline, due_at_epoch=math_due_epoch,
                original_requirement=f"高等数学第三章作业延期至 {math_deadline_date.isoformat()} 20:00。",
                reminder_enabled=False,
                status="open", version=1, created_at_epoch=now, updated_at_epoch=now,
                completed_at_epoch=None, archived_at_epoch=None,
            )
        )
        db.add(
            Task(
                id=prep_task_id, user_id=DEMO_USER_ID, event_id=lab_event_id,
                source_document_id=lab_source_id, notice_id=lab_notice_id,
                course_series_id=lab_series_id, course_occurrence_id=lab_occurrence_id,
                title="完成计算机组成原理实验预习报告", description="实验已调整，预习要求保持不变。",
                task_kind="preparation", priority="urgent", due_json=prep_deadline, due_at_epoch=prep_due_epoch,
                original_requirement="明天18:00前完成预习报告。", reminder_enabled=False,
                status="open", version=1, created_at_epoch=now, updated_at_epoch=now,
                completed_at_epoch=None, archived_at_epoch=None,
            )
        )
        db.add(
            Task(
                id=exam_task_id, user_id=DEMO_USER_ID, event_id=None, source_document_id=None,
                notice_id=None, course_series_id=math_series_id, course_occurrence_id=None,
                title="复习高等数学随堂测验", description="用户手工补充的个人事务。",
                task_kind="exam", priority="normal", due_json=exam_deadline, due_at_epoch=exam_due_epoch,
                original_requirement=None, reminder_enabled=False, status="open", version=1,
                created_at_epoch=now, updated_at_epoch=now, completed_at_epoch=None, archived_at_epoch=None,
            )
        )
        for entry_id, title, local_date, start, end, kind in schedule_rows:
            db.add(
                ScheduleEntry(
                    id=entry_id,
                    user_id=DEMO_USER_ID,
                    source_event_id=lab_event_id if kind == "deadline_marker" else None,
                    title=title,
                    kind=kind,
                    local_date=local_date,
                    start_at=f"{local_date}T{start}:00+08:00" if start else None,
                    end_at=f"{local_date}T{end}:00+08:00" if end else None,
                    timezone="Asia/Shanghai",
                    status="active",
                    version=1,
                    created_at_epoch=now,
                    updated_at_epoch=now,
                    archived_at_epoch=None,
                )
            )
        db.add(
            ChangeSet(
                id=math_change_id,
                user_id=DEMO_USER_ID,
                source_document_id=math_source_id,
                kind="operations",
                status="confirmed",
                summary="Agent 登记高数作业并关联课程",
                bundle_json=None,
                operations_json=[
                    {"type": "created", "object_type": "task", "object_id": math_task_id, "applied_version": 1},
                ],
                version=2,
                change_seq=1,
                created_at_epoch=now,
                updated_at_epoch=now,
                confirmed_at_epoch=now,
                undone_at_epoch=None,
            )
        )
        db.add(
            ChangeSet(
                id=lab_change_id,
                user_id=DEMO_USER_ID,
                source_document_id=lab_source_id,
                kind="operations",
                status="confirmed",
                summary="Agent 将实验改期到后天 19:00 并保留预习任务",
                bundle_json=None,
                operations_json=[
                    {
                        "type": "course_exception_created",
                        "object_type": "course_exception",
                        "object_id": exception_id,
                        "occurrence_id": lab_occurrence_id,
                        "before": {
                            "local_date": tomorrow.isoformat(),
                            "display_start": "10:10",
                            "display_end": "11:50",
                            "busy_segments": [{
                                "start_at": lab_original_start.isoformat(),
                                "end_at": lab_original_end.isoformat(),
                            }],
                            "room_override": None,
                            "status": "scheduled",
                        },
                        "applied_version": 2,
                    }
                ],
                version=2,
                change_seq=2,
                created_at_epoch=now,
                updated_at_epoch=now,
                confirmed_at_epoch=now,
                undone_at_epoch=None,
            )
        )
        db.flush()
        db.add(
            CourseException(
                id=exception_id, user_id=DEMO_USER_ID, occurrence_id=lab_occurrence_id,
                notice_id=lab_notice_id, source_document_id=lab_source_id, change_set_id=lab_change_id,
                actor_type="agent", kind="reschedule",
                original_json={
                    "local_date": tomorrow.isoformat(),
                    "display_start": "10:10",
                    "display_end": "11:50",
                    "busy_segments": [{
                        "start_at": lab_original_start.isoformat(),
                        "end_at": lab_original_end.isoformat(),
                    }],
                    "room_override": None,
                    "status": "scheduled",
                },
                replacement_json={
                    "start_at": lab_new_start.isoformat(),
                    "end_at": lab_new_end.isoformat(),
                    "room": "实验楼C308",
                },
                evidence_json={"refs": [{"segment_id": lab_segment_id, "quote": "实验调整到后天19:00"}]},
                reason="实验教师发布临时改期通知。", status="active", version=1, created_at_epoch=now,
            )
        )
        for link_id, from_type, from_id, to_type, to_id, relation, source_id in (
            (ids("link-math-task"), "notice", math_notice_id, "task", math_task_id, "task_update_source", math_source_id),
            (ids("link-math-course"), "notice", math_notice_id, "course", math_series_id, "course_context", math_source_id),
            (ids("link-lab-course"), "notice", lab_notice_id, "course_occurrence", lab_occurrence_id, "course_change", lab_source_id),
            (ids("link-lab-task"), "notice", lab_notice_id, "task", prep_task_id, "task_update_source", lab_source_id),
            (ids("link-task-math"), "task", math_task_id, "course", math_series_id, "course_context", math_source_id),
        ):
            db.add(
                BusinessLink(
                    id=link_id, user_id=DEMO_USER_ID, from_type=from_type, from_id=from_id,
                    to_type=to_type, to_id=to_id, relation=relation,
                    evidence_json={"source": "合成演示三端联动"}, source_document_id=source_id,
                    status="active", version=1, created_at_epoch=now, updated_at_epoch=now,
                )
            )
        db.add(
            OperationLog(
                id=ids("operation-course"), user_id=DEMO_USER_ID, actor_type="agent", actor_id=None,
                action="course.reschedule", object_type="course_occurrence", object_id=lab_occurrence_id,
                change_set_id=lab_change_id, source_document_id=lab_source_id,
                before_json={
                    "local_date": tomorrow.isoformat(), "start": "10:10", "end": "11:50", "room": "实验楼B202",
                },
                after_json={
                    "local_date": day_after.isoformat(), "start": "19:00", "end": "20:40", "room": "实验楼C308",
                },
                summary="Agent 将计算机组成原理实验调整到后天 19:00，并保留预习报告任务。",
                status="applied", undoable=True, created_at_epoch=now,
            )
        )
        db.add(
            OperationLog(
                id=ids("operation-task"), user_id=DEMO_USER_ID, actor_type="agent", actor_id=None,
                action="task.create", object_type="task", object_id=math_task_id,
                change_set_id=math_change_id, source_document_id=math_source_id,
                before_json={}, after_json={"title": "完成高等数学第三章作业", "priority": "high"},
                summary="Agent 从高等数学通知创建作业并关联课程。",
                status="applied", undoable=True, created_at_epoch=now - 1,
            )
        )
        db.add(
            AgentConversation(
                id=conversation_id,
                user_id=DEMO_USER_ID,
                title="演示：校园事务巡检",
                status="active",
                version=1,
                created_at_epoch=now,
                updated_at_epoch=now,
                archived_at_epoch=None,
            )
        )
        db.add(
            AgentTurn(
                id=ids("agent-turn"),
                user_id=DEMO_USER_ID,
                conversation_id=conversation_id,
                run_id=None,
                sequence=1,
                role="assistant",
                content="这是本地合成演示数据。可以检查今天课程、临近截止、实验调整依据和关联任务，并生成日报。",
                metadata_json={"demo": True},
                created_at_epoch=now,
            )
        )
        db.add(
            DigestPreference(
                id=ids("digest-preference"),
                user_id=DEMO_USER_ID,
                enabled=False,
                timezone="Asia/Shanghai",
                send_time="07:30",
                recipient_address_id=ids("address"),
                include_json={"courses": True, "arrangements": True, "deadlines": True, "changes": True, "pending": True},
                version=1,
                next_send_epoch=None,
                weekly_enabled=False,
                reminder_enabled=False,
                notification_flags_json={
                    "task_reminder": False,
                    "important_notice": False,
                    "course_change": False,
                    "conflict_warning": False,
                },
                quiet_hours_json={"enabled": True, "start": "22:00", "end": "07:00"},
                created_at_epoch=now,
                updated_at_epoch=now,
            )
        )
        db.add(DigestCursor(user_id=DEMO_USER_ID, last_change_seq=2, last_sent_local_date=None, updated_at_epoch=now))

    report_count = 0
    try:
        # 演示报告也走正式事实聚合，避免手工写一份看似真实但不可追溯的正文。
        from .services import insight_service, report_service

        insight_service.sync_user_insights(DEMO_USER_ID, now_epoch=now)
        report_service.generate_report(
            DEMO_USER_ID,
            kind="daily",
            period_start=today,
            send=False,
        )
        report_count = 1
    except Exception:
        # 主动洞察或报告失败不应让演示账户的基础数据缺失。
        report_count = 0
    return {
        "notices": 2,
        "events": 2,
        "tasks": 3,
        "schedule_entries": len(schedule_rows),
        "courses": 2,
        "course_occurrences": 2,
        "course_exceptions": 1,
        "reports": report_count,
    }


def current_app_environment() -> str:
    """读取当前 Flask 工厂配置，不打印环境变量值。"""

    return str(current_app.config.get("APP_ENV", "development"))


def issue_demo_session(engine: Engine, auth_key: str, cookie_name: str) -> dict[str, str]:
    """仅为本地合成账户签发短时会话，并返回可直接复制到浏览器的 Cookie 值。"""
    if current_app_environment() != "development":
        raise RuntimeError("演示会话只允许 APP_ENV=development。")
    now = int(epoch_time())
    raw_token = secrets.token_urlsafe(32)
    csrf_token = hmac.new(auth_key.encode("utf-8"), f"csrf:{raw_token}".encode("utf-8"), hashlib.sha256).hexdigest()
    with DbSession(engine) as db, db.begin():
        user = db.get(User, DEMO_USER_ID)
        if user is None or user.primary_email != DEMO_EMAIL:
            raise LookupError("请先运行 demo-reset 创建合成数据。")
        db.add(
            WebSession(
                id=str(uuid4()),
                user_id=DEMO_USER_ID,
                token_digest=session_digest(raw_token),
                created_at_epoch=now,
                expires_at_epoch=now + 3600,
                revoked_at_epoch=None,
            )
        )
    return {"cookie_name": cookie_name, "cookie_value": raw_token, "csrf_token": csrf_token}


def _build_parser() -> argparse.ArgumentParser:
    """定义带显式目标确认的运维命令行接口。"""
    parser = argparse.ArgumentParser(description="通知管理受控运维工具")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="创建数据库备份")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--overwrite", action="store_true")
    verify = commands.add_parser("verify-backup", help="校验备份摘要和 SQLite 完整性")
    verify.add_argument("backup", type=Path)
    restore = commands.add_parser("restore", help="恢复备份并保存恢复前快照")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--confirm-target", required=True, help="必须填写 DATABASE_URL 对应的绝对路径或数据库名")
    export = commands.add_parser("privacy-export", help="导出一个账户的数据与附件")
    export.add_argument("email")
    export.add_argument("destination", type=Path)
    erase = commands.add_parser("privacy-delete", help="删除一个账户及其私有附件")
    erase.add_argument("email")
    erase.add_argument("--confirm-email", required=True, help="必须与账户主邮箱逐字一致")
    demo_reset = commands.add_parser("demo-reset", help="重建固定合成账户和演示数据")
    demo_login = commands.add_parser("demo-issue-session", help="仅开发环境签发短时合成账户会话")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行运维命令；安全错误只报告受控说明，不记录数据库 URL。"""
    arguments = _build_parser().parse_args(argv)
    app = create_app()
    engine: Engine = app.extensions["notification_db_engine"]
    try:
        with app.app_context():
            if arguments.command == "backup":
                result = backup_database(engine, arguments.destination, overwrite=arguments.overwrite)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif arguments.command == "verify-backup":
                result = verify_backup(arguments.backup)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif arguments.command == "restore":
                rollback = restore_database(engine, arguments.backup, arguments.confirm_target)
                print(json.dumps({"status": "restored", "rollback_backup": str(rollback)}, ensure_ascii=False))
            elif arguments.command == "privacy-export":
                email_key = _privacy_email_key(arguments.email)
                result = export_user_data(engine, email_key, arguments.destination, Path(app.config["OCR_STORAGE_DIR"]))
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif arguments.command == "privacy-delete":
                if arguments.email != arguments.confirm_email:
                    raise ValueError("--confirm-email 必须与 --email 完全相同。")
                email_key = _privacy_email_key(arguments.email)
                with DbSession(engine) as db:
                    user = db.scalar(select(User).where(User.email_key == email_key))
                    if user is None:
                        raise LookupError("未找到该邮箱对应的账户。")
                    if user.is_admin:
                        raise ValueError("为避免删除部署管理员，请先撤销管理员权限。")
                    user_id = user.id
                result = _delete_user_records(engine, user_id, email_key, Path(app.config["OCR_STORAGE_DIR"]))
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif arguments.command == "demo-reset":
                result = seed_demo_data(engine, Path(app.config["OCR_STORAGE_DIR"]))
                print(json.dumps({"demo_email": DEMO_EMAIL, **result}, ensure_ascii=False, indent=2))
            elif arguments.command == "demo-issue-session":
                result = issue_demo_session(
                    engine,
                    str(app.config.get("AUTH_HMAC_KEY") or ""),
                    str(app.config["SESSION_COOKIE_NAME"]),
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (FileNotFoundError, FileExistsError, LookupError, OSError, RuntimeError, ValueError) as error:
        print(f"运维命令失败：{error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
