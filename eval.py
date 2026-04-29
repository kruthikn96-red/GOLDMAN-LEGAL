"""Evaluate extracted executable rules against hand-authored ground truth."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from schema import Document

GROUND_TRUTH_DIR = Path("ground_truth")
OUTPUT_DIR = Path("output")
REPORT_PATH = OUTPUT_DIR / "eval_report.json"


def canonical(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("op") in {"and", "or"} and isinstance(value.get("args"), list):
            rest = {k: canonical(v) for k, v in value.items() if k != "args"}
            rest["args"] = sorted(
                (canonical(arg) for arg in value["args"]),
                key=lambda item: json.dumps(item, sort_keys=True),
            )
            return rest
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def exact_equal(a: Any, b: Any) -> bool:
    return canonical(a) == canonical(b)


def load_doc(path: Path) -> Document:
    return Document.model_validate(json.loads(path.read_text(encoding="utf-8")))


def flatten_rule(rule) -> dict[str, Any]:
    data = rule.model_dump(mode="json", exclude_none=True)
    body = data.pop("body")
    data["scope"] = body.get("scope")
    data["body"] = body
    return data


def compare_docs(gold: Document, pred: Document) -> dict[str, Any]:
    gold_rules = {r.rule_id: r for r in gold.rules}
    pred_rules = {r.rule_id: r for r in pred.rules}

    gold_ids = set(gold_rules)
    pred_ids = set(pred_rules)
    matched_ids = sorted(gold_ids & pred_ids)

    precision = len(matched_ids) / len(pred_ids) if pred_ids else 0.0
    recall = len(matched_ids) / len(gold_ids) if gold_ids else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    field_counts = Counter()
    field_correct = Counter()
    mismatches = []
    scope_counts = defaultdict(lambda: Counter({"gold": 0, "pred": 0, "matched": 0}))

    for rule in gold.rules:
        scope_counts[rule.body.scope]["gold"] += 1
    for rule in pred.rules:
        scope_counts[rule.body.scope]["pred"] += 1

    for rule_id in matched_ids:
        g = flatten_rule(gold_rules[rule_id])
        p = flatten_rule(pred_rules[rule_id])
        scope_counts[g["scope"]]["matched"] += 1
        checks = {
            "scope": (g["scope"], p["scope"]),
            "body": (g["body"], p["body"]),
            "unmapped_attributes": (
                sorted(g.get("unmapped_attributes", [])),
                sorted(p.get("unmapped_attributes", [])),
            ),
        }
        for field, (gv, pv) in checks.items():
            field_counts[field] += 1
            if exact_equal(gv, pv):
                field_correct[field] += 1
            else:
                mismatches.append({"rule_id": rule_id, "field": field, "gold": gv, "pred": pv})

    field_accuracy = {
        field: field_correct[field] / count for field, count in field_counts.items()
    }
    critical_total = field_counts["scope"] + field_counts["body"]
    critical_correct = field_correct["scope"] + field_correct["body"]
    critical_accuracy = critical_correct / critical_total if critical_total else 0.0

    by_scope = {}
    for scope, counts in scope_counts.items():
        sp = counts["matched"] / counts["pred"] if counts["pred"] else 0.0
        sr = counts["matched"] / counts["gold"] if counts["gold"] else 0.0
        by_scope[scope] = {
            "gold": counts["gold"],
            "pred": counts["pred"],
            "matched": counts["matched"],
            "precision": sp,
            "recall": sr,
        }

    return {
        "document_id": gold.document_id,
        "rule_level": {
            "gold": len(gold_ids),
            "pred": len(pred_ids),
            "matched": len(matched_ids),
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "missing": sorted(gold_ids - pred_ids),
            "extra": sorted(pred_ids - gold_ids),
        },
        "field_accuracy": field_accuracy,
        "critical_field_accuracy": critical_accuracy,
        "by_scope": by_scope,
        "mismatches": mismatches,
    }


def main() -> None:
    reports = []
    totals = Counter()
    critical_weighted = []

    for gold_path in sorted(GROUND_TRUTH_DIR.glob("*.json")):
        pred_path = OUTPUT_DIR / gold_path.name
        if not pred_path.exists():
            reports.append({"document_id": gold_path.stem, "error": f"{pred_path} missing"})
            continue
        report = compare_docs(load_doc(gold_path), load_doc(pred_path))
        reports.append(report)
        rl = report["rule_level"]
        totals["gold"] += rl["gold"]
        totals["pred"] += rl["pred"]
        totals["matched"] += rl["matched"]
        critical_weighted.append(report["critical_field_accuracy"])

    precision = totals["matched"] / totals["pred"] if totals["pred"] else 0.0
    recall = totals["matched"] / totals["gold"] if totals["gold"] else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    summary = {
        "rule_level": {
            "gold": totals["gold"],
            "pred": totals["pred"],
            "matched": totals["matched"],
            "precision": precision,
            "recall": recall,
            "f1": f1,
        },
        "critical_field_accuracy_avg": (
            sum(critical_weighted) / len(critical_weighted) if critical_weighted else 0.0
        ),
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps({"summary": summary, "documents": reports}, indent=2),
        encoding="utf-8",
    )

    print("Rule-level")
    print(
        f"  P={precision:.3f} R={recall:.3f} F1={f1:.3f} "
        f"({totals['matched']}/{totals['gold']} matched)"
    )
    print(f"Critical-field accuracy avg: {summary['critical_field_accuracy_avg']:.3f}")
    for report in reports:
        if "error" in report:
            print(f"  {report['document_id']}: {report['error']}")
            continue
        rl = report["rule_level"]
        print(
            f"  {report['document_id']}: P={rl['precision']:.3f} "
            f"R={rl['recall']:.3f} F1={rl['f1']:.3f}"
        )
    print(f"Wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
