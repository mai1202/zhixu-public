from flask import Blueprint

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")

# 路由模块从本文件导入共享 Blueprint，避免创建未注册的孤立实例。
from . import agent, agent_ops, auth, business, devices, downloads, health, jobs, mail, models, ocr, timetable  # noqa: E402,F401
from . import assistant_workflow  # noqa: E402,F401
from . import knowledge  # noqa: E402,F401
from . import onboarding  # noqa: E402,F401
from . import journeys  # noqa: E402,F401
from . import collaboration  # noqa: E402,F401
from . import integrations  # noqa: E402,F401
from . import notification_suggestions  # noqa: E402,F401
from . import innovation  # noqa: E402,F401
