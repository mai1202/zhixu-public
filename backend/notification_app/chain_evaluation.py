"""通知链配对评测：严格分组、冻结输入，工程、模型和人审分开计数。"""

from collections import Counter
import hashlib
import json


def validate_dataset(dataset):
    """同活动的文本、截图和转发副本不能跨开发与测试集。"""
    activities, identifiers = {}, set()
    for chain in dataset["chains"]:
        if chain["id"] in identifiers: raise ValueError("通知链ID重复")
        identifiers.add(chain["id"])
        activity, split = chain["activity_id"], chain["split"]
        if split not in {"dev", "test"}: raise ValueError("仅允许dev或test分组")
        if activity in activities and activities[activity] != split: raise ValueError("同一活动跨分组")
        activities[activity] = split
        if chain["origin"] not in {"synthetic", "public", "authorized"}: raise ValueError("缺少来源类型")
        if chain["origin"] != "synthetic" and not chain.get("provenance"):
            raise ValueError("真实资料必须保留来源或授权记录")
        step_ids = [step["id"] for step in chain["steps"]]
        if len(step_ids) != len(set(step_ids)): raise ValueError("链内步骤ID重复")
        candidate_ids = [item.get("id", "primary") for item in [chain["initial"], *chain.get("other_initial", [])]]
        if len(candidate_ids) != len(set(candidate_ids)): raise ValueError("同名事项候选必须有不同标识")
        for step in chain["steps"]:
            if step.get("control_target") and step["control_target"] not in candidate_ids:
                raise ValueError("控制目标不在初始候选中")
    return hashlib.sha256(json.dumps(dataset, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def evaluate(dataset, predictions, *, split="test"):
    """未运行和未核验不记作零错误或100%通过。"""
    fingerprint = validate_dataset(dataset)
    if predictions.get("dataset_sha256") != fingerprint: raise ValueError("预测与冻结样本不一致")
    rows = {(row["chain_id"], row["step_id"]): row for row in predictions.get("records", [])}
    if len(rows) != len(predictions.get("records", [])): raise ValueError("预测记录重复")
    counts, failures = Counter(), []
    fields = {"deadline", "materials", "submission", "status", "target", "states"}
    for chain in dataset["chains"]:
        if chain["split"] != split: continue
        counts["chains"] += 1
        counts[f"origin_{chain['origin']}"] += 1
        if len(set(chain.get("reviewers", []))) >= 2: counts["double_reviewed_chains"] += 1
        for step in chain["steps"]:
            counts["steps"] += 1
            row = rows.get((chain["id"], step["id"]))
            if row is None:
                counts["not_run"] += 1
                continue
            counts["ran"] += 1
            expected, observed = step["expected"], row["observed"]
            checked = {key for key in expected if key in fields}
            wrong = [key for key in checked if expected[key] != observed.get(key)]
            counts["fields_checked"] += len(checked)
            counts["fields_correct"] += len(checked) - len(wrong)
            if "target" in expected:
                counts["target_checked"] += 1
                counts["target_correct"] += int(expected["target"] == observed.get("target"))
            counts["final_state_correct"] += int(not wrong)
            counts["needs_review"] += int(row.get("status") == "needs_review")
            counts["automatic_applied"] += int(row.get("status") == "applied")
            counts["verified_noop"] += int(row.get("status") == "noop")
            counts["wrong_automatic"] += int(bool(wrong) and row.get("status") == "applied")
            if wrong: failures.append({"chain_id": chain["id"], "step_id": step["id"], "fields": wrong})
    return {"dataset_sha256": fingerprint, "split": split, "counts": dict(counts),
        "field_accuracy": counts["fields_correct"] / counts["fields_checked"] if counts["fields_checked"] else None,
        "state_accuracy": counts["final_state_correct"] / counts["ran"] if counts["ran"] else None,
        "target_accuracy": counts["target_correct"] / counts["target_checked"] if counts["target_checked"] else None,
        "automatic_coverage": (counts["automatic_applied"] + counts["verified_noop"]) / counts["ran"] if counts["ran"] else None,
        "failures": failures, "layer": predictions.get("layer", "unknown"),
        "manual": "ran" if counts["chains"] and counts["double_reviewed_chains"] == counts["chains"] else "not_run"}
