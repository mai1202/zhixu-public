"""把当前账户的课程、个人安排和待办期限导出为标准 iCalendar。"""

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..models import AcademicTerm
from . import timetable_service

MAX_EXPORT_DAYS = 366
DEFAULT_TIMEZONE = "Asia/Shanghai"


def _engine():
    from flask import current_app

    return current_app.extensions["notification_db_engine"]


def _default_range(user_id: str) -> tuple[date, date]:
    """优先导出最近一个学期，没有课表时给出近期可用的默认范围。"""
    with DbSession(_engine()) as db:
        term = db.scalar(
            select(AcademicTerm)
            .where(AcademicTerm.user_id == user_id)
            .order_by(AcademicTerm.week1_monday.desc())
            .limit(1)
        )
    today = datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date()
    if term is None:
        return today - timedelta(days=7), today + timedelta(days=60)
    start = date.fromisoformat(term.week1_monday)
    end = start + timedelta(days=max(1, term.week_count) * 7 - 1)
    # 很久以前的旧学期不再作为默认导出范围，避免把过期课程塞进当前日历。
    if end < today - timedelta(days=30):
        return today - timedelta(days=7), today + timedelta(days=60)
    return start, end


def _merge_segments(segments: list[dict[str, str]]) -> list[tuple[datetime, datetime]]:
    """相邻或重叠的节次合并，避免同一门课被拆成许多日历条目。"""
    ranges = sorted(
        (datetime.fromisoformat(item["start_at"]), datetime.fromisoformat(item["end_at"]))
        for item in segments
        if item.get("start_at") and item.get("end_at")
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ranges:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _event_for_segment(
    *,
    item: dict[str, Any],
    start: datetime,
    end: datetime,
    index: int,
    timezone_name: str,
) -> dict[str, Any]:
    is_course = item.get("item_type") == "course"
    uid = f"{item['id']}-{item['local_date']}-{index}@zhixu"
    description: list[str] = []
    if is_course:
        teachers = item.get("teachers") or []
        if teachers:
            description.append("教师：" + "、".join(str(value) for value in teachers))
        periods = item.get("periods") or []
        if periods:
            description.append("节次：" + "、".join(str(value) for value in periods))
        if item.get("term_code"):
            description.append("学期：" + str(item["term_code"]))
    else:
        description.append("个人安排")
    return {
        "uid": uid,
        "summary": str(item.get("title") or "未命名安排"),
        "start": start,
        "end": end,
        "all_day": False,
        "timezone": timezone_name,
        "location": item.get("room") or "",
        "description": "\n".join(description),
        "sequence": int(item.get("version") or 1),
        "transparent": False,
    }


def _all_day_event(
    *,
    uid: str,
    title: str,
    local_date: str,
    description: str,
    sequence: int = 1,
    summary_prefix: str = "",
) -> dict[str, Any]:
    return {
        "uid": uid,
        "summary": summary_prefix + title,
        "start": date.fromisoformat(local_date),
        "end": date.fromisoformat(local_date) + timedelta(days=1),
        "all_day": True,
        "timezone": DEFAULT_TIMEZONE,
        "location": "",
        "description": description,
        "sequence": sequence,
        "transparent": True,
    }


def _collect_events(user_id: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    cursor = start_date
    # 现有日程查询单次最多 63 天；按窗口读取可保持与页面完全一致的实例和例外处理。
    while cursor <= end_date:
        window_end = min(cursor + timedelta(days=59), end_date)
        data = timetable_service.list_schedule(user_id, cursor, window_end)
        term_timezones = {
            str(term.get("term_code")): str(term.get("timezone") or DEFAULT_TIMEZONE)
            for term in data.get("terms", [])
        }
        for item in data["items"]:
            local_date = item.get("local_date")
            if not local_date or not start_date.isoformat() <= local_date <= end_date.isoformat():
                continue
            if item.get("item_type") == "course":
                timezone_name = term_timezones.get(str(item.get("term_code")), DEFAULT_TIMEZONE)
                for index, (start, end) in enumerate(_merge_segments(item.get("busy_segments", []))):
                    event = _event_for_segment(
                        item=item,
                        start=start,
                        end=end,
                        index=index,
                        timezone_name=timezone_name,
                    )
                    events.setdefault(event["uid"], event)
                continue
            if item.get("kind") == "busy_block" and item.get("busy_segments"):
                timezone_name = str(item.get("timezone") or DEFAULT_TIMEZONE)
                for index, (start, end) in enumerate(_merge_segments(item["busy_segments"])):
                    event = _event_for_segment(
                        item=item,
                        start=start,
                        end=end,
                        index=index,
                        timezone_name=timezone_name,
                    )
                    events.setdefault(event["uid"], event)
                continue
            prefix = "截止：" if item.get("kind") == "deadline_marker" else ""
            event = _all_day_event(
                uid=f"{item['id']}-{local_date}@zhixu",
                title=str(item.get("title") or "未命名事项"),
                local_date=local_date,
                description="个人安排" if not prefix else "知序待办期限",
                sequence=int(item.get("version") or 1),
                summary_prefix=prefix,
            )
            events.setdefault(event["uid"], event)
        for deadline in data.get("deadlines", []):
            local_date = deadline.get("local_date")
            if not local_date or not start_date.isoformat() <= local_date <= end_date.isoformat():
                continue
            event = _all_day_event(
                uid=f"task-{deadline['id']}-{local_date}@zhixu",
                title=str(deadline.get("title") or "待办"),
                local_date=local_date,
                description="原始期限：" + str(deadline.get("raw") or "未提供"),
                summary_prefix="截止：",
            )
            events.setdefault(event["uid"], event)
        cursor = window_end + timedelta(days=1)
    def sort_key(item: dict[str, Any]) -> tuple[float, str]:
        # 全天事项没有时区，统一按 UTC 零点换算时间戳后再与定时事项排序。
        start = item["start"]
        timestamp = (
            start.timestamp()
            if isinstance(start, datetime)
            else datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        )
        return timestamp, item["uid"]

    return sorted(events.values(), key=sort_key)


def _escape(value: Any) -> str:
    """RFC 5545 文本字段转义，避免标题中的逗号或换行破坏日历结构。"""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold_line(line: str, limit: int = 75) -> str:
    """按 UTF-8 字节数折行，中文标题也能被严格解析器完整读取。"""
    chunks: list[str] = []
    current = ""
    current_bytes = 0
    for character in line:
        character_bytes = len(character.encode("utf-8"))
        if current and current_bytes + character_bytes > limit:
            chunks.append(current)
            current = " " + character
            current_bytes = 1 + character_bytes
        else:
            current += character
            current_bytes += character_bytes
    chunks.append(current)
    return "\r\n".join(chunks)


def _format_datetime(value: datetime, timezone_name: str) -> str:
    # 使用 UTC 绝对时刻，避免部分客户端拒绝没有 VTIMEZONE 定义的 TZID。
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _format_all_day(value: date) -> str:
    return value.strftime("%Y%m%d")


def _render(events: list[dict[str, Any]]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Zhixu//Campus Assistant//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:知序日程",
        "X-WR-CALDESC:由知序生成的课程、个人安排与待办截止日期。",
        "X-WR-TIMEZONE:" + DEFAULT_TIMEZONE,
    ]
    for event in events:
        lines.extend(["BEGIN:VEVENT", "UID:" + _escape(event["uid"]), "DTSTAMP:" + stamp])
        if event["all_day"]:
            lines.append("DTSTART;VALUE=DATE:" + _format_all_day(event["start"]))
            lines.append("DTEND;VALUE=DATE:" + _format_all_day(event["end"]))
        else:
            timezone_name = event["timezone"] or DEFAULT_TIMEZONE
            lines.append(
                "DTSTART:" + _format_datetime(event["start"], timezone_name)
            )
            lines.append(
                "DTEND:" + _format_datetime(event["end"], timezone_name)
            )
        lines.append("SUMMARY:" + _escape(event["summary"]))
        if event.get("description"):
            lines.append("DESCRIPTION:" + _escape(event["description"]))
        if event.get("location"):
            lines.append("LOCATION:" + _escape(event["location"]))
        lines.append("STATUS:CONFIRMED")
        lines.append("TRANSP:" + ("TRANSPARENT" if event["transparent"] else "OPAQUE"))
        lines.append("SEQUENCE:" + str(event["sequence"]))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_line(line) for line in lines) + "\r\n"


def export_ics(
    user_id: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[str, str]:
    """返回日历正文和建议文件名；日期范围超过一年时拒绝，避免误导出巨量数据。"""
    default_start, default_end = _default_range(user_id)
    start = start_date or default_start
    end = end_date or default_end
    if end < start:
        raise ApiProblem("invalid_date_range", "导出结束日期必须晚于开始日期。", 422)
    if (end - start).days > MAX_EXPORT_DAYS:
        raise ApiProblem("invalid_date_range", f"单次导出的日期范围不能超过 {MAX_EXPORT_DAYS} 天。", 422)
    return _render(_collect_events(user_id, start, end)), "zhixu-calendar.ics"
