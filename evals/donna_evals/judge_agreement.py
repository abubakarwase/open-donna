"""Judge–human (or provisional) agreement metrics for layer-2 rubrics.

Intended-quality → expected score bands (provisional until human_grades filled):

| intended_quality | expected scores                         | invented_fact |
|------------------|-----------------------------------------|---------------|
| model_plain      | all criteria = 5                        | false         |
| good             | alias of model_plain                    | false         |
| vague            | coverage/clarity = 1; others = 3        | false         |
| wrong_tone       | tone = 1; others = 3                    | false         |
| invented_fact    | precision = 1; others = 3               | true          |
| missing_ask      | completeness/coverage = 1; others = 3   | false         |

Within-±1 agreement: |judge - ref| <= 1 on a criterion counts as agree.
"""

from __future__ import annotations

from typing import Any

# Tiny mapping table: intended_quality → (per-criterion defaults, invented_fact).
# Criteria not listed fall back to "others" value in each row.
_QUALITY_MAP: dict[str, dict[str, Any]] = {
    "model_plain": {"_all": 5, "invented_fact": False},
    "good": {"_all": 5, "invented_fact": False},  # alias
    "vague": {"coverage": 1, "clarity": 1, "_others": 3, "invented_fact": False},
    "wrong_tone": {"tone": 1, "_others": 3, "invented_fact": False},
    "invented_fact": {"precision": 1, "_others": 3, "invented_fact": True},
    "missing_ask": {"completeness": 1, "coverage": 1, "_others": 3, "invented_fact": False},
}


def intended_to_grades(
    intended_quality: str,
    criteria: list[str],
) -> dict[str, Any]:
    """Map an intended_quality label to provisional numeric grades + invented_fact."""
    spec = _QUALITY_MAP.get(intended_quality)
    if spec is None:
        raise KeyError(f"unknown intended_quality: {intended_quality!r}")
    grades: dict[str, Any] = {}
    if "_all" in spec:
        for name in criteria:
            grades[name] = spec["_all"]
    else:
        default = spec.get("_others", 3)
        for name in criteria:
            grades[name] = spec.get(name, default)
    grades["invented_fact"] = bool(spec["invented_fact"])
    return grades


def _criterion_names(row: dict[str, Any], grades: dict[str, Any]) -> list[str]:
    names = row.get("criteria")
    if isinstance(names, list) and names:
        return [str(n) for n in names]
    return [k for k in grades if k != "invented_fact" and isinstance(grades[k], (int, float))]


def _ref_grades(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (reference grades, provisional flag)."""
    human = row.get("human_grades")
    judge = row.get("judge") or row.get("judge_grades") or {}
    if human is not None:
        return human, False
    intended = row.get("intended_quality")
    if intended is None:
        raise ValueError("row needs human_grades or intended_quality")
    criteria = _criterion_names(row, judge if isinstance(judge, dict) else {})
    if not criteria:
        # Fall back to mapping keys that look like criteria in intended map.
        criteria = [
            k
            for k, v in intended_to_grades(str(intended), ["coverage", "precision", "completeness", "tone", "clarity"]).items()
            if k != "invented_fact"
        ]
        # Prefer judge keys when present.
        if isinstance(judge, dict):
            jcrit = [k for k in judge if k != "invented_fact"]
            if jcrit:
                criteria = jcrit
    return intended_to_grades(str(intended), criteria), True


def agreement(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute per-criterion within-±1 rates and invented_fact precision/recall.

    Each row: {"judge": {crit: int, invented_fact: bool}, "human_grades": ...|null,
               "intended_quality": str?, "criteria": [str]?}
    """
    if not rows:
        return {
            "per_criterion": {},
            "within_pm1_overall": None,
            "invented_fact": {"precision": None, "recall": None},
            "provisional": True,
            "n": 0,
            "label": "provisional (no rows)",
        }

    per_counts: dict[str, list[int]] = {}  # name -> [agree, total]
    overall_agree = 0
    overall_total = 0
    tp = fp = fn = 0
    any_provisional = False

    for row in rows:
        judge = row.get("judge") or row.get("judge_grades")
        if not isinstance(judge, dict):
            raise ValueError("each row needs a judge/judge_grades object")
        ref, provisional = _ref_grades(row)
        any_provisional = any_provisional or provisional

        for name, jval in judge.items():
            if name == "invented_fact":
                continue
            if name not in ref:
                continue
            rval = ref[name]
            bucket = per_counts.setdefault(name, [0, 0])
            bucket[1] += 1
            overall_total += 1
            if abs(float(jval) - float(rval)) <= 1:
                bucket[0] += 1
                overall_agree += 1

        j_inv = bool(judge.get("invented_fact", False))
        r_inv = bool(ref.get("invented_fact", False))
        if j_inv and r_inv:
            tp += 1
        elif j_inv and not r_inv:
            fp += 1
        elif (not j_inv) and r_inv:
            fn += 1

    per_criterion = {
        name: (agree / total if total else None)
        for name, (agree, total) in sorted(per_counts.items())
    }
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    overall = overall_agree / overall_total if overall_total else None

    label = (
        "provisional vs intended_quality (human_grades null)"
        if any_provisional
        else "judge vs human_grades"
    )
    return {
        "per_criterion": per_criterion,
        "within_pm1_overall": overall,
        "invented_fact": {"precision": precision, "recall": recall},
        "provisional": any_provisional,
        "n": len(rows),
        "label": label,
    }


def format_summary(report: dict[str, Any]) -> str:
    """One-line printable summary; always notes provisional when set."""
    overall = report.get("within_pm1_overall")
    overall_s = f"{overall:.3f}" if isinstance(overall, float) else "n/a"
    inv = report.get("invented_fact") or {}
    p, r = inv.get("precision"), inv.get("recall")
    p_s = f"{p:.3f}" if isinstance(p, float) else "n/a"
    r_s = f"{r:.3f}" if isinstance(r, float) else "n/a"
    flag = " PROVISIONAL" if report.get("provisional") else ""
    return (
        f"agreement within±1={overall_s} invented_fact P={p_s} R={r_s} "
        f"n={report.get('n')} ({report.get('label')}){flag}"
    )
