from datetime import date, datetime, time, timedelta, timezone
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..schemas.business import NormalizedBundle
from ..text_sanitize import clean_title

SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})?年?(?P<month>1[0-2]|0?[1-9])月(?P<day>3[01]|[12]\d|0?[1-9])日"
    r"(?:\s*(?:(?P<hour>2[0-3]|[01]?\d):(?P<minute>[0-5]\d)(?::(?P<second>[0-5]\d))?"
    r"|(?P<hour_cn>2[0-3]|[01]?\d)时(?:(?P<minute_cn>[0-5]\d)分?)?))?"
    r"(?P<ending>之前|以前|前)?"
)
_ISO_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})-(?P<month>1[0-2]|0[1-9])-(?P<day>3[01]|[12]\d|0[1-9])"
    r"(?:[T ](?P<hour>2[0-3]|[01]\d):(?P<minute>[0-5]\d)(?::(?P<second>[0-5]\d))?(?P<zone>Z|[+-]\d{2}:?\d{2})?)?"
)
_ACTION = re.compile(r"报名|提交|报送|上报|参加|完成|开展|举办|交(?:材料|作品|表)?|截止")
_ACTOR = re.compile(r"学生|学院|学校|教师|老师|辅导员|参赛者|负责人")
_ACTIVITY = re.compile(r"参加|开展|举办")
_DEADLINE_ACTION = re.compile(r"报名|提交|报送|上报|完成|截止")


def _event_title(content: str) -> str:
    """保留日期前后的动作对象，不把“请于”等时间引导词当成事项标题。"""
    cleaned = content
    for match in sorted(_date_matches(content), key=lambda item: item.start(), reverse=True):
        cleaned = cleaned[:match.start()] + cleaned[match.end():]
    cleaned = re.sub(r"^\s*(?:【[^】]*】\s*)?(?:请)?(?:于|在|定于|将于)?\s*", "", cleaned)
    cleaned = re.sub(r"^\s*(?:[-—–至到]\s*\d{1,2}:\d{2})\s*", "", cleaned)
    cleaned = re.sub(r"(?:，?\s*截止|，?\s*截至|，?\s*之前|，?\s*以前|，?\s*前)\s*[。；]?\s*$", "", cleaned)
    return cleaned.strip(" \t，,。：:；;、-—")[:500] or "待核验事项"


def split_segments(text: str) -> list[tuple[str, int, int]]:
    """按句子和分号保留原始字符偏移，供证据定位使用。"""
    parts: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^。！？；\n]+[。！？；\n]?", text):
        value = match.group(0)
        if value.strip():
            left = len(value) - len(value.lstrip())
            right = len(value.rstrip())
            parts.append((value[left:right], match.start() + left, match.start() + right))
    # 全部为空白时也保留一段空材料以便返回一致的来源结构。
    if not parts and text:
        parts.append((text, 0, len(text)))
    return parts


def _deadline(match: re.Match[str], raw: str, warnings: list[str]) -> dict:
    """只将原文明确给出的年月日和时刻转换为结构化时间。"""
    groups = match.groupdict()
    year_value = groups.get("year")
    month_value = int(groups["month"])
    day_value = int(groups["day"])
    hour_value = groups.get("hour") or groups.get("hour_cn")
    minute_value = groups.get("minute") or groups.get("minute_cn")
    second_value = groups.get("second")
    if not year_value:
        warnings.append(f"期限“{raw}”未给出年份，保留为待核验日期。")
        return {
            "raw": raw,
            "local_date": None,
            "local_time": None,
            "timezone": None,
            "precision": "unknown",
            "utc": None,
            "basis": "原文未明确年份，需人工核验。",
        }

    try:
        local_date = date(int(year_value), month_value, day_value)
        if hour_value is None:
            return {
                "raw": raw,
                "local_date": local_date.isoformat(),
                "local_time": None,
                "timezone": "Asia/Shanghai",
                "precision": "day",
                "utc": None,
                "basis": "原文明确年月日；未补造截止时刻。",
            }
        seconds = int(second_value or 0)
        zone_text = groups.get("zone")
        selected_zone = SHANGHAI
        zone_label = "Asia/Shanghai"
        if zone_text:
            if zone_text == "Z":
                selected_zone = timezone.utc
                zone_label = "UTC"
            else:
                compact_zone = zone_text.replace(":", "")
                sign = 1 if compact_zone[0] == "+" else -1
                offset_hours = int(compact_zone[1:3])
                offset_minutes = int(compact_zone[3:5])
                selected_zone = timezone(sign * timedelta(hours=offset_hours, minutes=offset_minutes))
                zone_label = zone_text
        local_dt = datetime.combine(
            local_date,
            time(int(hour_value), int(minute_value or 0), seconds),
            tzinfo=selected_zone,
        )
        precision = "second" if second_value is not None else "minute"
        time_text = local_dt.strftime("%H:%M:%S" if precision == "second" else "%H:%M:00")
        return {
            "raw": raw,
            "local_date": local_date.isoformat(),
            "local_time": time_text,
            "timezone": zone_label,
            "precision": precision,
            "utc": local_dt.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            "basis": "原文明确年月日和时刻，按所示时区转换。",
        }
    except ValueError:
        warnings.append(f"期限“{raw}”不是有效日期或时刻，保留原文待核验。")
        return {
            "raw": raw,
            "local_date": None,
            "local_time": None,
            "timezone": None,
            "precision": "unknown",
            "utc": None,
            "basis": "原文日期或时刻无效，需人工核验。",
        }


def _date_matches(text: str):
    """先扫常见中文日期，再扫 ISO 日期，防止同一串被重复识别。"""
    occupied: list[tuple[int, int]] = []
    for pattern in (_DATE_PATTERN, _ISO_DATE_PATTERN):
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            occupied.append((match.start(), match.end()))
            yield match


def normalize_text(text: str, source_id: str, segment_pairs: list[tuple[str, str]]) -> dict:
    """把清晰的文本模板转为统一候选包，所有推断都保留人工核验入口。"""
    nonempty = [content for _segment_id, content in segment_pairs if content.strip()]
    # 规则路径常把整条微信消息当标题，先去掉「[链接]」「[玫瑰]」这类导出占位符。
    document_title = clean_title((nonempty[0] if nonempty else "未命名通知").strip(" \t\r\n。！？；、:："))
    notice_id = f"notice-{uuid4().hex}"
    warnings: list[str] = []
    events: list[dict] = []

    for segment_id, content in segment_pairs:
        for match in _date_matches(content):
            has_action = bool(_ACTION.search(content))
            if not has_action:
                continue
            raw = match.group(0)
            title = clean_title(_event_title(content), "待核验事项")
            actor_match = _ACTOR.search(content)
            actor = actor_match.group(0) if actor_match else None
            action_match = _ACTION.search(content)
            stage = None
            if actor and action_match:
                stage = f"{actor}{action_match.group(0)}"
            event_id = f"event-{uuid4().hex}"
            deadline = _deadline(match, raw, warnings)
            time_range = None
            unknowns = ["责任范围、材料和提交渠道需人工核验。"]
            evidence_field = "deadline"
            if _ACTIVITY.search(content) and not _DEADLINE_ACTION.search(content) and not match.groupdict().get("ending"):
                # 活动开始不能冒充截止；只有同日结束时刻明确时才生成完整活动时段。
                evidence_field = "activity_start"
                end_match = re.match(r"\s*[-—–至到]\s*(?P<hour>2[0-3]|[01]?\d):(?P<minute>[0-5]\d)", content[match.end():])
                if deadline["utc"] and end_match and deadline["timezone"] == "Asia/Shanghai":
                    start = datetime.fromisoformat(deadline["utc"].replace("Z", "+00:00")).astimezone(SHANGHAI)
                    end = start.replace(hour=int(end_match["hour"]), minute=int(end_match["minute"]), second=0)
                    if end > start:
                        time_range = {"start": start.isoformat(), "end": end.isoformat(), "timezone": "Asia/Shanghai"}
                        evidence_field = "time_range"
                        raw += end_match.group(0)
                if time_range is None:
                    unknowns.append(f"活动时间原文：{raw}；未形成明确起止时段，请人工核验，不自动补造结束时间。")
                deadline = None
            events.append(
                {
                    "temp_id": event_id,
                    "notice_temp_id": notice_id,
                    "title": title,
                    "actor": actor,
                    "stage": stage,
                    "scope": None,
                    "deadline": deadline,
                    "time_range": time_range,
                    "materials": [],
                    "submission": None,
                    "eligibility": "unknown",
                    "evidence": {evidence_field: [{"segment_id": segment_id, "quote": raw}]},
                    "unknowns": unknowns,
                }
            )

    if not events:
        warnings.append("规则路径未识别出带期限的明确事项；原文已保存，可手工补充。")

    bundle = {
        "schema_version": "1.0",
        "notices": [
            {"temp_id": notice_id, "title": document_title, "source_ids": [source_id]}
        ],
        "events": events,
        "schedule_entries": [],
        "change_candidates": [],
        "warnings": warnings,
    }
    # 临时标识仅用于当前候选包内关联，后端不会据此授权访问对象。
    _ = text
    return NormalizedBundle.model_validate(bundle).model_dump(mode="json")
