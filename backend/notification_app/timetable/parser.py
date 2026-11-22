from dataclasses import dataclass
from io import BytesIO
import re
from typing import Any
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as SafeElementTree
from openpyxl import load_workbook
from openpyxl.xml import DEFUSEDXML
import xlrd

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_CELLS = 200_000
MAX_UNCOMPRESSED_XLSX = 50 * 1024 * 1024
_TERM_PATTERN = re.compile(r"20\d{2}-20\d{2}-[12]")
_WEEKDAYS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
}


@dataclass(frozen=True)
class GridWorkbook:
    """不同电子表格格式解析后的只读单元格网格。"""

    filename: str
    sheet_name: str
    rows: list[list[str]]
    merged_ranges: list[tuple[int, int, int, int]]


class TimetableParseError(ValueError):
    """可显示给用户的课表格式错误，不含文件内容或本机路径。"""


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _safe_xlsx_archive(data: bytes) -> None:
    """限制压缩包展开大小、条目数和宏内容，防止导入不可信压缩包。"""
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000:
                raise TimetableParseError("工作簿包含过多内部文件。")
            if sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_XLSX:
                raise TimetableParseError("工作簿展开后的内容超过 50 MB。")
            if any(item.file_size > 1_000_000 and item.file_size / max(item.compress_size, 1) > 150 for item in entries):
                raise TimetableParseError("工作簿压缩比例异常，已拒绝解析。")
            names = {item.filename.lower() for item in entries}
            if "xl/vbaproject.bin" in names:
                raise TimetableParseError("含宏工作簿不允许导入。")
            if "[content_types].xml" not in names or "xl/workbook.xml" not in names:
                raise TimetableParseError("文件不是有效的 .xlsx 工作簿。")
            # 用受限 XML 解析器先验证工作簿主清单，避免把畸形 XML 交给解析库。
            content_type = archive.read("[Content_Types].xml")
            SafeElementTree.fromstring(content_type)
    except BadZipFile:
        raise TimetableParseError("文件压缩结构无效。") from None


def read_workbook(data: bytes, filename: str) -> GridWorkbook:
    """根据文件签名选择 BIFF 或 OOXML 只读解析路径。"""
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise TimetableParseError("课表文件必须为 1 B 至 10 MB。")
    file_format = xlrd.inspect_format(content=data)
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if file_format == "xls" and suffix == "xls":
        try:
            book = xlrd.open_workbook(
                file_contents=data,
                formatting_info=True,
                on_demand=True,
                ragged_rows=True,
            )
            sheet = next((item for item in book.sheets() if item.visibility == 0), None)
            if sheet is None:
                raise TimetableParseError("工作簿没有可读取的工作表。")
            if sheet.nrows * max(sheet.ncols, 1) > MAX_CELLS:
                raise TimetableParseError("工作表行列数量超过导入上限。")
            rows = [
                [_cell_text(sheet.cell_value(row, col)) for col in range(sheet.row_len(row))]
                for row in range(sheet.nrows)
            ]
            merged_ranges = [tuple(int(value) for value in area) for area in sheet.merged_cells]
            result = GridWorkbook("课表.xls", sheet.name[:100], rows, merged_ranges)
            book.release_resources()
            return result
        except TimetableParseError:
            raise
        except Exception:
            raise TimetableParseError("BIFF .xls 文件无法解析；请检查文件是否损坏。") from None

    if file_format in {"xlsx", "zip"} and suffix == "xlsx":
        if not DEFUSEDXML:
            raise TimetableParseError("服务端缺少安全 XML 解析组件。")
        _safe_xlsx_archive(data)
        try:
            workbook = load_workbook(
                BytesIO(data),
                read_only=False,
                data_only=True,
                keep_links=False,
            )
            worksheet = next(
                (sheet for sheet in workbook.worksheets if sheet.sheet_state == "visible"),
                None,
            )
            if worksheet is None:
                raise TimetableParseError("工作簿没有可读取的工作表。")
            if worksheet.max_row * max(worksheet.max_column, 1) > MAX_CELLS:
                raise TimetableParseError("工作表行列数量超过导入上限。")
            rows = [
                [_cell_text(worksheet.cell(row=row, column=col).value) for col in range(1, worksheet.max_column + 1)]
                for row in range(1, worksheet.max_row + 1)
            ]
            merged_ranges = [
                (item.min_row - 1, item.max_row, item.min_col - 1, item.max_col)
                for item in worksheet.merged_cells.ranges
            ]
            result = GridWorkbook("课表.xlsx", worksheet.title[:100], rows, merged_ranges)
            workbook.close()
            return result
        except TimetableParseError:
            raise
        except Exception:
            raise TimetableParseError(".xlsx 文件无法解析；请检查文件是否损坏。") from None
    raise TimetableParseError("文件签名与支持的 .xls 或 .xlsx 格式不匹配。")


def _weekday_map(rows: list[list[str]]) -> tuple[int, dict[int, int]]:
    """定位星期表头和日列，支持星期列左右移动。"""
    best: tuple[int, dict[int, int]] | None = None
    for row_index, row in enumerate(rows):
        day_map: dict[int, int] = {}
        for col_index, value in enumerate(row):
            match = re.search(r"(?:星期|礼拜|周)\s*([一二三四五六日天1-7])", value)
            if match:
                day_map[col_index] = _WEEKDAYS[match.group(1)]
        if best is None or len(day_map) > len(best[1]):
            best = (row_index, day_map)
    if best is None or len(best[1]) < 5:
        raise TimetableParseError("未找到至少五天的星期表头；请确认上传的是课表。")
    return best


def _parse_period_label(value: str) -> tuple[list[int], str | None, str | None]:
    """从节次列提取小节集合和实际时间范围，不按课间平均拆分。"""
    lines = value.splitlines()
    periods: set[int] = set()
    for line in lines or [value]:
        if "大节" in line:
            continue
        if "节" not in line:
            continue
        for first, last in re.findall(r"(?<!\d)(\d{1,2})(?:\s*[-–—~至]\s*(\d{1,2}))?(?!\d)", line):
            start = int(first)
            end = int(last) if last else start
            if start <= end and end <= 30:
                periods.update(range(start, end + 1))
        if not periods:
            chinese = re.search(r"第([一二三四五六七八九十]+)节", line)
            if chinese:
                number = _chinese_number(chinese.group(1))
                if number:
                    periods.add(number)
    times = re.findall(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", value)
    start_time = end_time = None
    if len(times) >= 2:
        start_time = f"{int(times[0][0]):02d}:{times[0][1]}"
        end_time = f"{int(times[1][0]):02d}:{times[1][1]}"
    return sorted(periods), start_time, end_time


def _chinese_number(value: str) -> int | None:
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if value == "十":
        return 10
    if len(value) == 1:
        return digits.get(value)
    if value.startswith("十"):
        return 10 + digits.get(value[-1], 0)
    if value.endswith("十"):
        return digits.get(value[0], 0) * 10
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left, 0) * 10 + digits.get(right, 0)
    return None


def parse_week_set(expression: str, week_count: int) -> tuple[list[int], list[str]]:
    """解析离散周、区间和单双周，超出学期周数时返回可见警告。"""
    value = expression.strip().replace("—", "-").replace("–", "-").replace("~", "-").replace("至", "-")
    parity = None
    if "单周" in value or re.search(r"周\s*[（(]?\s*单", value):
        parity = "odd"
    elif "双周" in value or re.search(r"周\s*[（(]?\s*双", value):
        parity = "even"
    match = re.search(r"((?:\d+\s*(?:-\s*\d+)?\s*[,，、]?\s*)+)周", value)
    if not match:
        if parity:
            weeks = [week for week in range(1, week_count + 1) if (week % 2 == 1) == (parity == "odd")]
            return weeks, []
        return [], ["未识别周集合；请人工修正。"]
    weeks: set[int] = set()
    for token in re.split(r"[,，、]", match.group(1)):
        numbers = [int(item) for item in re.findall(r"\d+", token)]
        if not numbers:
            continue
        if len(numbers) == 1:
            weeks.add(numbers[0])
        else:
            if numbers[0] > numbers[1]:
                return [], ["周数区间的起点晚于终点；请人工修正。"]
            weeks.update(range(numbers[0], numbers[1] + 1))
    if parity == "odd":
        weeks = {week for week in weeks if week % 2 == 1}
    elif parity == "even":
        weeks = {week for week in weeks if week % 2 == 0}
    warnings = []
    if any(week < 1 or week > week_count for week in weeks):
        warnings.append("周数超出已设置学期周数；请确认学期配置。")
    weeks = {week for week in weeks if 1 <= week <= week_count}
    return sorted(weeks), warnings


def parse_course_text(raw_text: str, week_count: int) -> dict[str, Any]:
    """提取课程名、教师、地点和准确周集合，同时保留原始单元格文本。"""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    # 教务导出可能把教师接在课程名后，或把周次、地点、时刻写在同一行，不能先拼平换行。
    first = lines[0] if lines else "未识别课程"
    first_parts = re.split(r"[;；]", first, maxsplit=1)
    title = first_parts[0].strip()
    attached_teacher = re.fullmatch(r"(.+?[）)])\s*([\u4e00-\u9fff·]{2,20})", title)
    teacher_prefix = ""
    if attached_teacher:
        title, teacher_prefix = attached_teacher.groups()
    body = "\n".join(filter(None, [teacher_prefix, first_parts[1] if len(first_parts) > 1 else "", *lines[1:]]))
    week_expression_pattern = re.compile(
        r"(?:\d+\s*(?:[-–—~至]\s*\d+)?(?:\s*[,，、]\s*\d+\s*(?:[-–—~至]\s*\d+)?)*\s*周(?:\s*[（(]?[单双]周?[）)]?)?|单周|双周)"
    )
    week_match = week_expression_pattern.search(body)
    warnings: list[str] = []
    if week_match is None:
        week_expression = ""
        week_set: list[int] = []
        warnings.append("未识别周数表达；原文已保留，需人工修正。")
        before = body
        after = ""
    else:
        week_expression = week_match.group(0).strip()
        week_set, week_warnings = parse_week_set(week_expression, week_count)
        warnings.extend(week_warnings)
        before = body[:week_match.start()]
        after = body[week_match.end():]
    # 只识别完整的时刻区间，房间号和周数都不能被当作时间；不把时刻附加到地点。
    clock = r"(?:[01]?\d|2[0-3])[:：][0-5]\d"
    time_match = re.search(rf"(?<!\d)({clock})\s*[-—–~～至]\s*({clock})(?!\d)", body)
    start_time = end_time = None
    if time_match:
        values = [f"{int(value.replace('：', ':').split(':')[0]):02d}:{value[-2:]}" for value in time_match.groups()]
        start_time, end_time = values
        if end_time <= start_time:
            warnings.append("原文课程结束时间不晚于开始时间，请核对真实起止时间。")
            start_time = end_time = None
        after = re.sub(rf"(?<!\d){clock}\s*[-—–~～至]\s*{clock}(?!\d)", "", after)
    teachers: list[str] = []
    for field in re.split(r"[;；\n]", before):
        cleaned = re.sub(r"^(教师|老师|任课教师)\s*[:：]\s*", "", field)
        for teacher in re.split(r"[,，、/、]+", cleaned):
            teacher = teacher.strip()
            if teacher and teacher not in teachers:
                teachers.append(teacher)
    room_fields = [field.strip(" ;；\n") for field in re.split(r"[;；\n]", after)]
    room = next((re.sub(r"^(地点|教室)\s*[:：]\s*", "", field) for field in room_fields if field), None)
    if not teachers:
        warnings.append("未识别教师字段。")
    return {
        "title": title[:500],
        "display_name": title[:500],
        "teachers": teachers,
        "room": room,
        "start_time": start_time,
        "end_time": end_time,
        "week_expression": week_expression,
        "week_set": week_set,
        "raw_text": raw_text[:20_000],
        "warnings": warnings,
    }


def parse_grid(
    grid: GridWorkbook,
    *,
    week_count: int = 16,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """解析课程行与合并区域；合并单元格只处理左上角一次。"""
    rows = grid.rows
    if not rows or sum(len(row) for row in rows) > MAX_CELLS:
        raise TimetableParseError("工作表内容超过导入上限。")
    header_row, weekday_columns = _weekday_map(rows)
    term_code = next(
        (match.group(0) for row in rows for value in row if (match := _TERM_PATTERN.search(value))),
        None,
    )
    period_column = min(weekday_columns)
    period_scores: list[tuple[int, int]] = []
    for col in range(period_column):
        score = sum(
            bool(_parse_period_label(row[col])[0] or _parse_period_label(row[col])[1])
            for row in rows[header_row + 1 :]
            if col < len(row) and row[col]
        )
        period_scores.append((score, col))
    if period_scores:
        period_column = max(period_scores, key=lambda item: (item[0], -item[1]))[1]

    period_rows: dict[int, dict[str, Any]] = {}
    period_profile: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row_index in range(header_row + 1, len(rows)):
        label = rows[row_index][period_column] if period_column < len(rows[row_index]) else ""
        periods, start_time, end_time = _parse_period_label(label)
        if not periods:
            continue
        group = {
            "periods": periods,
            "start_time": start_time,
            "end_time": end_time,
            "source_label": label[:300],
        }
        period_profile.append(group)
        for period in periods:
            period_rows[period] = group
        if not start_time or not end_time:
            warnings.append(f"节次列第 {row_index + 1} 行未找到明确时刻，忙碌时段需手工配置。")
    if not period_profile:
        warnings.append("未识别节次与时间表；请在导入预览中补充作息设置。")

    covered: set[tuple[int, int]] = set()
    merged_by_anchor: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for r0, r1, c0, c1 in grid.merged_ranges:
        merged_by_anchor[(r0, c0)] = (r0, r1, c0, c1)
        for row_index in range(r0, r1):
            for col_index in range(c0, c1):
                if (row_index, col_index) != (r0, c0):
                    covered.add((row_index, col_index))

    courses: list[dict[str, Any]] = []
    unrecognized: list[dict[str, Any]] = []
    for row_index in range(header_row + 1, len(rows)):
        row = rows[row_index]
        for col_index, weekday in weekday_columns.items():
            if col_index >= len(row) or not row[col_index] or (row_index, col_index) in covered:
                continue
            r0, r1, c0, c1 = merged_by_anchor.get(
                (row_index, col_index), (row_index, row_index + 1, col_index, col_index + 1)
            )
            periods = sorted(
                {
                    period
                    for covered_row in range(r0, r1)
                    for period in (
                        _parse_period_label(
                            rows[covered_row][period_column]
                            if period_column < len(rows[covered_row])
                            else ""
                        )[0]
                    )
                }
            )
            if not periods:
                unrecognized.append({"cell": f"r{row_index + 1}c{col_index + 1}", "reason": "课程单元格无法关联到节次行。"})
                continue
            parsed = parse_course_text(row[col_index], week_count)
            for target_col in range(c0, c1):
                target_weekday = weekday_columns.get(target_col)
                if target_weekday is None:
                    continue
                key = f"{parsed['title'].casefold()}|{target_weekday}|{','.join(map(str, periods))}"
                courses.append(
                    {
                        **parsed,
                        "weekday": target_weekday,
                        "periods": periods,
                        "identity_key": key,
                        "source_cell": f"r{row_index + 1}c{col_index + 1}",
                    }
                )
                unrecognized.extend(
                    {"cell": f"r{row_index + 1}c{col_index + 1}", "reason": item}
                    for item in parsed["warnings"]
                )

    if not term_code:
        warnings.append("未识别学期代码；导入前需手工补充。")
    if not courses:
        warnings.append("没有识别到课程起始单元格；请检查星期表头和单元格结构。")
    return {
        "schema_version": "1.0",
        "field_parser_version": 2,
        "filename": grid.filename,
        "sheet_name": grid.sheet_name,
        "term_code": term_code,
        "week_count": week_count,
        "timezone": timezone_name,
        "weekday_columns": {str(col): weekday for col, weekday in weekday_columns.items()},
        "period_profile": period_profile,
        "courses": courses,
        "unrecognized": unrecognized,
        "warnings": warnings,
        "metrics": {
            "course_start_count": len(courses),
            "merged_range_count": len(grid.merged_ranges),
            "weekday_count": len(set(weekday_columns.values())),
            "sunday_course_count": sum(1 for course in courses if course["weekday"] == 7),
        },
    }


def parse_timetable_file(
    data: bytes,
    filename: str,
    *,
    week_count: int = 16,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    grid = read_workbook(data, filename)
    return parse_grid(grid, week_count=week_count, timezone_name=timezone_name)
