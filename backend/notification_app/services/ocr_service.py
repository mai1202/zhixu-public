from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from flask import current_app
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from werkzeug.datastructures import FileStorage

from ..errors import ApiProblem
from ..jobs.queue import ClaimedJob, enqueue_job
from ..models import Job, OcrArtifact, SourceDocument, SourceSegment
from ..ocr.server_runner import OcrRunner, OcrRunnerError
from ..schemas.business import CreateSourceInput
from ..schemas.ocr import OcrResultInput, OcrSourceInput
from .business_service import create_source

_runner: OcrRunner | None = None


def _engine():
    return current_app.extensions["notification_db_engine"]


def _now() -> int:
    return int(time.time())


def _inspect_image(data: bytes) -> dict[str, Any]:
    """校验图片格式/体积/像素并读 EXIF 旋转，不信任扩展名或 MIME 头。"""
    maximum = int(current_app.config["OCR_MAX_UPLOAD_BYTES"])
    if not data or len(data) > maximum:
        raise ApiProblem("image_size_invalid", "图片大小必须在 1 字节到 20 MiB 之间。", 413)
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = image.format
            if image_format not in {"PNG", "JPEG"}:
                raise ApiProblem("image_type_unsupported", "目前只接受 PNG 或 JPEG 图片。", 415)
            width, height = image.size
            if width < 1 or height < 1 or width * height > int(current_app.config["OCR_MAX_IMAGE_PIXELS"]):
                raise ApiProblem("image_pixels_invalid", "图片尺寸无效或超过 2500 万像素。", 413)
            image.verify()
        # verify() 必须紧跟首次 open；重新打开后再读 EXIF，避免先触发像素解码。
        with Image.open(BytesIO(data)) as image:
            orientation_tag = image.getexif().get(274, 1)
        orientation = {3: 180, 6: 90, 8: 270}.get(orientation_tag, 0)
        if orientation in {90, 270}:
            width, height = height, width
    except ApiProblem:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ApiProblem("image_invalid", "无法读取该图片，请重新导出后重试。", 422) from None
    return {
        "media_type": "image/png" if image_format == "PNG" else "image/jpeg",
        "extension": "png" if image_format == "PNG" else "jpg",
        "width": width,
        "height": height,
        "orientation": orientation,
        "image_hash": sha256(data).hexdigest(),
    }


def _read_upload(upload: FileStorage) -> tuple[bytes, dict[str, Any]]:
    data = upload.stream.read(int(current_app.config["OCR_MAX_UPLOAD_BYTES"]) + 1)
    return data, _inspect_image(data)


def _store_image(data: bytes, *, extension: str) -> str:
    """使用随机键写入私有数据目录，文件名不来自用户输入。"""
    storage_root = Path(str(current_app.config["OCR_STORAGE_DIR"])).expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    storage_key = f"{uuid4().hex}.{extension}"
    path = storage_root / storage_key
    with path.open("xb") as handle:
        handle.write(data)
    if os.name != "nt":
        path.chmod(0o600)
    return storage_key


def _image_path(storage_key: str) -> Path:
    """只解析服务端生成的单段文件键，不接受目录分隔符。"""
    if not storage_key or Path(storage_key).name != storage_key:
        raise ApiProblem("image_missing", "找不到私有图片文件。", 404)
    root = Path(str(current_app.config["OCR_STORAGE_DIR"])).expanduser().resolve()
    path = (root / storage_key).resolve()
    if path.parent != root or not path.is_file():
        raise ApiProblem("image_missing", "找不到私有图片文件。", 404)
    return path


def _artifact_by_owner(user_id: str, artifact_id: str) -> OcrArtifact:
    with DbSession(_engine()) as db:
        artifact = db.scalar(
            select(OcrArtifact).where(
                OcrArtifact.id == artifact_id,
                OcrArtifact.user_id == user_id,
            )
        )
        if artifact is None:
            raise ApiProblem("not_found", "未找到该 OCR 图片。", 404)
        return deepcopy(artifact)


def _artifact_status(artifact: OcrArtifact, db: DbSession) -> tuple[str, str | None]:
    if artifact.result_json is not None:
        return "succeeded", None
    if not artifact.job_id:
        return artifact.status, artifact.failure_code
    job = db.scalar(select(Job).where(Job.id == artifact.job_id, Job.user_id == artifact.user_id))
    if job is None:
        return "failed", "job_missing"
    if job.status in {"queued", "leased", "running", "retry_wait"}:
        return job.status, job.error_code
    if job.status in {"failed", "expired"}:
        return "failed", job.error_code
    if job.status == "cancelled":
        return "cancelled", job.error_code
    return artifact.status, artifact.failure_code


def artifact_public(artifact: OcrArtifact) -> dict[str, Any]:
    with DbSession(_engine()) as db:
        current = db.scalar(
            select(OcrArtifact).where(
                OcrArtifact.id == artifact.id,
                OcrArtifact.user_id == artifact.user_id,
            )
        )
        if current is None:
            raise ApiProblem("not_found", "未找到该 OCR 图片。", 404)
        status, failure_code = _artifact_status(current, db)
    return {
        "id": artifact.id,
        "source_document_id": artifact.source_document_id,
        "status": status,
        "failure_code": failure_code,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "image_hash": artifact.image_hash,
        "width": artifact.width,
        "height": artifact.height,
        "orientation": artifact.orientation,
        "version": artifact.version,
        "result": artifact.result_json,
        "corrected_items": artifact.corrected_items_json,
        "image_url": f"/api/v1/ocr/artifacts/{artifact.id}/image",
        "created_at_epoch": artifact.created_at_epoch,
    }


def create_browser_artifact(user_id: str, upload: FileStorage, result_payload: dict[str, Any]) -> dict[str, Any]:
    """保存前端 OCR 输出与原图，校验哈希和坐标后等待用户核对。"""
    data, image = _read_upload(upload)
    try:
        result = OcrResultInput.model_validate(result_payload)
    except Exception:
        raise ApiProblem("ocr_result_invalid", "浏览器 OCR 结果不符合共享契约。", 422) from None
    if result.execution_location != "browser":
        raise ApiProblem("ocr_location_invalid", "浏览器结果必须标记 browser 执行位置。", 422)
    if result.image_hash != image["image_hash"] or (result.width, result.height) != (image["width"], image["height"]):
        raise ApiProblem("ocr_image_mismatch", "OCR 结果与上传图片不匹配，请重新识别。", 422)

    artifact_id = str(uuid4())
    storage_key = _store_image(data, extension=image["extension"])
    now = _now()
    artifact = OcrArtifact(
        id=artifact_id,
        user_id=user_id,
        job_id=None,
        source_document_id=None,
        storage_key=storage_key,
        media_type=image["media_type"],
        size_bytes=len(data),
        image_hash=image["image_hash"],
        width=image["width"],
        height=image["height"],
        orientation=image["orientation"],
        status="succeeded",
        result_json=result.model_dump(mode="json"),
        corrected_items_json=None,
        failure_code=None,
        version=1,
        created_at_epoch=now,
        updated_at_epoch=now,
    )
    try:
        with DbSession(_engine()) as db, db.begin():
            db.add(artifact)
    except Exception:
        _image_path(storage_key).unlink(missing_ok=True)
        raise
    return get_artifact(user_id, artifact_id)


def create_server_artifact(user_id: str, upload: FileStorage) -> dict[str, Any]:
    """保存服务端 OCR 图片并排入持久队列，HTTP 请求不等待 Chromium。"""
    data, image = _read_upload(upload)
    artifact_id = str(uuid4())
    storage_key = _store_image(data, extension=image["extension"])
    now = _now()
    artifact = OcrArtifact(
        id=artifact_id,
        user_id=user_id,
        job_id=None,
        source_document_id=None,
        storage_key=storage_key,
        media_type=image["media_type"],
        size_bytes=len(data),
        image_hash=image["image_hash"],
        width=image["width"],
        height=image["height"],
        orientation=image["orientation"],
        status="queued",
        result_json=None,
        corrected_items_json=None,
        failure_code=None,
        version=1,
        created_at_epoch=now,
        updated_at_epoch=now,
    )
    persisted = False
    try:
        with DbSession(_engine()) as db, db.begin():
            db.add(artifact)
        persisted = True
        job = enqueue_job(
            _engine(),
            user_id=user_id,
            kind="ocr.server",
            payload={"artifact_id": artifact_id},
            idempotency_key=f"ocr:{artifact_id}",
            max_attempts=3,
            input_version=1,
        )
        with DbSession(_engine()) as db, db.begin():
            current = db.scalar(
                select(OcrArtifact).where(
                    OcrArtifact.id == artifact_id,
                    OcrArtifact.user_id == user_id,
                )
            )
            if current is None:
                raise RuntimeError("OCR artifact disappeared after queue insertion")
            current.job_id = job.id
            current.updated_at_epoch = _now()
        artifact.job_id = job.id
    except Exception:
        if persisted:
            # 队列暂时不可用时保留图片和记录，避免数据库留下悬空路径。
            with DbSession(_engine()) as db, db.begin():
                current = db.scalar(
                    select(OcrArtifact).where(
                        OcrArtifact.id == artifact_id,
                        OcrArtifact.user_id == user_id,
                    )
                )
                if current is not None and current.job_id is None:
                    current.status = "failed"
                    current.failure_code = "queue_unavailable"
                    current.updated_at_epoch = _now()
        else:
            _image_path(storage_key).unlink(missing_ok=True)
        raise
    return get_artifact(user_id, artifact_id)


def get_artifact(user_id: str, artifact_id: str) -> dict[str, Any]:
    return artifact_public(_artifact_by_owner(user_id, artifact_id))


def artifact_image(user_id: str, artifact_id: str) -> tuple[Path, str]:
    artifact = _artifact_by_owner(user_id, artifact_id)
    return _image_path(artifact.storage_key), artifact.media_type


def create_source_from_artifact(user_id: str, artifact_id: str, payload: OcrSourceInput) -> dict[str, Any]:
    """把用户校正后的 OCR 行交给统一规则/模型候选管线，并保留原始图框。"""
    artifact = _artifact_by_owner(user_id, artifact_id)
    if artifact.version != payload.expected_version:
        raise ApiProblem("version_conflict", "OCR 图片记录已变化，请刷新后重试。", 409)
    if artifact.result_json is None:
        raise ApiProblem("ocr_not_ready", "OCR 任务尚未成功，请稍后重试。", 409)
    original = OcrResultInput.model_validate(artifact.result_json)
    original_by_id = {item.id: item for item in original.items}
    corrections = payload.corrected_items
    if {item.id for item in corrections} != set(original_by_id) or len(corrections) != len(original.items):
        raise ApiProblem("ocr_items_changed", "OCR 行集合与原始结果不匹配，请刷新后重试。", 422)
    if not any(item.text.strip() for item in corrections):
        raise ApiProblem("ocr_text_empty", "至少保留一行可核对的文字。", 422)

    normalized_text = "\n".join(item.text for item in corrections)
    if len(normalized_text.encode("utf-8")) > 100_000:
        raise ApiProblem("content_too_large", "校正后的 OCR 文本超过 100 KB。", 413)
    source_payload = CreateSourceInput.model_validate(
        {
            "source_type": "text",
            "title": payload.title,
            "text": normalized_text,
            "analysis_mode": payload.analysis_mode,
            "provider_profile_id": payload.provider_profile_id,
        }
    )
    reservation_version = payload.expected_version + 1
    with DbSession(_engine()) as db, db.begin():
        current_artifact = db.scalar(
            select(OcrArtifact).where(
                OcrArtifact.id == artifact_id,
                OcrArtifact.user_id == user_id,
            )
        )
        if current_artifact is None:
            raise ApiProblem("not_found", "未找到该 OCR 图片。", 404)
        if current_artifact.version != payload.expected_version or current_artifact.source_document_id:
            raise ApiProblem("version_conflict", "OCR 图片记录已变化，请刷新后重试。", 409)
        current_artifact.status = "linking"
        current_artifact.version = reservation_version
        current_artifact.updated_at_epoch = _now()
    try:
        response = create_source(
            user_id,
            source_payload,
            explicit_segments=[item.text for item in corrections],
            source_type_override="image",
        )
    except Exception:
        with DbSession(_engine()) as db, db.begin():
            current_artifact = db.scalar(
                select(OcrArtifact).where(
                    OcrArtifact.id == artifact_id,
                    OcrArtifact.user_id == user_id,
                    OcrArtifact.version == reservation_version,
                    OcrArtifact.status == "linking",
                )
            )
            if current_artifact is not None:
                current_artifact.status = "succeeded"
                current_artifact.version += 1
                current_artifact.updated_at_epoch = _now()
        raise
    source_id = response["source"]["id"]
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        current_artifact = db.scalar(
            select(OcrArtifact).where(
                OcrArtifact.id == artifact_id,
                OcrArtifact.user_id == user_id,
            )
        )
        source = db.scalar(
            select(SourceDocument).where(
                SourceDocument.id == source_id,
                SourceDocument.user_id == user_id,
            )
        )
        if current_artifact is None or source is None:
            raise ApiProblem("not_found", "OCR 来源或材料已不存在。", 404)
        if (
            current_artifact.version != reservation_version
            or current_artifact.status != "linking"
            or current_artifact.source_document_id
        ):
            raise ApiProblem("version_conflict", "OCR 图片已关联其他材料，请刷新后重试。", 409)
        source.source_type = "image"
        segment_rows = db.scalars(
            select(SourceSegment)
            .where(SourceSegment.user_id == user_id, SourceSegment.source_document_id == source_id)
            .order_by(SourceSegment.ordinal)
        ).all()
        retained = [item for item in corrections if item.text.strip()]
        if len(segment_rows) != len(retained):
            raise ApiProblem("ocr_segments_invalid", "OCR 行与来源片段数量不一致。", 409)
        current_artifact.source_document_id = source_id
        current_artifact.corrected_items_json = [
            {"id": item.id, "original_text": original_by_id[item.id].text, "text": item.text, "segment_id": segment.id}
            for item, segment in zip(retained, segment_rows)
        ]
        current_artifact.version += 1
        current_artifact.updated_at_epoch = now
        response["source"]["source_type"] = "image"
    return response


def handle_server_ocr_job(claim: ClaimedJob) -> dict[str, Any]:
    """按队列租约 owner 读取一个图片工件，结果只写回其所有者记录。"""
    artifact_id = str(claim.payload.get("artifact_id", ""))
    with DbSession(_engine()) as db:
        artifact = db.scalar(
            select(OcrArtifact).where(
                OcrArtifact.id == artifact_id,
                OcrArtifact.user_id == claim.user_id,
                OcrArtifact.job_id == claim.id,
            )
        )
        if artifact is None:
            raise OcrRunnerError("artifact_missing")
        if artifact.result_json is not None:
            return {"artifact_id": artifact.id, "status": "succeeded"}
        storage_key = artifact.storage_key
        expected_hash = artifact.image_hash
        expected_size = artifact.size_bytes
        expected_type = artifact.media_type

    path = _image_path(storage_key)
    image_bytes = path.read_bytes()
    if len(image_bytes) != expected_size or sha256(image_bytes).hexdigest() != expected_hash:
        raise OcrRunnerError("stored_image_integrity_error")
    runner = _get_runner()
    filename = "upload.png" if expected_type == "image/png" else "upload.jpg"
    result_data = runner.recognize(image_bytes, filename=filename, media_type=expected_type)
    try:
        result = OcrResultInput.model_validate(result_data)
    except Exception:
        raise OcrRunnerError("ocr_contract_invalid") from None
    if result.execution_location != "server" or result.image_hash != expected_hash:
        raise OcrRunnerError("ocr_image_mismatch")
    if result.items and any(
        x < 0 or y < 0 or x > result.width or y > result.height
        for item in result.items
        for x, y in item.poly
    ):
        raise OcrRunnerError("ocr_coordinates_invalid")
    with DbSession(_engine()) as db, db.begin():
        current = db.scalar(
            select(OcrArtifact).where(
                OcrArtifact.id == artifact_id,
                OcrArtifact.user_id == claim.user_id,
                OcrArtifact.job_id == claim.id,
            )
        )
        if current is None:
            raise OcrRunnerError("artifact_missing")
        if current.result_json is None:
            current.result_json = result.model_dump(mode="json")
            current.failure_code = None
            current.status = "succeeded"
            current.updated_at_epoch = _now()
    return {"artifact_id": artifact_id, "status": "succeeded"}


def _get_runner() -> OcrRunner:
    """每个 worker 进程复用一个受限 Chromium 和 OCR 模型缓存。"""
    global _runner
    injected_runner = current_app.config.get("OCR_RUNNER")
    if injected_runner is not None:
        return injected_runner
    if _runner is None:
        _runner = OcrRunner(
            timeout_seconds=int(current_app.config["OCR_RUNNER_TIMEOUT_SECONDS"]),
            executable_path=current_app.config.get("OCR_CHROMIUM_EXECUTABLE"),
        )
    return _runner


def close_runner() -> None:
    """供 worker 收到退出信号时关闭受限浏览器进程。"""
    global _runner
    if _runner is not None:
        _runner.close()
        _runner = None
