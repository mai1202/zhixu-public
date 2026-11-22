"""对话附件的私有存储、文本解析和按页读取，不执行附件中的代码。"""

from io import BytesIO
import json
from pathlib import Path
import time
from uuid import uuid4
from zipfile import ZipFile, BadZipFile

from defusedxml import ElementTree
from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.datastructures import FileStorage

from ..errors import ApiProblem
from ..models import AgentAttachment, OcrArtifact

MAX_BYTES = 10 * 1024 * 1024
MAX_TEXT = 500000


def _engine():
    return current_app.extensions["notification_db_engine"]


def _root():
    root = Path(current_app.config["DATA_DIR"]) / "private" / "agent"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _document_text(data: bytes, suffix: str) -> str:
    """常见文本和办公文件本地解析，ZIP 解包前限制解压体积。"""
    if suffix in {".txt", ".md", ".csv", ".json", ".log", ".ics"}:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return data.decode("gb18030")
    if suffix in {".docx", ".xlsx"}:
        with ZipFile(BytesIO(data)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 40 * 1024 * 1024:
                raise ValueError("解压内容过大")
            if suffix == ".docx":
                document = ElementTree.fromstring(archive.read("word/document.xml"))
                return "\n".join("".join(paragraph.itertext()) for paragraph in document.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"))
        from openpyxl import load_workbook
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
        try:
            chunks = []
            for sheet in workbook:
                chunks.append(f"工作表：{sheet.title}")
                for index, row in enumerate(sheet.iter_rows(values_only=True)):
                    if index >= 10000:
                        raise ValueError("工作表行数过多")
                    chunks.append("\t".join(str(value) if value is not None else "" for value in row))
            return "\n".join(chunks)
        finally:
            workbook.close()
    if suffix == ".xls":
        import xlrd
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
        try:
            return "\n".join(f"工作表：{sheet.name}\n" + "\n".join("\t".join(map(str, sheet.row_values(index))) for index in range(sheet.nrows)) for sheet in workbook.sheets())
        finally:
            workbook.release_resources()
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted or len(reader.pages) > 200:
            raise ValueError("PDF 已加密或超过 200 页")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ApiProblem("unsupported_attachment", "支持 PNG、JPEG、TXT、MD、CSV、JSON、PDF、DOCX、XLS 和 XLSX。", 422)


def upload_material(user_id: str, upload: FileStorage) -> dict:
    """按账户保存附件；图片走既有本地 OCR，文本不自动发送到模型。"""
    filename = (upload.filename or "附件").replace("\\", "/").split("/")[-1][:240]
    suffix = Path(filename).suffix.lower()
    data = upload.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise ApiProblem("attachment_size", "附件不能为空且不能超过 10 MB。", 413)
    ocr_id = None
    text = None
    if suffix in {".png", ".jpg", ".jpeg"}:
        from .ocr_service import create_server_artifact
        artifact = create_server_artifact(user_id, FileStorage(stream=BytesIO(data), filename=filename))
        ocr_id = artifact["id"]
    else:
        try:
            text = _document_text(data, suffix)
        except ApiProblem:
            raise
        except Exception:
            raise ApiProblem("attachment_parse_failed", "无法解析此文件，请检查格式、加密状态或转换为文本后再试。", 422) from None
        if len(text) > MAX_TEXT:
            raise ApiProblem("attachment_text_limit", "附件文本超过 50 万字符，请拆分文件。", 422)
        if not text.strip():
            raise ApiProblem("attachment_no_text", "未提取到文本；扫描版 PDF 请先转成图片上传。", 422)
    identifier = str(uuid4())
    storage_key = uuid4().hex + suffix
    path = _root() / storage_key
    path.write_bytes(data)
    try:
        with Session(_engine()) as db, db.begin():
            item = AgentAttachment(id=identifier, user_id=user_id, filename=filename, storage_key=storage_key,
                                   media_type=upload.mimetype or "application/octet-stream", size_bytes=len(data),
                                   extracted_text=text, ocr_artifact_id=ocr_id, created_at_epoch=int(time.time()))
            db.add(item)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    try:
        from . import search_service
        search_service.invalidate_cache()
    except Exception:
        current_app.logger.warning('搜索缓存失效失败 attachment_id=%s',identifier)
    try:
        from .retrieval_service import schedule_vector_index
        if text and text.strip():
            schedule_vector_index(user_id, "attachment", identifier)
    except Exception:
        current_app.logger.warning('附件向量索引调度失败 attachment_id=%s', identifier)
    return material_info(user_id, identifier)


def owned_material(db: Session, user_id: str, identifier: str) -> AgentAttachment:
    """所有入口均按账户查找，不接受模型指定文件路径。"""
    item = db.scalar(select(AgentAttachment).where(AgentAttachment.id == identifier, AgentAttachment.user_id == user_id))
    if item is None:
        raise ApiProblem("not_found", "未找到该附件。", 404)
    return item


def material_file(user_id: str, identifier: str):
    """下载和邮件发送复用同一 owner 检查，只读服务器生成的私有文件键。"""
    with Session(_engine()) as db:
        item = owned_material(db, user_id, identifier)
    root = _root().resolve()
    path = (root / item.storage_key).resolve()
    if path.parent != root or not path.is_file():
        raise ApiProblem('attachment_missing', '附件文件不存在。', 404)
    return path, item


def create_text_material(user_id: str, filename: str, content: str):
    """助手可生成可下载的文本、表格数据与日历文件，沿用上传限额和解析。"""
    if Path(filename).suffix.lower() not in {'.txt', '.md', '.csv', '.json', '.ics'}:
        raise ApiProblem('unsupported_generated_file', '可生成 TXT、Markdown、CSV、JSON 或 ICS 文件。', 422)
    result = upload_material(user_id, FileStorage(stream=BytesIO(content.encode('utf-8')), filename=filename))
    result['download_href'] = f"/api/v1/agent/attachments/{result['id']}/download"
    return result


def list_materials(user_id: str):
    """主助手可查看本人附件目录，不把文件路径或其他账户资料暴露给模型。"""
    with Session(_engine()) as db:
        items = db.scalars(select(AgentAttachment).where(AgentAttachment.user_id == user_id)
            .order_by(AgentAttachment.created_at_epoch.desc()).limit(100)).all()
        return {'items': [{'id': item.id, 'filename': item.filename, 'size_bytes': item.size_bytes} for item in items]}


def material_info(user_id: str, identifier: str, *, offset: int = 0) -> dict:
    """按页返回最多 12000 字，保留页数和 OCR 状态，禁止默默截掉后半份文件。"""
    with Session(_engine()) as db:
        item = owned_material(db, user_id, identifier)
        text = item.extracted_text or ""
        status = "ready"
        if item.ocr_artifact_id:
            artifact = db.get(OcrArtifact, item.ocr_artifact_id)
            status = artifact.status
            if artifact.result_json:
                text = "\n".join(part["text"] for part in artifact.result_json.get("items", []))
                status = "ready"
        ocr_status = status if item.ocr_artifact_id else None
        # 原图可立即供支持视觉的模型使用；OCR 状态独立显示，文本模型需等识别就绪。
        if item.ocr_artifact_id:
            status = "ready"
        return {"id": item.id, "filename": item.filename, "size_bytes": item.size_bytes, "status": status,
                "image_url": f"/api/v1/ocr/artifacts/{item.ocr_artifact_id}/image" if item.ocr_artifact_id else None,
                "ocr_status": ocr_status, "offset": offset, "text": text[offset:offset + 12000], "total_characters": len(text),
                "next_offset": offset + 12000 if offset + 12000 < len(text) else None}
