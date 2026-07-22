"""Schema validation and deterministic scoring for compliance-review evals."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_GOLD_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "evals"
    / "fixtures"
    / "compliance_review_gold_v1.json"
)

_MISSING_METADATA_FIELDS = (
    "source_page",
    "requirement_type",
    "category",
    "weight",
    "confidence",
)
_VALID_REQUIREMENT_TYPES = {
    "shall",
    "must",
    "should",
    "submission_format",
    "evaluation_criterion",
    "mandatory_form",
}
_VALID_CATEGORIES = {
    "technical",
    "management",
    "past_performance",
    "personnel",
    "pricing",
    "administrative",
    "certification",
}
_VALID_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}


def _normalize(value: str) -> str:
    return " ".join((value or "").split()).strip().lower()


def _missing_text(entry: str | dict[str, Any]) -> str:
    if isinstance(entry, str):
        return entry
    return str(entry.get("requirement_text") or "")


def _expected_missing_metadata(entry: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(entry, str):
        return {}
    return {
        field: _canonical_metadata(field, entry.get(field))
        for field in _MISSING_METADATA_FIELDS
        if field in entry
    }


def _canonical_metadata(field: str, value: Any) -> Any:
    if field == "confidence":
        return str(value or "").upper()
    if field in {"requirement_type", "category"}:
        return str(value or "").lower()
    if field == "weight":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return ("invalid", repr(value))
        return float(value)
    if field == "source_page":
        if isinstance(value, bool) or not isinstance(value, int):
            return ("invalid", repr(value))
        return value
    return value


def _candidate_matches_metadata(
    expected: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    return all(
        _canonical_metadata(field, candidate.get(field)) == expected_value
        for field, expected_value in expected.items()
    )


def load_gold_set(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_GOLD_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    validate_gold_set(payload)
    return payload


def validate_gold_set(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not payload.get("version"):
        raise ValueError("gold set requires a version")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("gold set requires a non-empty cases array")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"case {index} is not an object")
        case_id = str(case.get("id") or "")
        if not case_id or case_id in seen:
            raise ValueError(f"case {index} has a missing/duplicate id")
        seen.add(case_id)
        if not isinstance(case.get("source_text"), str):
            raise ValueError(f"case {case_id} requires source_text")
        if not isinstance(case.get("items"), list):
            raise ValueError(f"case {case_id} requires items")
        expected = case.get("expected")
        if not isinstance(expected, dict):
            raise ValueError(f"case {case_id} requires expected")
        if not isinstance(expected.get("findings"), list):
            raise ValueError(f"case {case_id} expected.findings must be an array")
        if not isinstance(expected.get("missing_requirements"), list):
            raise ValueError(
                f"case {case_id} expected.missing_requirements must be an array"
            )
        seen_missing: set[str] = set()
        for missing_index, missing in enumerate(expected["missing_requirements"]):
            if isinstance(missing, str):
                text = missing
            elif isinstance(missing, dict):
                allowed = {"requirement_text", *_MISSING_METADATA_FIELDS}
                unknown = set(missing) - allowed
                if unknown:
                    raise ValueError(
                        f"case {case_id} missing requirement {missing_index} has "
                        f"unsupported fields: {', '.join(sorted(unknown))}"
                    )
                text = missing.get("requirement_text")
                if not isinstance(text, str):
                    raise ValueError(
                        f"case {case_id} missing requirement {missing_index} "
                        "requires requirement_text"
                    )
                requirement_type = missing.get("requirement_type")
                if (
                    requirement_type is not None
                    and requirement_type not in _VALID_REQUIREMENT_TYPES
                ):
                    raise ValueError(
                        f"case {case_id} missing requirement {missing_index} has "
                        f"invalid requirement_type {requirement_type!r}"
                    )
                category = missing.get("category")
                if category is not None and category not in _VALID_CATEGORIES:
                    raise ValueError(
                        f"case {case_id} missing requirement {missing_index} has "
                        f"invalid category {category!r}"
                    )
                confidence = missing.get("confidence")
                if confidence is not None and confidence not in _VALID_CONFIDENCE:
                    raise ValueError(
                        f"case {case_id} missing requirement {missing_index} has "
                        f"invalid confidence {confidence!r}"
                    )
                source_page = missing.get("source_page")
                if source_page is not None and (
                    isinstance(source_page, bool)
                    or not isinstance(source_page, int)
                    or source_page < 1
                ):
                    raise ValueError(
                        f"case {case_id} missing requirement {missing_index} has "
                        f"invalid source_page {source_page!r}"
                    )
                weight = missing.get("weight")
                if weight is not None and (
                    isinstance(weight, bool) or not isinstance(weight, (int, float))
                ):
                    raise ValueError(
                        f"case {case_id} missing requirement {missing_index} has "
                        f"invalid weight {weight!r}"
                    )
            else:
                raise ValueError(
                    f"case {case_id} missing requirement {missing_index} must be "
                    "a string or object"
                )
            normalized = _normalize(text)
            if not normalized or normalized in seen_missing:
                raise ValueError(
                    f"case {case_id} has an empty/duplicate missing requirement"
                )
            seen_missing.add(normalized)


def _ratio(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    return numerator / denominator if denominator else empty


def score_model_outputs(
    gold: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
) -> dict[str, float | int]:
    """Score normalized model outputs without making any provider calls."""

    validate_gold_set(gold)
    expected_findings: set[tuple[str, str, str]] = set()
    actual_findings: set[tuple[str, str, str]] = set()
    expected_missing: set[tuple[str, str]] = set()
    actual_missing: set[tuple[str, str]] = set()
    expected_missing_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    actual_missing_entries: list[tuple[tuple[str, str], dict[str, Any]]] = []
    expected_suggestions: dict[tuple[str, str, str], tuple[str | None, str | None]] = {}
    actual_suggestions: dict[tuple[str, str, str], tuple[str | None, str | None]] = {}
    high_actual: set[tuple[str, str, str]] = set()
    critical_expected: set[tuple[str, str]] = set()

    protocol_successes = 0
    total_latency = 0.0
    total_cost = 0.0
    for case in gold["cases"]:
        case_id = str(case["id"])
        expected = case["expected"]
        output = outputs.get(case_id) or {}
        if output.get("protocol_ok") is True:
            protocol_successes += 1
        total_latency += float(output.get("latency_seconds") or 0.0)
        total_cost += float(output.get("cost_usd") or 0.0)

        for finding in expected["findings"]:
            key = (
                case_id,
                str(finding.get("requirement_id") or ""),
                str(finding.get("issue") or ""),
            )
            expected_findings.add(key)
            expected_suggestions[key] = (
                finding.get("suggested_type"),
                finding.get("suggested_category"),
            )
        for finding in output.get("findings") or []:
            key = (
                case_id,
                str(finding.get("requirement_id") or ""),
                str(finding.get("issue") or ""),
            )
            actual_findings.add(key)
            actual_suggestions[key] = (
                finding.get("suggested_type"),
                finding.get("suggested_category"),
            )
            if str(finding.get("confidence") or "").upper() == "HIGH":
                high_actual.add(key)
        for missing in expected["missing_requirements"]:
            key = (case_id, _normalize(_missing_text(missing)))
            expected_missing.add(key)
            expected_missing_metadata[key] = _expected_missing_metadata(missing)
            if expected.get("critical_missing"):
                critical_expected.add(key)
        for candidate in output.get("missing_candidates") or []:
            if not isinstance(candidate, dict):
                candidate = {}
            key = (
                case_id,
                _normalize(str(candidate.get("requirement_text") or "")),
            )
            actual_missing.add(key)
            actual_missing_entries.append((key, candidate))

    finding_tp = len(expected_findings & actual_findings)
    finding_fp = len(actual_findings - expected_findings)
    finding_fn = len(expected_findings - actual_findings)
    finding_precision = _ratio(finding_tp, finding_tp + finding_fp)
    finding_recall = _ratio(finding_tp, finding_tp + finding_fn)
    finding_f1 = _ratio(
        2 * finding_precision * finding_recall,
        finding_precision + finding_recall,
        empty=0.0,
    )

    matched_findings = expected_findings & actual_findings
    suggestion_correct = sum(
        expected_suggestions[key] == actual_suggestions.get(key)
        for key in matched_findings
    )
    high_correct = sum(
        key in expected_findings
        and expected_suggestions.get(key) == actual_suggestions.get(key)
        for key in high_actual
    )
    missing_tp = len(expected_missing & actual_missing)
    missing_fp = len(actual_missing - expected_missing)
    missing_fn = len(expected_missing - actual_missing)
    missing_precision = _ratio(missing_tp, missing_tp + missing_fp)
    missing_recall = _ratio(missing_tp, missing_tp + missing_fn)
    missing_f1 = _ratio(
        2 * missing_precision * missing_recall,
        missing_precision + missing_recall,
        empty=0.0,
    )
    critical_misses = len(critical_expected - actual_missing)
    metadata_entries = [
        (expected_missing_metadata[key], candidate)
        for key, candidate in actual_missing_entries
        if key in expected_missing_metadata and expected_missing_metadata[key]
    ]
    missing_metadata_correct = sum(
        _candidate_matches_metadata(expected_metadata, candidate)
        for expected_metadata, candidate in metadata_entries
    )
    high_missing_entries = [
        (key, candidate)
        for key, candidate in actual_missing_entries
        if str(candidate.get("confidence") or "").upper() == "HIGH"
    ]
    high_missing_correct = sum(
        key in expected_missing_metadata
        and _candidate_matches_metadata(expected_missing_metadata[key], candidate)
        for key, candidate in high_missing_entries
    )

    return {
        "case_count": len(gold["cases"]),
        "protocol_success_rate": _ratio(protocol_successes, len(gold["cases"])),
        "finding_precision": finding_precision,
        "finding_recall": finding_recall,
        "finding_f1": finding_f1,
        "classification_accuracy": _ratio(
            suggestion_correct,
            len(matched_findings),
        ),
        "high_auto_fix_precision": _ratio(high_correct, len(high_actual)),
        "missing_requirement_precision": missing_precision,
        "missing_requirement_recall": missing_recall,
        "missing_requirement_f1": missing_f1,
        "missing_requirement_false_positives": missing_fp,
        "missing_metadata_accuracy": _ratio(
            missing_metadata_correct,
            len(metadata_entries),
        ),
        "missing_metadata_evaluated": len(metadata_entries),
        "high_missing_auto_add_precision": _ratio(
            high_missing_correct,
            len(high_missing_entries),
        ),
        "critical_misses": critical_misses,
        "latency_seconds": round(total_latency, 3),
        "estimated_cost_usd": round(total_cost, 6),
    }


def candidate_meets_replacement_gate(
    primary: dict[str, float | int],
    candidate: dict[str, float | int],
) -> bool:
    """Conservative gate used before changing the configured primary model."""

    # Omission precision is required so a model cannot satisfy the gate by
    # reporting every source passage as a missing requirement.  Treat legacy
    # score payloads that lack the metric as unsafe rather than silently
    # approving an unmeasured candidate.
    required_metrics = {
        "classification_accuracy",
        "missing_requirement_precision",
        "missing_metadata_accuracy",
        "high_missing_auto_add_precision",
    }
    if not required_metrics.issubset(primary) or not required_metrics.issubset(
        candidate
    ):
        return False
    primary_missing_precision = float(
        primary.get("missing_requirement_precision", 1.0)
    )

    return bool(
        float(candidate.get("critical_misses", 1.0)) == 0.0
        and float(candidate.get("protocol_success_rate", 0.0)) == 1.0
        and float(candidate.get("finding_f1", 0.0))
        >= float(primary.get("finding_f1", 0.0)) - 0.05
        and float(candidate.get("finding_precision", 0.0))
        >= float(primary.get("finding_precision", 0.0)) - 0.05
        and float(candidate["classification_accuracy"])
        >= float(primary.get("classification_accuracy", 1.0)) - 0.05
        and float(candidate.get("missing_requirement_recall", 0.0))
        >= float(primary.get("missing_requirement_recall", 0.0)) - 0.05
        and float(candidate["missing_requirement_precision"])
        >= primary_missing_precision - 0.05
        and float(candidate.get("missing_requirement_f1", 0.0))
        >= float(primary.get("missing_requirement_f1", 1.0)) - 0.05
        and float(candidate["missing_metadata_accuracy"])
        >= float(primary["missing_metadata_accuracy"]) - 0.05
        and float(candidate.get("high_auto_fix_precision", 0.0)) == 1.0
        and float(candidate["high_missing_auto_add_precision"]) == 1.0
    )


__all__ = [
    "DEFAULT_GOLD_PATH",
    "candidate_meets_replacement_gate",
    "load_gold_set",
    "score_model_outputs",
    "validate_gold_set",
]
