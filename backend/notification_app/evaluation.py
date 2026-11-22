"""只处理脱敏或合成结构化样本的研究评测指标。"""

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

REQUIRED_FIELDS = ("subject", "stage", "action", "local_date", "precision")


def _item_key(item: dict[str, Any]) -> str:
    """生成稳定字段元组，避免字典键顺序影响结果。"""
    return json.dumps(
        {field: item.get(field) for field in REQUIRED_FIELDS},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def validate_activity_splits(samples: list[dict[str, Any]]) -> None:
    """同一活动/通知版本链不得同时出现在训练集与测试集。"""
    if not isinstance(samples, list) or any(not isinstance(sample, dict) for sample in samples):
        raise ValueError("samples 必须是 JSON 对象数组。")
    split_by_activity: dict[str, str] = {}
    for sample in samples:
        activity_id = sample.get("activity_id")
        split = sample.get("split")
        if not isinstance(activity_id, str) or not activity_id.strip():
            raise ValueError("每条评测样本都必须提供脱敏 activity_id。")
        if not isinstance(split, str) or not split.strip():
            raise ValueError("每条评测样本都必须提供 split。")
        # 规范化只用于分组校验，防止空白字符绕过版本链隔离。
        previous = split_by_activity.setdefault(activity_id.strip(), split.strip())
        if previous != split.strip():
            raise ValueError("同一 activity_id 跨越了多个数据集，存在版本链泄漏。")


def evaluate_records(
    samples: list[dict[str, Any]],
    *,
    split: str = "test",
    dataset_bytes: bytes | None = None,
) -> dict[str, Any]:
    """报告结构化事项、联合字段、证据与人工成本的原始计数和比例。"""
    validate_activity_splits(samples)
    selected = [item for item in samples if item["split"] == split]
    if not selected:
        raise ValueError(f"没有 split={split!r} 的样本。")
    gold_count = 0
    predicted_count = 0
    true_positive = 0
    exact_samples = 0
    evidence_total = 0
    evidence_valid = 0
    abstained = 0
    latencies: list[float] = []
    costs: list[float] = []
    human_corrections: list[float] = []
    human_minutes: list[float] = []

    for sample in selected:
        gold = sample.get("gold")
        predicted = sample.get("predicted")
        if not isinstance(gold, list) or not isinstance(predicted, list):
            raise ValueError("gold 和 predicted 必须是结构化事项数组。")
        if any(not isinstance(item, dict) for item in (*gold, *predicted)):
            raise ValueError("gold 与 predicted 的每个事项都必须是 JSON 对象。")
        for item in (*gold, *predicted):
            if any(field not in item for field in REQUIRED_FIELDS):
                raise ValueError("每个事项必须包含 subject/stage/action/local_date/precision。")
        gold_keys = Counter(_item_key(item) for item in gold)
        predicted_keys = Counter(_item_key(item) for item in predicted)
        matched = sum((gold_keys & predicted_keys).values())
        gold_count += len(gold)
        predicted_count += len(predicted)
        true_positive += matched
        if gold_keys == predicted_keys:
            exact_samples += 1
        if not predicted:
            abstained += 1
        for item in predicted:
            if isinstance(item.get("evidence_valid"), bool):
                evidence_total += 1
                evidence_valid += int(item["evidence_valid"])
        for field, target in (
            ("latency_ms", latencies),
            ("cost_usd", costs),
            ("human_corrections", human_corrections),
            ("human_minutes", human_minutes),
        ):
            value = sample.get(field)
            # bool 属于 int 的子类；无穷和 NaN 也不能进入研究统计或 JSON 输出。
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
            ):
                raise ValueError(f"{field} 必须是非负有限数字或 null。")
            if value is not None:
                target.append(float(value))

    precision = true_positive / predicted_count if predicted_count else 0.0
    recall = true_positive / gold_count if gold_count else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    activity_joint = sum(
        Counter(_item_key(item) for item in sample["gold"])
        == Counter(_item_key(item) for item in sample["predicted"])
        for sample in selected
    )
    return {
        "schema_version": 1,
        "split": split,
        "sample_count": len(selected),
        "dataset_sha256": hashlib.sha256(dataset_bytes or json.dumps(samples, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "items": {
            "gold": gold_count,
            "predicted": predicted_count,
            "true_positive": true_positive,
            "false_additions": predicted_count - true_positive,
            "missed_items": gold_count - true_positive,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "joint_record_accuracy": true_positive / gold_count if gold_count else 0.0,
        },
        "activities": {
            "exact": activity_joint,
            "total": len(selected),
            "exact_accuracy": activity_joint / len(selected),
            "abstained": abstained,
        },
        "evidence": {
            "checked": evidence_total,
            "valid": evidence_valid,
            "valid_rate": evidence_valid / evidence_total if evidence_total else None,
        },
        "human_corrections_mean": mean(human_corrections) if human_corrections else None,
        "human_minutes_mean": mean(human_minutes) if human_minutes else None,
        "latency_ms_mean": mean(latencies) if latencies else None,
        "cost_usd_mean": mean(costs) if costs else None,
        "measurement_counts": {
            "latency_ms": len(latencies), "cost_usd": len(costs),
            "human_corrections": len(human_corrections), "human_minutes": len(human_minutes),
        },
        "statistical_note": "所有原始计数均保留；小样本结果只描述该样本集。",
    }


def evaluate_file(path: str | Path, *, split: str = "test") -> dict[str, Any]:
    """读取 UTF-8 JSON 评测集，输出中只带内容哈希和聚合结果。"""
    source = Path(path)
    raw = source.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("samples"), list):
        raise ValueError("评测文件必须包含 samples 数组。")
    return evaluate_records(document["samples"], split=split, dataset_bytes=raw)


def compare_records(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]], *, split: str = "test",
) -> dict[str, Any]:
    """在完全相同的样本与金标上做配对比较，拒绝不同测试集间的效果宣传。"""
    baseline_report = evaluate_records(baseline, split=split)
    candidate_report = evaluate_records(candidate, split=split)

    def indexed(samples):
        result = {}
        for sample in samples:
            if sample["split"] != split:
                continue
            # 一个活动可有多版通知，比较时必须显式提供唯一 sample_id。
            sample_id = sample.get("sample_id")
            if not isinstance(sample_id, str) or not sample_id.strip():
                raise ValueError("配对比较每条样本都需要唯一 sample_id。")
            key = sample_id.strip()
            if key in result:
                raise ValueError("配对比较存在重复 sample_id。")
            result[key] = sample
        return result

    left, right = indexed(baseline), indexed(candidate)
    if left.keys() != right.keys():
        raise ValueError("基线与候选的测试 sample_id 必须完全一致。")
    transitions = {"both_exact": 0, "improved": 0, "regressed": 0, "both_inexact": 0}
    for sample_id, previous in left.items():
        current = right[sample_id]
        gold = Counter(_item_key(item) for item in previous["gold"])
        if previous["activity_id"].strip() != current["activity_id"].strip() or gold != Counter(
            _item_key(item) for item in current["gold"]
        ):
            raise ValueError("配对样本的 activity_id 与金标必须一致。")
        was_exact = Counter(_item_key(item) for item in previous["predicted"]) == gold
        is_exact = Counter(_item_key(item) for item in current["predicted"]) == gold
        label = "both_exact" if was_exact and is_exact else (
            "regressed" if was_exact else "improved" if is_exact else "both_inexact"
        )
        transitions[label] += 1
    return {
        "schema_version": 1, "split": split, "paired_sample_count": len(left),
        "baseline": baseline_report, "candidate": candidate_report,
        "exact_transitions": transitions,
        "delta": {
            "precision": candidate_report["items"]["precision"] - baseline_report["items"]["precision"],
            "recall": candidate_report["items"]["recall"] - baseline_report["items"]["recall"],
            "f1": candidate_report["items"]["f1"] - baseline_report["items"]["f1"],
            "exact_accuracy": candidate_report["activities"]["exact_accuracy"] - baseline_report["activities"]["exact_accuracy"],
        },
        "statistical_note": "差值仅描述同一批配对样本，不构成显著性、因果或真实用户收益证明；缺失测量不按零计算。",
    }


def compare_files(baseline_path: str | Path, candidate_path: str | Path, *, split: str = "test") -> dict[str, Any]:
    """配对报告只输出聚合结果与输入哈希，不泄露样本 ID、正文和金标。"""
    raw_baseline = Path(baseline_path).read_bytes()
    raw_candidate = Path(candidate_path).read_bytes()
    documents = [json.loads(raw.decode("utf-8")) for raw in (raw_baseline, raw_candidate)]
    if any(not isinstance(doc, dict) or not isinstance(doc.get("samples"), list) for doc in documents):
        raise ValueError("评测文件必须包含 samples 数组。")
    result = compare_records(documents[0]["samples"], documents[1]["samples"], split=split)
    result["baseline"]["dataset_sha256"] = hashlib.sha256(raw_baseline).hexdigest()
    result["candidate"]["dataset_sha256"] = hashlib.sha256(raw_candidate).hexdigest()
    return result
