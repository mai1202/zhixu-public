"""协作接口始终重新核验当前成员身份，写入沿用登录态和 CSRF。"""

from flask import g, jsonify, send_file

from . import api_bp
from ..schemas.collaboration import AvailabilityConsentInput, CommentInput, SharedFileInput, SourceTrustInput, PublicationInput
from ..security import authenticated, parse_json_body
from ..services import collaboration_service as service
from ..services import trusted_source_service


@api_bp.get("/journeys/<uuid:journey_id>/collaboration")
@authenticated()
def collaboration_list(journey_id):
    return jsonify(service.list_content(g.auth_identity["user_id"], str(journey_id)))


@api_bp.post("/journeys/<uuid:journey_id>/comments")
@authenticated(csrf=True)
def collaboration_comment(journey_id):
    return jsonify(service.add_comment(g.auth_identity["user_id"], str(journey_id), parse_json_body(CommentInput).content))


@api_bp.delete("/journeys/<uuid:journey_id>/comments/<uuid:item_id>")
@authenticated(csrf=True)
def collaboration_delete_comment(journey_id, item_id):
    return jsonify(service.remove_content(g.auth_identity["user_id"], str(journey_id), "comments", str(item_id)))


@api_bp.post("/journeys/<uuid:journey_id>/files")
@authenticated(csrf=True)
def collaboration_file(journey_id):
    payload = parse_json_body(SharedFileInput)
    return jsonify(service.share_file(g.auth_identity["user_id"], str(journey_id), str(payload.attachment_id), payload.audience))


@api_bp.delete("/journeys/<uuid:journey_id>/files/<uuid:item_id>")
@authenticated(csrf=True)
def collaboration_delete_file(journey_id, item_id):
    return jsonify(service.remove_content(g.auth_identity["user_id"], str(journey_id), "files", str(item_id)))


@api_bp.get("/journeys/<uuid:journey_id>/files/<uuid:item_id>/download")
@authenticated()
def collaboration_download(journey_id, item_id):
    path, item = service.download_file(g.auth_identity["user_id"], str(journey_id), str(item_id))
    response = send_file(path, as_attachment=True, download_name=item.filename, mimetype=item.media_type)
    # 共享文件不允许浏览器缓存或把上传内容当作页面执行。
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@api_bp.get("/journeys/<uuid:journey_id>/availability")
@authenticated()
def collaboration_availability(journey_id):
    return jsonify(service.consent_state(g.auth_identity["user_id"], str(journey_id)))


@api_bp.put("/journeys/<uuid:journey_id>/availability")
@authenticated(csrf=True)
def collaboration_save_availability(journey_id):
    return jsonify(service.save_consent(g.auth_identity["user_id"], str(journey_id), parse_json_body(AvailabilityConsentInput)))


@api_bp.get("/journeys/<uuid:journey_id>/common-availability")
@authenticated()
def collaboration_common_availability(journey_id):
    return jsonify(service.common_availability(g.auth_identity["user_id"], str(journey_id)))


@api_bp.get("/journeys/<uuid:journey_id>/publications")
@authenticated()
def publication_list(journey_id):
    return jsonify(trusted_source_service.state(g.auth_identity["user_id"], str(journey_id)))


@api_bp.put("/journeys/<uuid:journey_id>/source-trust")
@authenticated(csrf=True)
def publication_trust(journey_id):
    return jsonify(trusted_source_service.set_trust(g.auth_identity["user_id"], str(journey_id), parse_json_body(SourceTrustInput).enabled))


@api_bp.post("/journeys/<uuid:journey_id>/publications")
@authenticated(csrf=True)
def publication_create(journey_id):
    payload = parse_json_body(PublicationInput)
    return jsonify(trusted_source_service.publish(g.auth_identity["user_id"], str(journey_id), payload.expected_version, payload.expected_snapshot_hash))


@api_bp.post("/journeys/<uuid:journey_id>/publications/<uuid:publication_id>/adopt")
@authenticated(csrf=True)
def publication_adopt(journey_id, publication_id):
    return jsonify(trusted_source_service.adopt(g.auth_identity["user_id"], str(journey_id), str(publication_id)))
