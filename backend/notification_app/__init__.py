from pathlib import Path

from flask import Flask, abort, g, jsonify, redirect, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from .api import api_bp, calendar_export  # noqa: F401
from .config import load_config
from .database import create_engine_for_app
from .errors import ApiProblem
from .mail import create_mail_transport
from .mail.commands import register_mail_commands
from .services.client_download_service import client_download_info, client_download_path
from .security import register_account_commands
from .services.auth_service import set_web_session_cookie


def create_app(test_config: dict | None = None) -> Flask:
    """创建 Flask 应用；配置、数据库和路由均在工厂内绑定。"""
    app = Flask(__name__)
    app.config.from_mapping(load_config())
    if test_config:
        app.config.update(test_config)

    if app.config["APP_ENV"] == "production":
        key = app.config.get("AUTH_HMAC_KEY")
        if not isinstance(key, str) or len(key.encode("utf-8")) < 32:
            raise RuntimeError("生产环境必须配置至少 32 字节的 AUTH_HMAC_KEY。")
        if not app.config["SESSION_COOKIE_SECURE"]:
            raise RuntimeError("生产环境必须启用 SESSION_COOKIE_SECURE。")
        if not app.config["TRUSTED_ORIGINS"]:
            raise RuntimeError("生产环境必须配置 TRUSTED_ORIGINS。")

    # 本地运行数据与代码分开，避免意外提交业务数据。
    Path(app.config["DATA_DIR"]).expanduser().mkdir(parents=True, exist_ok=True)
    app.json.ensure_ascii = False
    app.extensions["notification_db_engine"] = create_engine_for_app(app.config)
    # 导入一次即可注册 ORM 写入序列，既有页面与 Agent 共用数据库级规划保护。
    from .services import planning_guard  # noqa: F401
    app.extensions["notification_mail_transport"] = create_mail_transport(
        app.config,
        engine=app.extensions["notification_db_engine"],
    )
    app.register_blueprint(api_bp)
    register_account_commands(app)
    register_mail_commands(app)

    @app.before_request
    def assign_request_id() -> None:
        # 不信任客户端传入的追踪值，统一生成不可预测的服务端请求标识。
        import uuid

        g.request_id = uuid.uuid4().hex

    @app.after_request
    def add_response_headers(response):
        # 只在认证成功后续期；退出和撤销当前会话会显式阻止重新种回 Cookie。
        identity = g.get('auth_identity')
        if identity and identity.get('renew_cookie') and not g.get('skip_session_renewal'):
            set_web_session_cookie(response, request.cookies[str(app.config['SESSION_COOKIE_NAME'])])
            response.headers['Cache-Control'] = 'no-store'
        response.headers["X-Request-ID"] = g.get("request_id", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            return (
                jsonify(
                    {
                        "error": {
                            "code": error.name.lower().replace(" ", "_"),
                            "message": error.description,
                            "details": None,
                        },
                        "request_id": g.get("request_id"),
                    }
                ),
                error.code,
            )
        return error

    @app.errorhandler(ApiProblem)
    def handle_api_problem(error: ApiProblem):
        response = jsonify(
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                },
                "request_id": g.get("request_id"),
            }
        )
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        response.headers["Cache-Control"] = "no-store"
        return response, error.status_code

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        # 日志只保留错误类别和请求标识，避免未来把通知原文写入日志。
        app.logger.error(
            "未处理错误类型=%s request_id=%s",
            type(error).__name__,
            g.get("request_id"),
        )
        return (
            jsonify(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "服务暂时不可用。",
                        "details": None,
                    },
                    "request_id": g.get("request_id"),
                }
            ),
            500,
        )

    frontend_dist = Path(app.config["FRONTEND_DIST"])

    @app.get("/downloads/client")
    def download_client_package():
        """公开下载已发布的采集客户端；登入设备页会展示同一地址。"""
        info = client_download_info()
        if not info["available"]:
            abort(404)
        if info["external"]:
            return redirect(str(info["url"]))
        path = client_download_path()
        if path is None:
            abort(404)
        return send_file(
            path,
            as_attachment=True,
            download_name=str(info["filename"]),
            mimetype="application/zip",
        )

    @app.get("/")
    def frontend_index():
        if not (frontend_dist / "index.html").is_file():
            return jsonify({"error": "前端尚未构建，请先执行 npm run build。"}), 503
        return send_from_directory(frontend_dist, "index.html")

    @app.get("/<path:asset_path>")
    def frontend_assets(asset_path: str):
        if asset_path == "api" or asset_path.startswith("api/"):
            abort(404)
        if not (frontend_dist / "index.html").is_file():
            abort(404)
        requested_file = frontend_dist / asset_path
        if requested_file.is_file():
            return send_from_directory(frontend_dist, asset_path)
        # Vue Router 使用 history 模式；未知前端路径交给前端路由显示。
        return send_from_directory(frontend_dist, "index.html")

    return app
