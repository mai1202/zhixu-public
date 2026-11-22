"""在字段自己的引文内核对日期，不把全文命中误当作字段证据。"""

from datetime import date, time
import re


# 仅接受原文完整年月日；不借当前年份补全，也不自动纠正 OCR 数字。
DATE = re.compile(r"(?<!\d)(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})(?:日|号)?(?!\d)")
CLAUSE = re.compile(r"[^，,；;。！？!?\n]+")
CLOCK = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?:[:：]([0-5]\d))?(?!\d)")
REVISION = re.compile(r"(?:延期|延长|推迟|调整|改为|更正|变更).{0,12}$")


def check_deadline(value, evidence, identity):
    """返回可审阅的规则诊断；supported 只代表局部一致，不代表语义已证明。"""
    result = {"method": "local-date-evidence-v1", "status": "unknown", "reason": "引文未提供完整日期。",
        "dates": [], "anchors": [], "scope_checked": False}
    if not value or not value.get("local_date"):
        return result
    target = value["local_date"]
    parts = []
    for ref in evidence:
        for clause in CLAUSE.finditer(ref["quote"]):
            matches = []
            for match in DATE.finditer(clause.group()):
                try:
                    found = date(*(int(item) for item in match.groups())).isoformat()
                except ValueError:
                    continue
                matches.append((found, match))
            if matches:
                parts.append((ref["segment_id"], clause.group(), matches))
    result["dates"] = sorted({found for _, _, matches in parts for found, _ in matches})
    if target not in result["dates"]:
        result.update(status="unsupported", reason="结构化日期未出现在该字段引文中，不能借用通知其他位置的日期。")
        return result
    multiple = len(result["dates"]) > 1
    for segment_id, text, matches in parts:
        selected = [(found, match) for found, match in matches if found == target]
        if not selected:
            continue
        # 多期限时要求主体与阶段在同一分句精确出现；别名或跨句继承留给人工核对。
        scoped = all(identity.get(key) and identity[key] in text for key in ("actor", "stage"))
        distinct = {found for found, _ in matches}
        correction = len(distinct) > 1 and matches[-1][0] == target and bool(
            REVISION.search(text[matches[-2][1].end():matches[-1][1].start()]))
        if len(distinct) > 1 and not correction:
            continue
        if multiple and not scoped and not correction:
            continue
        for _, match in selected:
            next_date = next((other.start() for _, other in matches if other.start() > match.start()), len(text))
            if value.get("local_time"):
                # 日期和时刻必须在同一个分句且在下一日期之前，防止拼接别人的时刻。
                clocks = {time(int(c[1]), int(c[2]), int(c[3] or 0)) for c in CLOCK.finditer(text[match.end():next_date])}
                if clocks != {time.fromisoformat(value["local_time"])}:
                    continue
            result["anchors"].append({"segment_id": segment_id, "quote": text, "date": target})
        if result["anchors"]:
            result.update(status="supported", reason="字段引文中的完整日期与结构化值一致。", scope_checked=scoped)
    if not result["anchors"]:
        result["reason"] = "引文有多个期限或日期与时刻不能唯一配对，请核对主体、阶段和具体期限。"
    return result
