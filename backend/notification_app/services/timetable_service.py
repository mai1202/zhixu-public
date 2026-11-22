from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import re
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..errors import ApiProblem
from ..models import (
    AcademicTerm,
    ChangeSet,
    CourseException,
    CourseOccurrence,
    CourseSeries,
    Event,
    Notice,
    ScheduleEntry,
    SourceDocument,
    Task,
    TimetableImport,
)
from ..schemas.agent_ops import LinkInput
from ..schemas.timetable import (
    CourseExceptionInput,
    CourseMakeupInput,
    CourseOverrideInput,
    CourseSeriesCreateInput,
    CourseSeriesUpdateInput,
    PeriodGroupInput,
    ScheduleEntryInput,
    UpdateScheduleEntryInput,
)
from ..timetable.parser import TimetableParseError, parse_course_text, parse_timetable_file, parse_week_set

DEFAULT_TERM_ANCHORS = {
    "2026-2027-1": ("2026-09-07", "旧课表项目提供的本学期参考值；需由用户核对"),
}


def _now() -> int:
    return int(datetime.now().astimezone().timestamp())


def _engine():
    from flask import current_app

    return current_app.extensions["notification_db_engine"]


def _owned(db: DbSession, model, object_id: str, user_id: str):
    entity = db.scalar(select(model).where(model.id == object_id, model.user_id == user_id))
    if entity is None:
        raise ApiProblem("not_found", "未找到该记录。", 404)
    return entity


def _identity_digest(course: dict[str, Any]) -> str:
    identity = f"{course['title'].strip().casefold()}|{course['weekday']}|{','.join(map(str, course['periods']))}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _course_values(course: dict[str, Any], week_count: int) -> dict[str, Any]:
    values = dict(course)
    values["identity_key"] = _identity_digest(values)
    weeks, warnings = parse_week_set(values.get("week_expression", ""), week_count)
    values["week_set"] = weeks
    values["warnings"] = list(dict.fromkeys(values.get("warnings", []) + warnings))
    return values


def _course_matches(course: CourseSeries, candidate: dict[str, Any]) -> bool:
    return (
        course.title == candidate["title"]
        and course.display_name == candidate["display_name"]
        and course.teachers_json == candidate["teachers"]
        and course.room == candidate["room"]
        and course.week_set_json == candidate["week_set"]
        and course.periods_json == candidate["periods"]
        and course.weekday == candidate["weekday"]
        and course.start_time == candidate.get("start_time")
        and course.end_time == candidate.get("end_time")
    )


def _course_import_diff(
    db: DbSession,
    *,
    user_id: str,
    term_code: str,
    courses: list[dict[str, Any]],
) -> dict[str, Any]:
    term = db.scalar(
        select(AcademicTerm).where(AcademicTerm.user_id == user_id, AcademicTerm.term_code == term_code)
    )
    existing = (
        db.scalars(
            select(CourseSeries).where(
                CourseSeries.user_id == user_id,
                CourseSeries.term_id == term.id,
                CourseSeries.status == "active",
            )
        ).all()
        if term
        else []
    )
    by_identity = {course.identity_key: course for course in existing}
    incoming = {candidate["identity_key"]: candidate for candidate in courses}
    added: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for key, candidate in incoming.items():
        old = by_identity.get(key)
        summary = {"title": candidate["title"], "weekday": candidate["weekday"], "periods": candidate["periods"]}
        if old is None:
            added.append(summary)
        elif _course_matches(old, candidate):
            unchanged.append(summary)
        else:
            changed.append(
                {
                    **summary,
                    "previous_teachers": old.teachers_json,
                    "next_teachers": candidate["teachers"],
                    "previous_room": old.room,
                    "next_room": candidate["room"],
                    "previous_week_set": old.week_set_json,
                    "next_week_set": candidate["week_set"],
                }
            )
    removed = [
        {"title": old.title, "weekday": old.weekday, "periods": old.periods_json}
        for key, old in by_identity.items()
        if key not in incoming
    ]
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": unchanged,
        "summary": {
            "added": len(added),
            "changed": len(changed),
            "removed": len(removed),
            "unchanged": len(unchanged),
        },
    }


def _normalized_import_draft(item: TimetableImport) -> dict[str, Any]:
    """旧版尚未确认的预览按原文重新分字段，刷新后也能直接得到修复结果。"""
    draft = deepcopy(item.draft_json)
    if item.status == "proposed" and draft.get("field_parser_version") != 2:
        for course in draft.get("courses", []):
            course.update(parse_course_text(course.get("raw_text", ""), draft.get("week_count", 16)))
        draft["field_parser_version"] = 2
    return draft


def import_public(item: TimetableImport) -> dict[str, Any]:
    return {
        "id": item.id,
        "term_code": item.term_code,
        "file_format": item.file_format,
        "status": item.status,
        "apply_mode": item.apply_mode,
        "draft": _normalized_import_draft(item),
        "diff": item.diff_json,
        "version": item.version,
        "created_at_epoch": item.created_at_epoch,
        "confirmed_at_epoch": item.confirmed_at_epoch,
    }


def preview_import(
    user_id: str,
    *,
    file_bytes: bytes,
    filename: str,
    week1_monday: date | None,
    week_count: int,
    timezone_name: str,
    term_code_override: str | None = None,
) -> dict[str, Any]:
    """解析文件并保存候选差异；不会创建课程实例或覆盖已有学期。"""
    try:
        draft = parse_timetable_file(
            file_bytes,
            filename,
            week_count=week_count,
            timezone_name=timezone_name,
        )
    except TimetableParseError as error:
        raise ApiProblem("timetable_invalid", str(error), 422) from None
    term_code = (draft.get("term_code") or term_code_override or "").strip()
    if term_code and not re.fullmatch(r"[0-9A-Za-z-]{3,32}", term_code):
        raise ApiProblem("invalid_term_code", "学期代码格式无效。", 422)
    if not term_code:
        raise ApiProblem("term_code_required", "文件没有可识别学期代码，请补充学期代码。", 422)
    draft["term_code"] = term_code
    draft["week_count"] = week_count
    draft["timezone"] = timezone_name
    courses = [_course_values(course, week_count) for course in draft["courses"]]
    draft["courses"] = courses
    anchor = week1_monday.isoformat() if week1_monday else None
    anchor_source = "用户提供的日期，确认前仍需核对。"
    if anchor is None and term_code in DEFAULT_TERM_ANCHORS:
        anchor, anchor_source = DEFAULT_TERM_ANCHORS[term_code]
        draft["warnings"].append("首周周一使用旧项目参考值，仅供预览，确认前必须人工核对。")
    draft["week1_monday"] = anchor
    draft["anchor_source"] = anchor_source
    if anchor and date.fromisoformat(anchor).weekday() != 0:
        raise ApiProblem("invalid_term_anchor", "学期第 1 周日期必须是星期一。", 422)

    file_hash = hashlib.sha256(file_bytes).hexdigest()
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        existing = db.scalar(
            select(TimetableImport).where(
                TimetableImport.user_id == user_id,
                TimetableImport.file_hash == file_hash,
            )
        )
        if existing is not None:
            return {"import": import_public(existing), "already_seen": True}
        diff = _course_import_diff(db, user_id=user_id, term_code=term_code, courses=courses)
        detected_format = "xls" if filename.lower().endswith(".xls") else "xlsx"
        item = TimetableImport(
            id=str(uuid4()),
            user_id=user_id,
            term_code=term_code,
            file_hash=file_hash,
            filename="课表.xls" if detected_format == "xls" else "课表.xlsx",
            file_format=detected_format,
            status="proposed",
            apply_mode=None,
            draft_json=draft,
            diff_json=diff,
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
        )
        db.add(item)
    return {"import": import_public(item), "already_seen": False}


def _group_segments(
    *,
    local_date: date,
    periods: list[int],
    period_profile: list[dict[str, Any]],
    timezone_name: str,
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[dict[str, str]]:
    zone = ZoneInfo(timezone_name)
    # 课程明确给出的真实时间优先，不能由包含额外小节的整组作息覆盖。
    if start_time and end_time:
        start = datetime.combine(local_date, time.fromisoformat(start_time), tzinfo=zone)
        end = datetime.combine(local_date, time.fromisoformat(end_time), tzinfo=zone)
        if end <= start:
            raise ApiProblem("invalid_course_time", "课程结束必须晚于开始。", 422)
        return [{"start_at": start.isoformat(), "end_at": end.isoformat()}]
    segments: list[dict[str, str]] = []
    covered = set()
    for group in period_profile:
        if not set(periods).intersection(group["periods"]):
            continue
        if not set(group["periods"]).issubset(periods):
            raise ApiProblem("incomplete_period_profile", "所选小节只占作息组的一部分，请填写课程真实开始和结束时间。", 422)
        start_time = group.get("start_time")
        end_time = group.get("end_time")
        if not start_time or not end_time:
            continue
        start = datetime.combine(local_date, time.fromisoformat(start_time), tzinfo=zone)
        end = datetime.combine(local_date, time.fromisoformat(end_time), tzinfo=zone)
        if end <= start:
            raise ApiProblem("invalid_period_profile", "作息表时段结束必须晚于开始。", 422)
        segments.append({"start_at": start.isoformat(), "end_at": end.isoformat()})
        covered.update(group["periods"])
    if set(periods) - covered:
        raise ApiProblem("incomplete_period_profile", "部分小节没有作息，请补全作息或填写课程真实开始和结束时间。", 422)
    return segments


def _materialize_occurrences(
    db: DbSession,
    *,
    user_id: str,
    term: AcademicTerm,
    course: CourseSeries,
    source_import_id: str,
    now: int,
) -> None:
    """按显式周集合生成实例，跨课间只保存各自忙碌片段。"""
    anchor = date.fromisoformat(term.week1_monday)
    zone = ZoneInfo(term.timezone)
    profile = term.period_profile_json
    generated_keys: set[str] = set()
    for week_number in course.week_set_json:
        if week_number < 1 or week_number > term.week_count:
            continue
        local_date = anchor + timedelta(days=(week_number - 1) * 7 + course.weekday - 1)
        segments = _group_segments(
            local_date=local_date,
            periods=course.periods_json,
            period_profile=profile,
            timezone_name=term.timezone,
            start_time=course.start_time,
            end_time=course.end_time,
        )
        if not segments:
            raise ApiProblem(
                "incomplete_period_profile",
                "课程小节缺少明确作息时刻，补充节次表后才能确认导入。",
                422,
                {"course_title": course.title, "periods": course.periods_json},
            )
        start_values = [datetime.fromisoformat(item["start_at"]).astimezone(zone).strftime("%H:%M") for item in segments]
        end_values = [datetime.fromisoformat(item["end_at"]).astimezone(zone).strftime("%H:%M") for item in segments]
        occurrence_key = f"{course.id}|{local_date.isoformat()}"
        generated_keys.add(occurrence_key)
        occurrence = db.scalar(
            select(CourseOccurrence).where(
                CourseOccurrence.user_id == user_id,
                CourseOccurrence.occurrence_key == occurrence_key,
            )
        )
        if occurrence is None:
            db.add(
                CourseOccurrence(
                    id=str(uuid4()),
                    user_id=user_id,
                    term_id=term.id,
                    course_series_id=course.id,
                    source_import_id=source_import_id,
                    occurrence_key=occurrence_key,
                    local_date=local_date.isoformat(),
                    week_number=week_number,
                    display_start=min(start_values),
                    display_end=max(end_values),
                    busy_segments_json=segments,
                    status="scheduled",
                    version=1,
                )
            )
            continue
        has_exception = db.scalar(
            select(CourseException.id).where(
                CourseException.user_id == user_id,
                CourseException.occurrence_id == occurrence.id,
                CourseException.status == "active",
            )
        )
        if has_exception:
            continue
        occurrence.source_import_id = source_import_id
        occurrence.local_date = local_date.isoformat()
        occurrence.week_number = week_number
        occurrence.display_start = min(start_values)
        occurrence.display_end = max(end_values)
        occurrence.busy_segments_json = segments
        occurrence.status = "scheduled"
        occurrence.version += 1

    old_occurrences = db.scalars(
        select(CourseOccurrence).where(
            CourseOccurrence.user_id == user_id,
            CourseOccurrence.course_series_id == course.id,
            # 补课使用独立 occurrence_key，修改常规课程系列时不能顺带停掉补课。
            CourseOccurrence.occurrence_key.like(f"{course.id}|%"),
            CourseOccurrence.status.in_(("scheduled", "rescheduled")),
        )
    ).all()
    for occurrence in old_occurrences:
        if occurrence.occurrence_key not in generated_keys:
            occurrence.status = "superseded"
            occurrence.version += 1


def confirm_import(
    user_id: str,
    import_id: str,
    *,
    expected_version: int,
    week1_monday: date,
    week_count: int,
    timezone_name: str,
    anchor_confirmed: bool,
    apply_mode: str,
    period_profile: list[PeriodGroupInput],
    course_overrides: list[CourseOverrideInput] | None = None,
) -> dict[str, Any]:
    """以版本保护原子创建学期、系列和课程实例。"""
    if not anchor_confirmed:
        raise ApiProblem("anchor_confirmation_required", "请先核对首周周一和节次作息。", 422)
    if week1_monday.weekday() != 0 or not 1 <= week_count <= 30:
        raise ApiProblem("invalid_term_configuration", "学期日期或周数设置无效。", 422)
    if apply_mode not in {"merge", "replace_term"}:
        raise ApiProblem("invalid_apply_mode", "课表更新范围无效。", 422)
    profile = [item.model_dump(mode="json") for item in period_profile]
    profile_periods = [period for group in profile for period in group["periods"]]
    if len(profile_periods) != len(set(profile_periods)):
        raise ApiProblem("duplicate_period", "作息表中的小节重复。", 422)

    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        item = _owned(db, TimetableImport, import_id, user_id)
        if item.status != "proposed" or item.version != expected_version:
            raise ApiProblem("version_conflict", "课表预览已变化或已处理，请重新预览。", 409)
        term = db.scalar(
            select(AcademicTerm).where(
                AcademicTerm.user_id == user_id,
                AcademicTerm.term_code == item.term_code,
            )
        )
        if term is None:
            term = AcademicTerm(
                id=str(uuid4()),
                user_id=user_id,
                term_code=item.term_code,
                week1_monday=week1_monday.isoformat(),
                week_count=week_count,
                timezone=timezone_name,
                period_profile_json=profile,
                anchor_source=item.draft_json.get("anchor_source", "用户核对并确认"),
                version=1,
                created_at_epoch=now,
                updated_at_epoch=now,
            )
            db.add(term)
            db.flush()
        else:
            term.week1_monday = week1_monday.isoformat()
            term.week_count = week_count
            term.timezone = timezone_name
            term.period_profile_json = profile
            term.anchor_source = "用户核对并确认"
            term.version += 1
            term.updated_at_epoch = now

        override_map = {override.source_cell: override for override in course_overrides or []}
        draft_courses = _normalized_import_draft(item).get("courses", [])
        known_cells = {course.get("source_cell") for course in draft_courses}
        if set(override_map) - known_cells:
            raise ApiProblem("invalid_course_override", "课程修正对应的单元格不在本次导入中。", 422)
        for course in draft_courses:
            override = override_map.get(course.get("source_cell"))
            if override is None:
                continue
            for field, value in override.model_dump(exclude_unset=True, exclude={"source_cell"}).items():
                if field == "week_expression" and value is not None:
                    course[field] = value
                elif value is not None or field in {"start_time", "end_time", "room"}:
                    course[field] = value
            if not str(course.get("title", "")).strip():
                raise ApiProblem("invalid_course_override", "修正后的课程名称不能为空。", 422)
            if not str(course.get("display_name", "")).strip():
                course["display_name"] = course["title"]
        candidates = [_course_values(course, week_count) for course in draft_courses]
        if not candidates:
            raise ApiProblem("empty_timetable", "课表没有可确认的课程起始单元格。", 422)
        incoming_keys = {course["identity_key"] for course in candidates}
        existing_series = db.scalars(
            select(CourseSeries).where(
                CourseSeries.user_id == user_id,
                CourseSeries.term_id == term.id,
                CourseSeries.status == "active",
            )
        ).all()
        series_by_key = {course.identity_key: course for course in existing_series}
        for candidate in candidates:
            if not candidate["week_set"]:
                raise ApiProblem(
                    "week_set_required",
                    "仍有课程周集合无法识别，请在预览中填写具体周数范围后确认。",
                    422,
                    {"course_title": candidate["title"]},
                )
            series = series_by_key.get(candidate["identity_key"])
            if series is None:
                series = db.scalar(
                    select(CourseSeries).where(
                        CourseSeries.user_id == user_id,
                        CourseSeries.term_id == term.id,
                        CourseSeries.identity_key == candidate["identity_key"],
                    )
                )
            if series is None:
                series = CourseSeries(
                    id=str(uuid4()),
                    user_id=user_id,
                    term_id=term.id,
                    source_import_id=item.id,
                    identity_key=candidate["identity_key"],
                    title=candidate["title"],
                    display_name=candidate["display_name"],
                    teachers_json=candidate["teachers"],
                    room=candidate["room"],
                    week_set_json=candidate["week_set"],
                    weekday=candidate["weekday"],
                    periods_json=candidate["periods"],
                    start_time=candidate.get("start_time"),
                    end_time=candidate.get("end_time"),
                    raw_text=candidate["raw_text"],
                    status="active",
                    version=1,
                    created_at_epoch=now,
                    updated_at_epoch=now,
                )
                db.add(series)
                db.flush()
            else:
                series.source_import_id = item.id
                series.title = candidate["title"]
                series.display_name = candidate["display_name"]
                series.teachers_json = candidate["teachers"]
                series.room = candidate["room"]
                series.week_set_json = candidate["week_set"]
                series.weekday = candidate["weekday"]
                series.periods_json = candidate["periods"]
                series.start_time = candidate.get("start_time")
                series.end_time = candidate.get("end_time")
                series.raw_text = candidate["raw_text"]
                series.status = "active"
                series.version += 1
                series.updated_at_epoch = now
            _materialize_occurrences(
                db,
                user_id=user_id,
                term=term,
                course=series,
                source_import_id=item.id,
                now=now,
            )

        if apply_mode == "replace_term":
            for series in existing_series:
                if series.identity_key not in incoming_keys:
                    series.status = "archived"
                    series.version += 1
                    series.updated_at_epoch = now
                    db.execute(
                        CourseOccurrence.__table__.update()
                        .where(
                            CourseOccurrence.user_id == user_id,
                            CourseOccurrence.course_series_id == series.id,
                            CourseOccurrence.status.in_(("scheduled", "rescheduled")),
                        )
                        .values(status="superseded", version=CourseOccurrence.version + 1)
                    )

        item.term_id = term.id
        item.status = "confirmed"
        item.apply_mode = apply_mode
        item.version += 1
        item.confirmed_at_epoch = now
        item.updated_at_epoch = now
    return import_public(item)


def course_series_public(
    series: CourseSeries,
    *,
    term: AcademicTerm | None = None,
    occurrence_count: int | None = None,
) -> dict[str, Any]:
    """课程系列详情统一给网页和 Agent 使用，避免两边各写一套字段解释。"""
    return {
        "id": series.id,
        "term_id": series.term_id,
        "term_code": term.term_code if term else None,
        "title": series.title,
        "display_name": series.display_name,
        "teachers": series.teachers_json,
        "room": series.room,
        "weekday": series.weekday,
        "periods": series.periods_json,
        "start_time": series.start_time,
        "end_time": series.end_time,
        "weeks": series.week_set_json,
        "status": series.status,
        "version": series.version,
        "source_import_id": series.source_import_id,
        "occurrence_count": occurrence_count,
        "updated_at_epoch": series.updated_at_epoch,
    }


def _manual_term(db: DbSession, user_id: str, term_id: str | None) -> AcademicTerm:
    """优先使用指定学期；未指定时选择包含今天的最近教学周，否则回退到最新学期。"""
    if term_id:
        return _owned(db, AcademicTerm, term_id, user_id)
    terms = db.scalars(
        select(AcademicTerm)
        .where(AcademicTerm.user_id == user_id)
        .order_by(AcademicTerm.week1_monday.desc())
    ).all()
    if not terms:
        raise ApiProblem("term_required", "请先导入或确认一个学期，再新增课程。", 422)
    for term in terms:
        # 每个学期按自身时区判断“包含今天”，避免服务器 UTC 跨日选错学期。
        today = datetime.now(ZoneInfo(term.timezone)).date()
        start = date.fromisoformat(term.week1_monday)
        if start <= today <= start + timedelta(days=term.week_count * 7 - 1):
            return term
    return terms[0]


def _manual_course_values(
    db: DbSession,
    *,
    user_id: str,
    term: AcademicTerm,
    title: str,
    display_name: str | None,
    teachers: list[str],
    room: str | None,
    weekday: int,
    periods: list[int],
    week_numbers: list[int],
    start_time: str | None = None,
    end_time: str | None = None,
    series_id: str | None = None,
) -> dict[str, Any]:
    """校验手动课程能否落入当前学期作息，并阻止同身份课程静默重复。"""
    clean_weeks = sorted(set(int(item) for item in week_numbers))
    if not clean_weeks or clean_weeks[0] < 1 or clean_weeks[-1] > term.week_count:
        raise ApiProblem(
            "outside_term",
            f"教学周必须位于 1–{term.week_count} 周范围内。",
            422,
        )
    covered_periods = {
        int(period)
        for group in term.period_profile_json
        if group.get("start_time") and group.get("end_time")
        for period in group.get("periods", [])
    }
    missing_periods = sorted(set(periods) - covered_periods)
    if missing_periods and not (start_time and end_time):
        raise ApiProblem(
            "incomplete_period_profile",
            "课程节次缺少明确作息时刻，请先在课表导入页补全节次表。",
            422,
            {"periods": missing_periods},
        )
    candidate = {
        "title": title.strip(),
        "display_name": (display_name or title).strip(),
        "teachers": [item.strip() for item in teachers if item.strip()],
        "room": room.strip() if room else None,
        "week_set": clean_weeks,
        "weekday": weekday,
        "periods": sorted(set(periods)),
        "start_time": start_time,
        "end_time": end_time,
    }
    candidate["identity_key"] = _identity_digest(candidate)
    collision_query = select(CourseSeries).where(
        CourseSeries.user_id == user_id,
        CourseSeries.term_id == term.id,
        CourseSeries.identity_key == candidate["identity_key"],
    )
    if series_id:
        collision_query = collision_query.where(CourseSeries.id != series_id)
    collision = db.scalar(collision_query)
    if collision is not None:
        if collision.status == "archived":
            raise ApiProblem(
                "archived_course_exists",
                "同一学期已有同名、同星期和同节次的归档课程；请直接恢复该课程。",
                409,
                {"series_id": collision.id},
            )
        raise ApiProblem(
            "course_exists",
            "同一学期已有同名、同星期和同节次的课程；请直接修改现有课程系列。",
            409,
            {"series_id": collision.id},
        )
    return candidate


def _course_series_change_set(
    db: DbSession,
    *,
    user_id: str,
    actor_type: str,
    actor_id: str | None,
    summary: str,
    action: str,
    series: CourseSeries,
    now: int,
    operation_type: str,
    before: dict[str, Any] | None = None,
) -> ChangeSet:
    """课程系列写操作统一生成可撤销变更集和操作轨迹。"""
    from . import business_service, operation_service

    operation = {
        "type": operation_type,
        "object_type": "course_series",
        "object_id": series.id,
        "applied_version": series.version,
    }
    if before is not None:
        operation["before"] = deepcopy(before)
    change_set = ChangeSet(
        id=str(uuid4()),
        user_id=user_id,
        source_document_id=None,
        kind="operations",
        status="confirmed",
        summary=summary[:1000],
        bundle_json=None,
        operations_json=[operation],
        version=2,
        change_seq=business_service._next_change_seq(db, user_id),
        created_at_epoch=now,
        updated_at_epoch=now,
        confirmed_at_epoch=now,
    )
    db.add(change_set)
    db.flush()
    operation_service.record_operation(
        db,
        user_id=user_id,
        actor_type=actor_type,
        actor_id=actor_id or user_id,
        action=action,
        object_type="course_series",
        object_id=series.id,
        change_set_id=change_set.id,
        summary=summary,
        before=before or {},
        after=course_series_public(series),
        undoable=True,
        created_at_epoch=now,
    )
    return change_set


def _planned_series_conflicts(
    db: DbSession,
    *,
    user_id: str,
    term: AcademicTerm,
    weekday: int,
    periods: list[int],
    weeks: list[int],
    start_time: str | None = None,
    end_time: str | None = None,
    series_id: str | None = None,
) -> list[dict[str, Any]]:
    """在写入前检查整门课程的每个实例，返回可定位的真实冲突而不是仅靠事后扫描。"""
    anchor = date.fromisoformat(term.week1_monday)
    own_occurrence_ids = {
        item
        for item in db.scalars(
            select(CourseOccurrence.id).where(
                CourseOccurrence.user_id == user_id,
                CourseOccurrence.course_series_id == series_id,
            )
        ).all()
    } if series_id else set()
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for week in sorted(set(weeks)):
        local_date = anchor + timedelta(days=(week - 1) * 7 + weekday - 1)
        for segment in _group_segments(
            local_date=local_date,
            periods=periods,
            period_profile=term.period_profile_json,
            timezone_name=term.timezone,
            start_time=start_time,
            end_time=end_time,
        ):
            start_at = datetime.fromisoformat(segment["start_at"])
            end_at = datetime.fromisoformat(segment["end_at"])
            for conflict in _conflicts_for_interval(
                db,
                user_id=user_id,
                start_at=start_at,
                end_at=end_at,
            ):
                if conflict["object_id"] in own_occurrence_ids:
                    continue
                key = (
                    str(conflict["object_type"]),
                    str(conflict["object_id"]),
                    str(conflict["start_at"]),
                    str(conflict["end_at"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                found.append({**conflict, "week_number": week, "local_date": local_date.isoformat()})
                if len(found) >= 20:
                    return found
    return found


def create_course_series(
    user_id: str,
    payload: CourseSeriesCreateInput,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> dict[str, Any]:
    """手动新增常规课程系列；课程实例仍由统一物化函数生成。"""
    from . import business_service

    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        term = _manual_term(db, user_id, payload.term_id)
        candidate = _manual_course_values(
            db,
            user_id=user_id,
            term=term,
            title=payload.title,
            display_name=payload.display_name,
            teachers=payload.teachers,
            room=payload.room,
            weekday=payload.weekday,
            periods=payload.periods,
            week_numbers=payload.week_numbers,
            start_time=payload.start_time,
            end_time=payload.end_time,
        )
        conflicts = _planned_series_conflicts(
            db,
            user_id=user_id,
            term=term,
            weekday=candidate["weekday"],
            periods=candidate["periods"],
            weeks=candidate["week_set"],
            start_time=candidate["start_time"],
            end_time=candidate["end_time"],
        )
        series_id = str(uuid4())
        import_id = str(uuid4())
        # 保留原始导入表外键，同时把人工创建标记为独立来源，避免被误当作 Excel 文件。
        manual_import = TimetableImport(
            id=import_id,
            user_id=user_id,
            term_id=term.id,
            term_code=term.term_code,
            file_hash=hashlib.sha256(f"manual:{user_id}:{series_id}".encode()).hexdigest(),
            filename="手动课程",
            file_format="manual",
            status="confirmed",
            apply_mode="manual",
            draft_json={"source": "manual", "course": candidate},
            diff_json={},
            version=2,
            created_at_epoch=now,
            updated_at_epoch=now,
            confirmed_at_epoch=now,
        )
        db.add(manual_import)
        db.flush()
        series = CourseSeries(
            id=series_id,
            user_id=user_id,
            term_id=term.id,
            source_import_id=import_id,
            identity_key=candidate["identity_key"],
            title=candidate["title"],
            display_name=candidate["display_name"],
            teachers_json=candidate["teachers"],
            room=candidate["room"],
            week_set_json=candidate["week_set"],
            weekday=candidate["weekday"],
            periods_json=candidate["periods"],
            start_time=candidate["start_time"],
            end_time=candidate["end_time"],
            raw_text=(payload.raw_text or "用户手动新增的常规课程").strip(),
            status="active",
            version=1,
            created_at_epoch=now,
            updated_at_epoch=now,
        )
        db.add(series)
        db.flush()
        _materialize_occurrences(
            db,
            user_id=user_id,
            term=term,
            course=series,
            source_import_id=manual_import.id,
            now=now,
        )
        change_set = _course_series_change_set(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id,
            summary=f"{'Agent' if actor_type == 'agent' else '用户'}新增课程：{series.display_name}",
            action="course.create",
            series=series,
            now=now,
            operation_type="course_series_created",
        )
        # 让事务内的文字结果也携带完整系列字段，页面无需再发一次请求。
        public_series = course_series_public(series, term=term)
        public_change_set = business_service.change_set_public(change_set)
    return {"series": public_series, "change_set": public_change_set, "conflicts": conflicts}


def update_course_series(
    user_id: str,
    series_id: str,
    payload: CourseSeriesUpdateInput,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> dict[str, Any]:
    """修改常规课程系列并重新物化实例；已有单次例外继续保留。"""
    from . import business_service

    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        series = _owned(db, CourseSeries, series_id, user_id)
        if series.status != "active" or series.version != payload.expected_version:
            raise ApiProblem("version_conflict", "课程系列已变化，请刷新后重试。", 409)
        term = _owned(db, AcademicTerm, series.term_id, user_id)
        candidate = _manual_course_values(
            db,
            user_id=user_id,
            term=term,
            title=payload.title,
            display_name=payload.display_name,
            teachers=payload.teachers,
            room=payload.room,
            weekday=payload.weekday,
            periods=payload.periods,
            week_numbers=payload.week_numbers,
            start_time=payload.start_time if "start_time" in payload.model_fields_set or payload.periods != series.periods_json else series.start_time,
            end_time=payload.end_time if "end_time" in payload.model_fields_set or payload.periods != series.periods_json else series.end_time,
            series_id=series.id,
        )
        conflicts = _planned_series_conflicts(
            db,
            user_id=user_id,
            term=term,
            weekday=candidate["weekday"],
            periods=candidate["periods"],
            weeks=candidate["week_set"],
            start_time=candidate["start_time"],
            end_time=candidate["end_time"],
            series_id=series.id,
        )
        before = business_service._snapshot(series, "course_series")
        series.identity_key = candidate["identity_key"]
        series.title = candidate["title"]
        series.display_name = candidate["display_name"]
        series.teachers_json = candidate["teachers"]
        series.room = candidate["room"]
        series.week_set_json = candidate["week_set"]
        series.weekday = candidate["weekday"]
        series.periods_json = candidate["periods"]
        series.start_time = candidate["start_time"]
        series.end_time = candidate["end_time"]
        if payload.raw_text is not None:
            series.raw_text = payload.raw_text.strip()
        series.version += 1
        series.updated_at_epoch = now
        _materialize_occurrences(
            db,
            user_id=user_id,
            term=term,
            course=series,
            source_import_id=series.source_import_id,
            now=now,
        )
        change_set = _course_series_change_set(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id,
            summary=f"{'Agent' if actor_type == 'agent' else '用户'}修改课程系列：{series.display_name}",
            action="course.update",
            series=series,
            now=now,
            operation_type="course_series_snapshot",
            before=before,
        )
        public_series = course_series_public(series, term=term)
        public_change_set = business_service.change_set_public(change_set)
    return {"series": public_series, "change_set": public_change_set, "conflicts": conflicts}


def set_course_series_status(
    user_id: str,
    series_id: str,
    expected_version: int,
    *,
    restore: bool,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> dict[str, Any]:
    """软归档或恢复整门课程；归档会停止未来实例，恢复时重新物化且保护单次例外。"""
    from . import business_service

    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        series = _owned(db, CourseSeries, series_id, user_id)
        expected_status = "archived" if restore else "active"
        target_status = "active" if restore else "archived"
        if series.status != expected_status or series.version != expected_version:
            raise ApiProblem("version_conflict", "课程系列已变化，请刷新后重试。", 409)
        term = _owned(db, AcademicTerm, series.term_id, user_id)
        if restore:
            _manual_course_values(
                db,
                user_id=user_id,
                term=term,
                title=series.title,
                display_name=series.display_name,
                teachers=series.teachers_json,
                room=series.room,
                weekday=series.weekday,
                periods=series.periods_json,
                week_numbers=series.week_set_json,
                series_id=series.id,
            )
            conflicts = _planned_series_conflicts(
                db,
                user_id=user_id,
                term=term,
                weekday=series.weekday,
                periods=series.periods_json,
                weeks=series.week_set_json,
                series_id=series.id,
            )
        else:
            conflicts = []
        before = business_service._snapshot(series, "course_series")
        series.status = target_status
        series.version += 1
        series.updated_at_epoch = now
        if restore:
            _materialize_occurrences(
                db,
                user_id=user_id,
                term=term,
                course=series,
                source_import_id=series.source_import_id,
                now=now,
            )
        action = "course.restore" if restore else "course.archive"
        verb = "恢复" if restore else "归档"
        change_set = _course_series_change_set(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id,
            summary=f"{'Agent' if actor_type == 'agent' else '用户'}{verb}课程：{series.display_name}",
            action=action,
            series=series,
            now=now,
            operation_type="course_series_snapshot",
            before=before,
        )
        public_series = course_series_public(series, term=term)
        public_change_set = business_service.change_set_public(change_set)
    return {"series": public_series, "change_set": public_change_set, "conflicts": conflicts}


def restore_course_series_in_session(
    db: DbSession,
    *,
    user_id: str,
    series: CourseSeries,
    before: dict[str, Any],
    now: int,
) -> CourseSeries:
    """供通用撤销事务恢复课程系列字段并重新生成实例，不提交独立事务。"""
    target_identity = before.get("identity_key")
    target_status = before.get("status")
    if target_status == "active" and target_identity:
        collision = db.scalar(
            select(CourseSeries).where(
                CourseSeries.user_id == user_id,
                CourseSeries.term_id == series.term_id,
                CourseSeries.identity_key == target_identity,
                CourseSeries.status == "active",
                CourseSeries.id != series.id,
            )
        )
        if collision is not None:
            raise ApiProblem(
                "course_exists",
                "撤销后课程身份会与现有课程冲突，请先处理重复课程。",
                409,
                {"series_id": collision.id},
            )
    for key, value in before.items():
        if key not in {"version", "updated_at_epoch"}:
            setattr(series, key, deepcopy(value))
    series.version += 1
    series.updated_at_epoch = now
    term = _owned(db, AcademicTerm, series.term_id, user_id)
    if series.status == "active":
        _materialize_occurrences(
            db,
            user_id=user_id,
            term=term,
            course=series,
            source_import_id=series.source_import_id,
            now=now,
        )
    return series


def list_course_series(
    user_id: str,
    *,
    term_id: str | None = None,
    status: str = "all",
    query: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """列出常规课程系列，供网页管理与 Agent 查找可恢复的归档课程。"""
    if status not in {"active", "archived", "all"}:
        raise ApiProblem("invalid_status", "课程状态筛选无效。", 422)
    with DbSession(_engine()) as db:
        statement = select(CourseSeries).where(CourseSeries.user_id == user_id)
        if term_id:
            term = _owned(db, AcademicTerm, term_id, user_id)
            statement = statement.where(CourseSeries.term_id == term.id)
        if status != "all":
            statement = statement.where(CourseSeries.status == status)
        if query.strip():
            token = f"%{query.strip()[:200]}%"
            statement = statement.where(
                CourseSeries.display_name.ilike(token)
                | CourseSeries.room.ilike(token)
            )
        rows = db.scalars(
            statement.order_by(
                CourseSeries.status,
                CourseSeries.weekday,
                CourseSeries.display_name,
            ).limit(max(1, min(limit, 200)))
        ).all()
        term_ids = {item.term_id for item in rows}
        terms = {
            term.id: term
            for term in db.scalars(
                select(AcademicTerm).where(
                    AcademicTerm.user_id == user_id,
                    AcademicTerm.id.in_(term_ids or {"__none__"}),
                )
            ).all()
        }
        counts = {
            item.id: int(
                db.scalar(
                    select(func.count(CourseOccurrence.id)).where(
                        CourseOccurrence.user_id == user_id,
                        CourseOccurrence.course_series_id == item.id,
                        CourseOccurrence.status.in_(("scheduled", "rescheduled")),
                    )
                )
                or 0
            )
            for item in rows
        }
        return {
            "items": [
                course_series_public(
                    item,
                    term=terms.get(item.term_id),
                    occurrence_count=counts[item.id],
                )
                for item in rows
            ]
        }


def list_terms(user_id: str) -> list[dict[str, Any]]:
    with DbSession(_engine()) as db:
        terms = db.scalars(
            select(AcademicTerm).where(AcademicTerm.user_id == user_id).order_by(AcademicTerm.week1_monday.desc())
        ).all()
    return [
        {
            "id": term.id,
            "term_code": term.term_code,
            "week1_monday": term.week1_monday,
            "week_count": term.week_count,
            "timezone": term.timezone,
            "period_profile": term.period_profile_json,
            "anchor_source": term.anchor_source,
            "version": term.version,
        }
        for term in terms
    ]


def list_imports(user_id: str) -> list[dict[str, Any]]:
    with DbSession(_engine()) as db:
        items = db.scalars(
            select(TimetableImport)
            .where(
                TimetableImport.user_id == user_id,
                # 手动新增课程复用了导入外键，但不应该出现在 Excel 导入历史中。
                TimetableImport.file_format != "manual",
            )
            .order_by(TimetableImport.created_at_epoch.desc())
            .limit(50)
        ).all()
    return [import_public(item) for item in items]


def get_import(user_id: str, import_id: str) -> dict[str, Any]:
    """核对页刷新后按 owner 恢复已解析的文件。"""
    with DbSession(_engine()) as db:
        return import_public(_owned(db, TimetableImport, import_id, user_id))


def _overlaps(first_start: datetime, first_end: datetime, second_start: datetime, second_end: datetime) -> bool:
    """半开区间比较，相邻结束和开始时刻不算冲突。"""
    return first_start < second_end and first_end > second_start


def _split_busy_interval(start_at: datetime, end_at: datetime, timezone_name: str) -> list[tuple[str, dict[str, str]]]:
    """把跨午夜的忙碌区间按指定时区的自然日切开，便于周视图呈现。"""
    zone = ZoneInfo(timezone_name)
    start_utc = start_at.astimezone(timezone.utc).timestamp()
    end_utc = end_at.astimezone(timezone.utc).timestamp()
    first_day = start_at.astimezone(zone).date()
    last_day = end_at.astimezone(zone).date()
    result: list[tuple[str, dict[str, str]]] = []
    day = first_day
    while day <= last_day:
        day_start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc).timestamp()
        day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc).timestamp()
        segment_start = max(start_utc, day_start)
        segment_end = min(end_utc, day_end)
        if segment_start < segment_end:
            result.append(
                (
                    day.isoformat(),
                    {
                        "start_at": datetime.fromtimestamp(segment_start, timezone.utc).astimezone(zone).isoformat(),
                        "end_at": datetime.fromtimestamp(segment_end, timezone.utc).astimezone(zone).isoformat(),
                    },
                )
            )
        day += timedelta(days=1)
    return result


def _busy_intervals_for_range(
    db: DbSession,
    user_id: str,
    start_at: datetime,
    end_at: datetime,
    exclude_entry_id: str | None = None,
    exclude_occurrence_id: str | None = None,
):
    """跨日冲突按实际带时区时刻比较，并覆盖临近时区日期与最长安排时长。"""
    lower_date = start_at.date() - timedelta(days=8)
    upper_date = end_at.date() + timedelta(days=1)
    lower_text, upper_text = lower_date.isoformat(), upper_date.isoformat()
    intervals: list[dict[str, Any]] = []
    occurrences = db.scalars(
        select(CourseOccurrence)
        .join(CourseSeries, CourseSeries.id == CourseOccurrence.course_series_id)
        .where(
            CourseOccurrence.user_id == user_id,
            CourseOccurrence.local_date >= lower_text,
            CourseOccurrence.local_date <= upper_text,
            CourseOccurrence.status.in_(("scheduled", "rescheduled")),
            CourseSeries.status == "active",
        )
    ).all()
    for occurrence in occurrences:
        if occurrence.id == exclude_occurrence_id:
            continue
        course = db.scalar(select(CourseSeries).where(CourseSeries.id == occurrence.course_series_id, CourseSeries.user_id == user_id))
        if not course:
            continue
        for segment in occurrence.busy_segments_json:
            intervals.append(
                {
                    "object_type": "course",
                    "object_id": occurrence.id,
                    "title": course.display_name,
                    "start_at": datetime.fromisoformat(segment["start_at"]),
                    "end_at": datetime.fromisoformat(segment["end_at"]),
                }
            )
    entries = db.scalars(
        select(ScheduleEntry).where(
            ScheduleEntry.user_id == user_id,
            ScheduleEntry.local_date >= lower_text,
            ScheduleEntry.local_date <= upper_text,
            ScheduleEntry.status == "active",
            ScheduleEntry.kind == "busy_block",
        )
    ).all()
    for entry in entries:
        if entry.id == exclude_entry_id or not entry.start_at or not entry.end_at:
            continue
        intervals.append(
            {
                "object_type": "schedule_entry",
                "object_id": entry.id,
                "title": entry.title,
                "start_at": datetime.fromisoformat(entry.start_at),
                "end_at": datetime.fromisoformat(entry.end_at),
            }
        )
    return intervals


def _conflicts_for_interval(
    db: DbSession,
    *,
    user_id: str,
    start_at: datetime,
    end_at: datetime,
    exclude_entry_id: str | None = None,
    exclude_occurrence_id: str | None = None,
) -> list[dict[str, Any]]:
    conflicts = []
    for existing in _busy_intervals_for_range(
        db,
        user_id,
        start_at,
        end_at,
        exclude_entry_id,
        exclude_occurrence_id,
    ):
        if _overlaps(start_at, end_at, existing["start_at"], existing["end_at"]):
            conflicts.append(
                {
                    "object_type": existing["object_type"],
                    "object_id": existing["object_id"],
                    "title": existing["title"],
                    "start_at": existing["start_at"].isoformat(),
                    "end_at": existing["end_at"].isoformat(),
                }
            )
    return conflicts


def create_schedule_entry_in_session(
    db: DbSession,
    user_id: str,
    payload: ScheduleEntryInput,
    *,
    now_epoch: int | None = None,
) -> tuple[ScheduleEntry, list[dict[str, Any]]]:
    """在调用方事务中创建日程，供网页和 Agent 共用同一冲突校验。"""
    now = _now() if now_epoch is None else now_epoch
    zone = ZoneInfo(payload.timezone)
    if payload.start_at and payload.start_at.astimezone(zone).date() != payload.local_date:
        raise ApiProblem("invalid_schedule_date", "开始时间与本地日期不一致。", 422)
    if payload.source_event_id:
        event = db.scalar(
            select(Event).where(Event.id == payload.source_event_id, Event.user_id == user_id)
        )
        if event is None:
            raise ApiProblem("not_found", "未找到关联事项。", 404)
    conflicts: list[dict[str, Any]] = []
    if payload.kind == "busy_block":
        conflicts = _conflicts_for_interval(
            db,
            user_id=user_id,
            start_at=payload.start_at,
            end_at=payload.end_at,
        )
        if conflicts and not payload.allow_conflict:
            raise ApiProblem(
                "schedule_conflict",
                "该时间与现有课程或安排冲突。",
                409,
                {"conflicts": conflicts},
            )
    entry = ScheduleEntry(
        id=str(uuid4()),
        user_id=user_id,
        source_event_id=payload.source_event_id,
        title=payload.title,
        kind=payload.kind,
        local_date=payload.local_date.isoformat(),
        start_at=payload.start_at.isoformat() if payload.start_at else None,
        end_at=payload.end_at.isoformat() if payload.end_at else None,
        timezone=payload.timezone,
        status="active",
        version=1,
        created_at_epoch=now,
        updated_at_epoch=now,
    )
    db.add(entry)
    return entry, conflicts


def preview_schedule_entry_conflicts(
    user_id: str,
    payload: ScheduleEntryInput,
    *,
    exclude_entry_id: str | None = None,
) -> list[dict[str, Any]]:
    """创建或更改前只读检查时段；确认时仍会在事务内重新检查。"""
    zone = ZoneInfo(payload.timezone)
    if payload.start_at and payload.start_at.astimezone(zone).date() != payload.local_date:
        raise ApiProblem("invalid_schedule_date", "开始时间与本地日期不一致。", 422)
    if payload.source_event_id:
        with DbSession(_engine()) as db:
            event = db.scalar(
                select(Event).where(Event.id == payload.source_event_id, Event.user_id == user_id)
            )
        if event is None:
            raise ApiProblem("not_found", "未找到关联事项。", 404)
    if payload.kind != "busy_block":
        return []
    with DbSession(_engine()) as db:
        return _conflicts_for_interval(
            db,
            user_id=user_id,
            start_at=payload.start_at,
            end_at=payload.end_at,
            exclude_entry_id=exclude_entry_id,
        )


def create_schedule_entry(user_id: str, payload: ScheduleEntryInput) -> dict[str, Any]:
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        entry, conflicts = create_schedule_entry_in_session(db, user_id, payload, now_epoch=now)
    return {"entry": schedule_entry_public(entry), "conflicts": conflicts}


def update_schedule_entry_in_session(
    db: DbSession,
    user_id: str,
    entry_id: str,
    payload: UpdateScheduleEntryInput,
    *,
    now_epoch: int | None = None,
) -> tuple[ScheduleEntry, list[dict[str, Any]]]:
    """在调用方事务中按版本修改日程并执行相同的冲突校验。"""
    now = _now() if now_epoch is None else now_epoch
    entry = _owned(db, ScheduleEntry, entry_id, user_id)
    if entry.version != payload.expected_version or entry.status != "active":
        raise ApiProblem("version_conflict", "日程已变化，请刷新后重试。", 409)
    zone = ZoneInfo(payload.timezone)
    if payload.start_at and payload.start_at.astimezone(zone).date() != payload.local_date:
        raise ApiProblem("invalid_schedule_date", "开始时间与本地日期不一致。", 422)
    if payload.source_event_id:
        source_event = db.scalar(
            select(Event).where(Event.id == payload.source_event_id, Event.user_id == user_id)
        )
        if source_event is None:
            raise ApiProblem("not_found", "未找到关联事项。", 404)
    conflicts: list[dict[str, Any]] = []
    if payload.kind == "busy_block":
        conflicts = _conflicts_for_interval(
            db,
            user_id=user_id,
            start_at=payload.start_at,
            end_at=payload.end_at,
            exclude_entry_id=entry.id,
        )
        if conflicts and not payload.allow_conflict:
            raise ApiProblem("schedule_conflict", "该时间与现有课程或安排冲突。", 409, {"conflicts": conflicts})
    entry.title = payload.title
    entry.source_event_id = payload.source_event_id
    entry.kind = payload.kind
    entry.local_date = payload.local_date.isoformat()
    entry.start_at = payload.start_at.isoformat() if payload.start_at else None
    entry.end_at = payload.end_at.isoformat() if payload.end_at else None
    entry.timezone = payload.timezone
    entry.version += 1
    entry.updated_at_epoch = now
    return entry, conflicts


def archive_schedule_entry_in_session(
    db: DbSession,
    user_id: str,
    entry_id: str,
    expected_version: int,
    *,
    now_epoch: int | None = None,
) -> ScheduleEntry:
    """在调用方事务中软归档日程并检查当前对象版本。"""
    now = _now() if now_epoch is None else now_epoch
    entry = _owned(db, ScheduleEntry, entry_id, user_id)
    if entry.version != expected_version or entry.status != "active":
        raise ApiProblem("version_conflict", "日程已变化，请刷新后重试。", 409)
    entry.status = "archived"
    entry.archived_at_epoch = now
    entry.version += 1
    entry.updated_at_epoch = now
    return entry


def update_schedule_entry(
    user_id: str,
    entry_id: str,
    payload: UpdateScheduleEntryInput,
) -> dict[str, Any]:
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        entry, conflicts = update_schedule_entry_in_session(
            db,
            user_id,
            entry_id,
            payload,
            now_epoch=now,
        )
    return {"entry": schedule_entry_public(entry), "conflicts": conflicts}


def archive_schedule_entry(user_id: str, entry_id: str, expected_version: int) -> dict[str, Any]:
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        entry = archive_schedule_entry_in_session(
            db,
            user_id,
            entry_id,
            expected_version,
            now_epoch=now,
        )
    return schedule_entry_public(entry)


def restore_schedule_entry_in_session(
    db: DbSession,
    user_id: str,
    entry_id: str,
    expected_version: int,
    *,
    allow_conflict: bool = False,
    now_epoch: int | None = None,
) -> tuple[ScheduleEntry, list[dict[str, Any]]]:
    """在调用方事务中恢复软归档日程，复用与网页相同的冲突保护。"""
    now = _now() if now_epoch is None else now_epoch
    entry = _owned(db, ScheduleEntry, entry_id, user_id)
    if entry.version != expected_version or entry.status != "archived":
        raise ApiProblem("version_conflict", "日程已变化，无法恢复。", 409)
    conflicts: list[dict[str, Any]] = []
    if entry.kind == "busy_block" and entry.start_at and entry.end_at:
        conflicts = _conflicts_for_interval(
            db,
            user_id=user_id,
            start_at=datetime.fromisoformat(entry.start_at),
            end_at=datetime.fromisoformat(entry.end_at),
            exclude_entry_id=entry.id,
        )
        if conflicts and not allow_conflict:
            raise ApiProblem("schedule_conflict", "恢复日程会与现有安排冲突。", 409, {"conflicts": conflicts})
    entry.status = "active"
    entry.archived_at_epoch = None
    entry.version += 1
    entry.updated_at_epoch = now
    return entry, conflicts


def restore_schedule_entry(
    user_id: str,
    entry_id: str,
    expected_version: int,
    *,
    allow_conflict: bool = False,
) -> dict[str, Any]:
    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        entry, conflicts = restore_schedule_entry_in_session(
            db,
            user_id,
            entry_id,
            expected_version,
            allow_conflict=allow_conflict,
            now_epoch=now,
        )
    return {"entry": schedule_entry_public(entry), "conflicts": conflicts}


def schedule_entry_public(entry: ScheduleEntry) -> dict[str, Any]:
    return {
        "planning_policy": entry.planning_policy_json or {},
        "id": entry.id,
        "source_event_id": entry.source_event_id,
        "title": entry.title,
        "kind": entry.kind,
        "local_date": entry.local_date,
        "start_at": entry.start_at,
        "end_at": entry.end_at,
        "timezone": entry.timezone,
        "status": entry.status,
        "version": entry.version,
    }


def list_schedule(user_id: str, start_date: date, end_date: date) -> dict[str, Any]:
    if end_date < start_date or (end_date - start_date).days > 62:
        raise ApiProblem("invalid_date_range", "日程范围最多为 63 天。", 422)
    start_text, end_text = start_date.isoformat(), end_date.isoformat()
    with DbSession(_engine()) as db:
        occurrences = db.execute(
            select(CourseOccurrence, CourseSeries, AcademicTerm)
            .join(CourseSeries, CourseSeries.id == CourseOccurrence.course_series_id)
            .join(AcademicTerm, AcademicTerm.id == CourseOccurrence.term_id)
            .where(
                CourseOccurrence.user_id == user_id,
                CourseOccurrence.local_date >= (start_date - timedelta(days=8)).isoformat(),
                CourseOccurrence.local_date <= (end_date + timedelta(days=1)).isoformat(),
                CourseOccurrence.status.in_(("scheduled", "rescheduled")),
                CourseSeries.status == "active",
            )
            .order_by(CourseOccurrence.local_date, CourseOccurrence.display_start)
        ).all()
        entries = db.scalars(
            select(ScheduleEntry).where(
                ScheduleEntry.user_id == user_id,
                ScheduleEntry.local_date >= (start_date - timedelta(days=8)).isoformat(),
                ScheduleEntry.local_date <= (end_date + timedelta(days=1)).isoformat(),
                ScheduleEntry.status == "active",
            ).order_by(ScheduleEntry.local_date, ScheduleEntry.start_at)
        ).all()
        terms = db.scalars(select(AcademicTerm).where(AcademicTerm.user_id == user_id)).all()
        tasks = db.scalars(select(Task).where(Task.user_id == user_id, Task.status == "open")).all()
        course_items: list[dict[str, Any]] = []
        for occurrence, series, term in occurrences:
            base = {
                "id": occurrence.id,
                "course_series_id": series.id,
                "item_type": "course",
                "title": series.display_name,
                "weekday": series.weekday,
                "week_number": occurrence.week_number,
                "teachers": series.teachers_json,
                "room": occurrence.room_override or series.room,
                "periods": series.periods_json,
                "term_code": term.term_code,
                "version": occurrence.version,
            }
            # 同一课程实例可能包含多个相邻节次时段；页面按日期聚合为一个卡片，
            # 同时保留每个忙碌片段供冲突计算，避免同一门课重复显示。
            segments_by_date: dict[str, list[dict[str, str]]] = {}
            for segment in occurrence.busy_segments_json:
                start_at = datetime.fromisoformat(segment["start_at"])
                end_at = datetime.fromisoformat(segment["end_at"])
                for segment_date, day_segment in _split_busy_interval(start_at, end_at, term.timezone):
                    if start_text <= segment_date <= end_text:
                        segments_by_date.setdefault(segment_date, []).append(day_segment)
            for segment_date, day_segments in segments_by_date.items():
                day_segments.sort(key=lambda item: item["start_at"])
                course_items.append(
                    {
                        **base,
                        "local_date": segment_date,
                        "start_at": occurrence.display_start,
                        "end_at": occurrence.display_end,
                        "busy_segments": day_segments,
                    }
                )
        course_items.sort(key=lambda item: (item["local_date"], item["start_at"], item["id"]))
        schedule_items: list[dict[str, Any]] = []
        for entry in entries:
            public = schedule_entry_public(entry)
            if entry.kind != "busy_block" or not entry.start_at or not entry.end_at:
                if start_text <= entry.local_date <= end_text:
                    schedule_items.append({**public, "item_type": "personal", "busy_segments": []})
                continue
            for segment_date, segment in _split_busy_interval(
                datetime.fromisoformat(entry.start_at),
                datetime.fromisoformat(entry.end_at),
                entry.timezone,
            ):
                if not start_text <= segment_date <= end_text:
                    continue
                schedule_items.append({
                    **public,
                    "item_type": "personal",
                    "local_date": segment_date,
                    "start_at": segment["start_at"],
                    "end_at": segment["end_at"],
                    "original_start_at": entry.start_at,
                    "original_end_at": entry.end_at,
                    "busy_segments": [segment],
                })
        deadline_items = []
        for task in tasks:
            if task.due_json and task.due_json.get("local_date"):
                local_date = task.due_json["local_date"]
                if start_text <= local_date <= end_text:
                    deadline_items.append({"id": task.id, "title": task.title, "local_date": local_date, "precision": task.due_json.get("precision"), "raw": task.due_json.get("raw")})
        all_items = course_items + schedule_items
        conflicts: list[dict[str, Any]] = []
        checked_pairs: set[tuple[str, str, str, str]] = set()
        for index, first in enumerate(all_items):
            for second in all_items[index + 1 :]:
                if first["id"] == second["id"]:
                    continue
                for first_segment in first["busy_segments"]:
                    for second_segment in second["busy_segments"]:
                        first_start = datetime.fromisoformat(first_segment["start_at"])
                        first_end = datetime.fromisoformat(first_segment["end_at"])
                        second_start = datetime.fromisoformat(second_segment["start_at"])
                        second_end = datetime.fromisoformat(second_segment["end_at"])
                        if _overlaps(first_start, first_end, second_start, second_end):
                            intersection_start = max(first_start, second_start).isoformat()
                            intersection_end = min(first_end, second_end).isoformat()
                            pair_key = tuple(sorted((first["id"], second["id"]))) + (intersection_start, intersection_end)
                            if pair_key in checked_pairs:
                                continue
                            checked_pairs.add(pair_key)
                            conflicts.append(
                                {
                                    "local_date": min(first["local_date"], second["local_date"]),
                                    "first": {"id": first["id"], "title": first["title"]},
                                    "second": {"id": second["id"], "title": second["title"]},
                                    "start_at": intersection_start,
                                    "end_at": intersection_end,
                                }
                            )
    return {
        "start_date": start_text,
        "end_date": end_text,
        "terms": [
            # 网页小节联动必须读取服务端当前作息，不能只依赖类型声明或旧页面缓存。
            {"id": term.id, "term_code": term.term_code, "week1_monday": term.week1_monday, "week_count": term.week_count, "timezone": term.timezone,
             "period_profile": term.period_profile_json, "anchor_source": term.anchor_source, "version": term.version}
            for term in terms
        ],
        "items": all_items,
        "deadlines": deadline_items,
        "conflicts": conflicts,
    }


def apply_course_exception(
    user_id: str,
    occurrence_id: str,
    payload: CourseExceptionInput,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
    source_document_id: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """只修改一个课程实例，并把通知依据、操作审计和变更集一起写入。"""
    from . import business_service, cross_module_service, operation_service

    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        occurrence = _owned(db, CourseOccurrence, occurrence_id, user_id)
        series = _owned(db, CourseSeries, occurrence.course_series_id, user_id)
        if series.status != "active":
            raise ApiProblem("course_archived", "课程系列已归档，请先恢复后再调整。", 409)
        notice = None
        if payload.notice_id:
            notice = db.scalar(
                select(Notice).where(Notice.id == payload.notice_id, Notice.user_id == user_id)
            )
            if notice is None:
                raise ApiProblem("not_found", "课程变更引用的通知不存在。", 404)
            source_document_id = source_document_id or notice.source_document_id
        elif source_document_id:
            # Agent 自动路径通常只持有来源 ID；仍要反查通知以建立可点击证据链。
            notice = db.scalar(
                select(Notice).where(
                    Notice.user_id == user_id,
                    Notice.source_document_id == source_document_id,
                ).order_by(Notice.created_at_epoch.desc()).limit(1)
            )
        if source_document_id:
            source = db.scalar(
                select(SourceDocument).where(
                    SourceDocument.id == source_document_id,
                    SourceDocument.user_id == user_id,
                )
            )
            if source is None:
                raise ApiProblem("not_found", "课程变更依据不存在。", 404)
        if occurrence.version != payload.expected_version or occurrence.status not in {"scheduled", "rescheduled"}:
            raise ApiProblem("version_conflict", "课程实例已变化，请刷新后重试。", 409)
        active_exception = db.scalar(
            select(CourseException).where(
                CourseException.user_id == user_id,
                CourseException.occurrence_id == occurrence.id,
                CourseException.status == "active",
            )
        )
        if active_exception:
            raise ApiProblem("exception_exists", "该课程实例已有单次例外，请先撤销或编辑例外。", 409)
        original = {
            "local_date": occurrence.local_date,
            "display_start": occurrence.display_start,
            "display_end": occurrence.display_end,
            "busy_segments": deepcopy(occurrence.busy_segments_json),
            "room_override": occurrence.room_override,
            "status": occurrence.status,
        }
        replacement = None
        if payload.kind == "cancel":
            occurrence.status = "cancelled"
        elif payload.kind == "room_change":
            replacement = {"room": payload.replacement_room}
            occurrence.room_override = payload.replacement_room
        else:
            replacement_start = payload.replacement_start
            replacement_end = payload.replacement_end
            conflicts = _conflicts_for_interval(
                db,
                user_id=user_id,
                start_at=replacement_start,
                end_at=replacement_end,
                exclude_occurrence_id=occurrence.id,
            )
            if conflicts:
                raise ApiProblem("schedule_conflict", "改期与现有课程或安排冲突。", 409, {"conflicts": conflicts})
            replacement = {"start_at": replacement_start.isoformat(), "end_at": replacement_end.isoformat()}
            if payload.replacement_room:
                replacement["room"] = payload.replacement_room
                occurrence.room_override = payload.replacement_room
            occurrence.local_date = replacement_start.date().isoformat()
            occurrence.display_start = replacement_start.strftime("%H:%M")
            occurrence.display_end = replacement_end.strftime("%H:%M")
            occurrence.busy_segments_json = [replacement]
            occurrence.status = "rescheduled"
        occurrence.version += 1
        change_set = ChangeSet(
            id=str(uuid4()),
            user_id=user_id,
            source_document_id=source_document_id,
            kind="operations",
            status="confirmed",
            summary=f"{'Agent' if actor_type == 'agent' else '用户'}{'取消' if payload.kind == 'cancel' else '调整'}课程：{series.display_name} {occurrence.local_date}"[:1000],
            bundle_json=None,
            operations_json=[
                {
                    "type": "course_exception_created",
                    "object_type": "course_exception",
                    "object_id": "",
                    "occurrence_id": occurrence.id,
                    "before": original,
                    "applied_version": occurrence.version,
                }
            ],
            version=2,
            change_seq=business_service._next_change_seq(db, user_id),
            created_at_epoch=now,
            updated_at_epoch=now,
            confirmed_at_epoch=now,
        )
        db.add(change_set)
        db.flush()
        exception = CourseException(
            id=str(uuid4()),
            user_id=user_id,
            occurrence_id=occurrence.id,
            notice_id=notice.id if notice else None,
            source_document_id=source_document_id,
            change_set_id=change_set.id,
            actor_type=actor_type,
            kind=payload.kind,
            original_json=original,
            replacement_json=replacement,
            evidence_json={"refs": evidence or []},
            reason=payload.reason,
            status="active",
            version=1,
            created_at_epoch=now,
        )
        db.add(exception)
        db.flush()
        # JSON 列不追踪嵌套原地修改，重新赋值整张操作表才能持久化例外 ID。
        change_set.operations_json = [
            {
                "type": "course_exception_created",
                "object_type": "course_exception",
                "object_id": exception.id,
                "occurrence_id": occurrence.id,
                "before": original,
                "applied_version": occurrence.version,
            }
        ]
        if notice:
            cross_module_service.link_entities_in_session(
                db,
                user_id=user_id,
                payload=LinkInput(
                    from_type="notice",
                    from_id=notice.id,
                    to_type="course_occurrence",
                    to_id=occurrence.id,
                    relation="course_change",
                ),
                source_document_id=source_document_id,
                created_at_epoch=now,
            )
        after = {
            "occurrence_id": occurrence.id,
            "exception_id": exception.id,
            "exception_version": exception.version,
            "local_date": occurrence.local_date,
            "display_start": occurrence.display_start,
            "display_end": occurrence.display_end,
            "room": occurrence.room_override or series.room,
            "status": occurrence.status,
            "version": occurrence.version,
        }
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id or user_id,
            action=f"course.{payload.kind}",
            object_type="course_occurrence",
            object_id=occurrence.id,
            change_set_id=change_set.id,
            source_document_id=source_document_id,
            summary=change_set.summary,
            before=original,
            after=after,
            undoable=True,
            created_at_epoch=now,
        )
    return {
        "exception": {
            "id": exception.id,
            "occurrence_id": exception.occurrence_id,
            "kind": exception.kind,
            "reason": exception.reason,
            "notice_id": exception.notice_id,
            "replacement": exception.replacement_json,
            "version": exception.version,
        },
        "occurrence": {
            "id": occurrence.id,
            "course_series_id": series.id,
            "title": series.display_name,
            "local_date": occurrence.local_date,
            "display_start": occurrence.display_start,
            "display_end": occurrence.display_end,
            "room": occurrence.room_override or series.room,
            "status": occurrence.status,
            "version": occurrence.version,
        },
        "change_set": business_service.change_set_public(change_set),
    }


def create_course_makeup(
    user_id: str,
    series_id: str,
    payload: CourseMakeupInput,
    *,
    actor_type: str = "user",
    actor_id: str | None = None,
    source_document_id: str | None = None,
) -> dict[str, Any]:
    """创建一节临时补课，与常规课程系列和其他周次保持独立。"""
    from . import business_service, cross_module_service, operation_service

    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        series = _owned(db, CourseSeries, series_id, user_id)
        if series.status != "active":
            raise ApiProblem("course_archived", "课程系列已归档，请先恢复后再安排补课。", 409)
        term = _owned(db, AcademicTerm, series.term_id, user_id)
        notice = None
        if payload.notice_id:
            notice = db.scalar(
                select(Notice).where(Notice.id == payload.notice_id, Notice.user_id == user_id)
            )
            if notice is None:
                raise ApiProblem("not_found", "补课通知不存在。", 404)
            source_document_id = source_document_id or notice.source_document_id
        elif source_document_id:
            notice = db.scalar(
                select(Notice).where(
                    Notice.user_id == user_id,
                    Notice.source_document_id == source_document_id,
                ).order_by(Notice.created_at_epoch.desc()).limit(1)
            )
        conflicts = _conflicts_for_interval(
            db,
            user_id=user_id,
            start_at=payload.start_at,
            end_at=payload.end_at,
            exclude_occurrence_id=None,
        )
        if conflicts:
            raise ApiProblem("schedule_conflict", "补课时间与现有课程或安排冲突。", 409, {"conflicts": conflicts})
        week_start = date.fromisoformat(term.week1_monday)
        week_number = (payload.local_date - week_start).days // 7 + 1
        if week_number < 1 or week_number > term.week_count:
            raise ApiProblem("outside_term", "补课日期不在当前学期教学周内。", 422)
        occurrence_key = f"makeup:{series.id}:{payload.start_at.isoformat()}"
        existing = db.scalar(
            select(CourseOccurrence).where(
                CourseOccurrence.user_id == user_id,
                CourseOccurrence.occurrence_key == occurrence_key,
            )
        )
        if existing is not None:
            return {
                "occurrence": {
                    "id": existing.id,
                    "course_series_id": series.id,
                    "title": series.display_name,
                    "local_date": existing.local_date,
                    "start": existing.display_start,
                    "end": existing.display_end,
                    "room": existing.room_override or series.room,
                    "status": existing.status,
                    "version": existing.version,
                },
                "already_exists": True,
            }
        occurrence = CourseOccurrence(
            id=str(uuid4()),
            user_id=user_id,
            term_id=term.id,
            course_series_id=series.id,
            source_import_id=series.source_import_id,
            occurrence_key=occurrence_key,
            local_date=payload.local_date.isoformat(),
            week_number=week_number,
            display_start=payload.start_at.strftime("%H:%M"),
            display_end=payload.end_at.strftime("%H:%M"),
            busy_segments_json=[
                {"start_at": payload.start_at.isoformat(), "end_at": payload.end_at.isoformat()}
            ],
            room_override=payload.room,
            status="scheduled",
            version=1,
        )
        db.add(occurrence)
        db.flush()
        change_set = ChangeSet(
            id=str(uuid4()),
            user_id=user_id,
            source_document_id=source_document_id,
            kind="operations",
            status="confirmed",
            summary=f"{'Agent' if actor_type == 'agent' else '用户'}新增补课：{series.display_name} {payload.local_date.isoformat()}"[:1000],
            bundle_json=None,
            operations_json=[
                {
                    "type": "created",
                    "object_type": "course_occurrence",
                    "object_id": occurrence.id,
                    "applied_version": 1,
                }
            ],
            version=2,
            change_seq=business_service._next_change_seq(db, user_id),
            created_at_epoch=now,
            updated_at_epoch=now,
            confirmed_at_epoch=now,
        )
        db.add(change_set)
        if notice:
            cross_module_service.link_entities_in_session(
                db,
                user_id=user_id,
                payload=LinkInput(
                    from_type="notice",
                    from_id=notice.id,
                    to_type="course_occurrence",
                    to_id=occurrence.id,
                    relation="course_makeup",
                ),
                source_document_id=source_document_id,
                created_at_epoch=now,
            )
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id or user_id,
            action="course.makeup",
            object_type="course_occurrence",
            object_id=occurrence.id,
            change_set_id=change_set.id,
            source_document_id=source_document_id,
            summary=change_set.summary,
            after={
                "local_date": occurrence.local_date,
                "start": occurrence.display_start,
                "end": occurrence.display_end,
                "room": occurrence.room_override or series.room,
                "reason": payload.reason,
            },
            undoable=True,
            created_at_epoch=now,
        )
    return {
        "occurrence": {
            "id": occurrence.id,
            "course_series_id": series.id,
            "title": series.display_name,
            "local_date": occurrence.local_date,
            "start": occurrence.display_start,
            "end": occurrence.display_end,
            "room": occurrence.room_override or series.room,
            "status": occurrence.status,
            "version": occurrence.version,
        },
        "change_set": business_service.change_set_public(change_set),
    }


def undo_course_exception(user_id: str, exception_id: str, expected_version: int) -> dict[str, Any]:
    from . import operation_service

    now = _now()
    with DbSession(_engine(), expire_on_commit=False) as db, db.begin():
        exception = _owned(db, CourseException, exception_id, user_id)
        if exception.version != expected_version or exception.status != "active":
            raise ApiProblem("version_conflict", "课程例外已变化或已撤销。", 409)
        occurrence = _owned(db, CourseOccurrence, exception.occurrence_id, user_id)
        original = exception.original_json
        occurrence.local_date = original["local_date"]
        occurrence.display_start = original["display_start"]
        occurrence.display_end = original["display_end"]
        occurrence.busy_segments_json = original["busy_segments"]
        occurrence.room_override = original.get("room_override")
        occurrence.status = original["status"]
        occurrence.version += 1
        exception.status = "reverted"
        exception.version += 1
        operation_service.record_operation(
            db,
            user_id=user_id,
            actor_type="user",
            actor_id=user_id,
            action="course.undo",
            object_type="course_occurrence",
            object_id=occurrence.id,
            change_set_id=exception.change_set_id,
            source_document_id=exception.source_document_id,
            summary="撤销课程单次例外",
            before=exception.replacement_json or {},
            after=original,
            undoable=False,
            created_at_epoch=now,
        )
    return {"exception_id": exception.id, "status": exception.status, "occurrence_id": occurrence.id}
