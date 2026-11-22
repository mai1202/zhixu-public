"""依赖压力与容量下界：复用排程输入，给出确定性排序和必要条件证据。"""

from datetime import timedelta


def available_minutes(windows, earliest, deadline):
    """只累计真实空闲区间；调用方先合并窗口，避免重叠容量重复计算。"""
    return sum(max(0, int((min(right, deadline) - max(left, earliest)).total_seconds() // 60))
        for left, right in windows)


def dependency_pressure(units, done):
    """逆拓扑传播最紧后继截止与剩余关键路径，不把分叉工作量错误相加。"""
    by_id = {unit["id"]: unit for unit in units}
    children = {identifier: [] for identifier in by_id}
    for unit in units:
        for parent in unit["depends_on"]:
            if parent in children:
                children[parent].append(unit["id"])
    visiting, profiles, invalid = set(), {}, set()

    def visit(identifier):
        if identifier in profiles:
            return profiles[identifier]
        if identifier in visiting:
            invalid.update(visiting)
            return None
        visiting.add(identifier)
        unit = by_id[identifier]
        missing = [p for p in unit["depends_on"] if p not in by_id and p not in done]
        if missing:
            invalid.add(identifier)
        latest = unit["deadline"] - timedelta(minutes=unit["minutes"])
        path = unit["minutes"]
        for child in sorted(children[identifier]):
            profile = visit(child)
            if profile is not None:
                latest = min(latest, profile["latest_start"] - timedelta(minutes=unit["minutes"]))
                path = max(path, unit["minutes"] + profile["path_minutes"])
        visiting.remove(identifier)
        profiles[identifier] = {"latest_start": latest, "path_minutes": path, "missing": missing}
        return profiles[identifier]

    for identifier in sorted(by_id):
        visit(identifier)
    return profiles, invalid


def capacity_certificates(units, windows, done):
    """用需求区间下界证明部分无解原因；未发现违反必要条件不表示一定可行。"""
    profiles, invalid = dependency_pressure(units, done)
    certificates = []
    if invalid:
        certificates.append({"kind": "dependency_invalid", "unit_ids": sorted(invalid),
            "proven": True, "reason": "依赖存在环或缺失前置步骤，当前输入不能完整安排。"})
    for unit in units:
        # 整段个人安排还受最大连续空闲长度约束，不能只看分钟总和。
        largest = max((max(0, int((min(right, unit["deadline"]) - max(left, unit["earliest"])).total_seconds() // 60))
            for left, right in windows), default=0)
        if unit["deadline"] <= unit["earliest"]:
            certificates.append({"kind": "empty_window", "proven": True, "unit_ids": [unit["id"]],
                "reason": f"{unit['title']}的最早开始不早于截止，当前没有可用执行区间。"})
        elif not unit["split"] and largest < unit["minutes"]:
            certificates.append({"kind": "fragmentation", "proven": True, "unit_ids": [unit["id"]],
                "required_minutes": unit["minutes"], "largest_window_minutes": largest,
                "reason": f"{unit['title']}需要连续{unit['minutes']}分钟，最长空闲只有{largest}分钟。"})
    # 任意 [release, deadline] 内必须执行的任务总耗时不能大于同区间空闲容量。
    # 最多100个路线步骤加100项个人安排；仅输出最严重的五个区间下界，避免刷屏。
    overloads = []
    for left in sorted({unit["earliest"] for unit in units}):
        for right in sorted({unit["deadline"] for unit in units}):
            if right <= left:
                continue
            contained = [u for u in units if u["earliest"] >= left and u["deadline"] <= right]
            demand = sum(unit["minutes"] for unit in contained)
            capacity = available_minutes(windows, left, right)
            if demand > capacity:
                overloads.append({"kind": "capacity_overload", "proven": True,
                    "start_at": left.isoformat(), "end_at": right.isoformat(),
                    "required_minutes": demand, "available_minutes": capacity, "deficit_minutes": demand - capacity,
                    "unit_ids": sorted(u["id"] for u in contained),
                    "reason": f"该区间至少需要{demand}分钟，只有{capacity}分钟可用，至少缺少{demand - capacity}分钟。"})
    overloads.sort(key=lambda c: (-c["deficit_minutes"], len(c["unit_ids"]), c["start_at"], c["end_at"]))
    seen = set()
    for certificate in overloads:
        group = tuple(certificate["unit_ids"])
        if group not in seen:
            certificates.append(certificate)
            seen.add(group)
            if len(seen) >= 5:
                break
    return profiles, certificates
