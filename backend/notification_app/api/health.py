from flask import current_app, g, jsonify
from sqlalchemy import text

from . import api_bp


@api_bp.get("/health")
def health():
    """检查服务与数据库连通性，不返回路径、凭证或用户数据。"""
    engine = current_app.extensions["notification_db_engine"]
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return (
            jsonify(
                {
                    "error": {
                        "code": "database_unavailable",
                        "message": "数据库暂时不可用。",
                        "details": None,
                    },
                    "request_id": g.get("request_id"),
                }
            ),
            503,
        )

    return jsonify(
        {
            "status": "ok",
            "service": "notification-management-api",
            "contract_version": current_app.config["DEVICE_CONTRACT_VERSION"],
        }
    )
