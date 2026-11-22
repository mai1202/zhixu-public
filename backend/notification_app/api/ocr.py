import json

from flask import g, jsonify, request, send_file

from ..errors import ApiProblem
from ..schemas.ocr import OcrSourceInput
from ..security import authenticated, parse_json_body
from ..services import ocr_service as service
from . import api_bp


@api_bp.post("/ocr/browser-results")
@authenticated(csrf=True)
def save_browser_ocr_result():
    """保存本地完成的 OCR 结果与私有截图，不重复上传到模型服务。"""
    upload = request.files.get("image")
    raw_result = request.form.get("result", "")
    if upload is None or not raw_result:
        raise ApiProblem("ocr_upload_missing", "请同时提交原图和 OCR 结果。", 422)
    try:
        parsed = json.loads(raw_result)
    except json.JSONDecodeError:
        raise ApiProblem("ocr_result_invalid", "OCR 结果不是有效 JSON。", 422) from None
    if not isinstance(parsed, dict):
        raise ApiProblem("ocr_result_invalid", "OCR 结果必须是 JSON 对象。", 422)
    artifact = service.create_browser_artifact(g.auth_identity["user_id"], upload, parsed)
    response = jsonify({"artifact": artifact})
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.post("/ocr/server-jobs")
@authenticated(csrf=True)
def create_server_ocr_job():
    """只排入固定 OCR 工作，不在 HTTP 请求内启动浏览器。"""
    upload = request.files.get("image")
    if upload is None:
        raise ApiProblem("ocr_upload_missing", "请选择要识别的 PNG 或 JPEG 图片。", 422)
    artifact = service.create_server_artifact(g.auth_identity["user_id"], upload)
    response = jsonify({"artifact": artifact})
    response.status_code = 202
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/ocr/artifacts/<uuid:artifact_id>")
@authenticated()
def get_ocr_artifact(artifact_id):
    response = jsonify(service.get_artifact(g.auth_identity["user_id"], str(artifact_id)))
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/ocr/artifacts/<uuid:artifact_id>/image")
@authenticated()
def get_ocr_image(artifact_id):
    path, media_type = service.artifact_image(g.auth_identity["user_id"], str(artifact_id))
    response = send_file(path, mimetype=media_type, as_attachment=False, conditional=False)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@api_bp.post("/ocr/artifacts/<uuid:artifact_id>/source")
@authenticated(csrf=True)
def create_ocr_source(artifact_id):
    payload = parse_json_body(OcrSourceInput)
    response = jsonify(
        service.create_source_from_artifact(
            g.auth_identity["user_id"], str(artifact_id), payload
        )
    )
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    return response
