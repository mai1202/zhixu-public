from datetime import date

from flask import Response, g, request

from ..errors import ApiProblem
from ..security import authenticated
from ..services import calendar_export_service
from . import api_bp


@api_bp.get("/schedule/export.ics")
@authenticated()
def export_schedule_calendar():
    """导出当前账户的课程、个人安排和待办期限，文件可直接导入系统日历。"""
    try:
        start = date.fromisoformat(request.args["start_date"]) if "start_date" in request.args else None
        end = date.fromisoformat(request.args["end_date"]) if "end_date" in request.args else None
    except ValueError:
        raise ApiProblem("validation_error", "导出日期格式应为 YYYY-MM-DD。", 422) from None
    content, filename = calendar_export_service.export_ics(g.auth_identity["user_id"], start, end)
    response = Response(content, mimetype="text/calendar")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response
