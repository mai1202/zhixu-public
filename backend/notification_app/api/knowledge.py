"""知识库、网页快照与运营邮箱入口；所有写入都经服务层权限与版本校验。"""
from uuid import UUID

from flask import g, jsonify, request

from ..errors import ApiProblem
from ..schemas.knowledge import (CitationVerifyInput, KnowledgeInput, KnowledgeStateInput,
                                SchoolPageInput)
from ..security import authenticated, parse_json_body
from ..services import (knowledge_service, mail_ingress_service, search_service,
                        web_knowledge_service)
from . import api_bp


def _int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except ValueError:
        raise ApiProblem("validation_error", f"{name} 必须是整数。", 422) from None
    if value < minimum or value > maximum:
        raise ApiProblem("validation_error", f"{name} 超出允许范围。", 422)
    return value


def _bool_arg(name: str, default: bool = False) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes"}


@api_bp.get("/knowledge/search")
@authenticated()
def search_knowledge():
    """统一检索入口：本地资料、DuckDuckGo、可选 Brave 独立报告渠道状态。"""
    mode = request.args.get("mode", "auto")
    web = request.args.get("web", "auto")
    return jsonify(search_service.search(
        g.auth_identity["user_id"],
        request.args.get("q", ""),
        mode=mode,
        scope=request.args.get("scope", "all"),
        kind=request.args.get("kind", "all"),
        limit=_int_arg("limit", 8, 1, 20),
        web=web,
        use_cache=not _bool_arg("refresh"),
    ))


@api_bp.get("/knowledge/entries/<kind>/<identifier>")
@authenticated()
def read_knowledge_entry(kind, identifier):
    return jsonify(knowledge_service.read_entry(
        g.auth_identity["user_id"], kind, identifier,
        offset=_int_arg("offset", 0, 0, 10_000_000),
    ))


@api_bp.post("/knowledge/documents")
@authenticated(csrf=True)
def create_knowledge_document():
    payload = parse_json_body(KnowledgeInput)
    result = knowledge_service.save_document(g.auth_identity["user_id"], payload)
    response = jsonify(result)
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.patch("/knowledge/documents/<uuid:document_id>")
@authenticated(csrf=True)
def update_knowledge_document(document_id: UUID):
    payload = parse_json_body(KnowledgeStateInput)
    return jsonify(knowledge_service.set_state(g.auth_identity["user_id"], str(document_id), payload))


@api_bp.post("/knowledge/web-pages")
@authenticated(csrf=True)
def import_web_page():
    """学校官网读取只接受公开 HTTPS；保存私人快照仍不自动升级为公共规定。"""
    payload = parse_json_body(SchoolPageInput)
    result = web_knowledge_service.import_page(
        g.auth_identity["user_id"], payload.url, scope=payload.scope, school_only=True,
    )
    response = jsonify(result)
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.post("/knowledge/verify-citation")
@authenticated(csrf=True)
def verify_knowledge_citation():
    """引用校验是只读判断，但用 POST 避免长引文进入 URL 和访问日志。"""
    payload = parse_json_body(CitationVerifyInput)
    return jsonify(knowledge_service.verify_citation(
        g.auth_identity["user_id"], payload.kind, payload.id, payload.quote))


@api_bp.get("/knowledge/status")
@authenticated()
def knowledge_status():
    """显示真实检索方式、搜索渠道配置和资料数量，不宣称未启用的能力。"""
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from ..models import KnowledgeDocument
    with Session(knowledge_service._engine()) as db:
        private_count = db.scalar(select(func.count()).select_from(KnowledgeDocument).where(
            KnowledgeDocument.user_id == g.auth_identity["user_id"],
            KnowledgeDocument.scope == "private",
            KnowledgeDocument.status == "active",
        )) or 0
        public_count = db.scalar(select(func.count()).select_from(KnowledgeDocument).where(
            KnowledgeDocument.scope == "public",
            KnowledgeDocument.status == "active",
        )) or 0
    return jsonify({
        "search": search_service.provider_status(),
        "documents": {"private": private_count, "public": public_count},
        "mail_ingress": mail_ingress_service.status(g.auth_identity["user_id"]),
        "note": "公共库发布需要管理员权限；私人通知、附件和检索记录不会自动公开。",
    })


@api_bp.get("/mail-ingress")
@authenticated()
def mail_ingress_status():
    return jsonify(mail_ingress_service.status(g.auth_identity["user_id"]))


@api_bp.post("/mail-ingress/token")
@authenticated(csrf=True)
def rotate_mail_ingress_token():
    """轮换专属收件码；旧码立即失效，响应只向当前登录账户显示新码。"""
    response = jsonify(mail_ingress_service.issue_token(g.auth_identity["user_id"]))
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.put("/mail-ingress/commands")
@authenticated(csrf=True)
def configure_mail_commands():
    """邮件操作属于独立授权，不随通知转发自动开启。"""
    from ..schemas.collaboration import SourceTrustInput
    response = jsonify(mail_ingress_service.set_commands(g.auth_identity["user_id"], parse_json_body(SourceTrustInput).enabled))
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.post("/mail-ingress/import")
@authenticated(csrf=True)
def import_eml():
    """手动导入 .eml 是可验证、可回退的收信路径，不依赖真实 IMAP 联调。"""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ApiProblem("file_required", "请选择 .eml 文件。", 422)
    if not upload.filename.lower().endswith(".eml"):
        raise ApiProblem("invalid_mail_file", "当前只支持 .eml 文件。", 422)
    raw = upload.stream.read(mail_ingress_service.MAX_MAIL_BYTES + 1)
    if not raw:
        raise ApiProblem("mail_too_large", "邮件不能为空。", 413)
    if len(raw) > mail_ingress_service.MAX_MAIL_BYTES:
        raise ApiProblem("mail_too_large", "邮件不能超过 20MB。", 413)
    import hashlib
    digest = hashlib.sha256(raw).hexdigest()
    result = mail_ingress_service.ingest_message(
        raw, f"manual:{digest}", authenticated_user_id=g.auth_identity["user_id"],
    )
    response = jsonify(result)
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    return response
