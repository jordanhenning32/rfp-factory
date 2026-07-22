"""Validation and UI-safe normalization for durable requirements-review state."""
from __future__ import annotations

import math
from typing import Any

_ALLOWED_STATUSES = {
    "pending",
    "extracting",
    "reviewing",
    "complete",
    "review_required",
    "degraded",
    "not_applicable",
    "partial",
    "failed",
}

_COUNT_FIELDS = {
    "extraction": {
        "initial_item_count",
        "recovered_item_count",
        "final_item_count",
    },
    "classification": {
        "total_count",
        "reviewed_count",
        "findings_count",
        "auto_applied_count",
        "auto_applied_change_count",
        "manual_review_count",
        "flagged_for_review_count",
        "blocked_correction_count",
    },
    "completeness": {
        "source_units_total",
        "reviewed_units",
        "candidate_count",
        "manual_review_candidate_count",
        "uncertain_passage_count",
    },
}

_LIST_FIELDS: dict[str, dict[str, bool]] = {
    "classification": {
        "unresolved_requirement_ids": False,
        "manual_review": True,
        "auto_applied": True,
    },
    "completeness": {
        "unresolved_unit_labels": False,
        "manual_review": True,
        "uncertain_passages": True,
    },
}


def _invalid_review(reason: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "requires_manual_review": True,
        "reason": reason,
    }


def _coerce_count(value: Any) -> tuple[int, bool]:
    if value is None or value == "":
        return 0, True
    if isinstance(value, bool):
        return 0, False
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0, False
    if parsed < 0 or (isinstance(value, float) and not value.is_integer()):
        return 0, False
    return parsed, True


def _coerce_cost(value: Any) -> tuple[float, bool]:
    if value is None or value == "":
        return 0.0, True
    if isinstance(value, bool):
        return 0.0, False
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0, False
    if not math.isfinite(parsed) or parsed < 0:
        return 0.0, False
    return parsed, True


def normalize_requirements_review(structure: object) -> dict[str, Any]:
    """Return a copy safe for rendering and lifecycle decisions.

    Absence remains the legacy-compatible empty state. Once a review key is
    present, malformed nested dictionaries, counts, lists, or model IDs are
    converted to a visible fail-closed ``unknown`` state instead of crashing
    a screen or being accepted as a clean review.
    """

    if not isinstance(structure, dict) or "requirements_review" not in structure:
        return {}
    raw = structure.get("requirements_review")
    if not isinstance(raw, dict) or not raw:
        return _invalid_review(
            "Stored requirements-review state is invalid. Retry intake."
        )

    review = dict(raw)
    malformed = False
    status = str(review.get("status") or "").strip().lower()
    if status not in _ALLOWED_STATUSES:
        malformed = True
    else:
        review["status"] = status

    sections: dict[str, dict[str, Any]] = {}
    for field in ("extraction", "classification", "completeness"):
        value = review.get(field)
        if value is None and field not in review:
            sections[field] = {}
        elif not isinstance(value, dict):
            malformed = True
            sections[field] = {}
        else:
            sections[field] = dict(value)
        review[field] = sections[field]

    coverage_value = sections["extraction"].get("coverage")
    if coverage_value is None and "coverage" not in sections["extraction"]:
        coverage: dict[str, Any] = {}
    elif not isinstance(coverage_value, dict):
        malformed = True
        coverage = {}
    else:
        coverage = dict(coverage_value)
    sections["extraction"]["coverage"] = coverage

    for section_name, fields in _COUNT_FIELDS.items():
        section = sections[section_name]
        for field in fields:
            if field not in section:
                continue
            section[field], valid = _coerce_count(section[field])
            malformed = malformed or not valid

    for field in (
        "source_chunks_total",
        "source_chunks_completed",
        "failed_chunk_count",
        "malformed_items_skipped",
    ):
        if field not in coverage:
            continue
        coverage[field], valid = _coerce_count(coverage[field])
        malformed = malformed or not valid

    for section_name, fields in _LIST_FIELDS.items():
        section = sections[section_name]
        for field, require_dict_items in fields.items():
            if field not in section:
                continue
            value = section[field]
            valid = isinstance(value, list)
            if valid and require_dict_items:
                valid = all(isinstance(item, dict) for item in value)
            if not valid:
                malformed = True
                section[field] = []
            elif require_dict_items:
                section[field] = [dict(item) for item in value]
            else:
                section[field] = list(value)

    for field in ("failed_chunk_labels", "incomplete_reasons"):
        if field not in coverage:
            continue
        if not isinstance(coverage[field], list):
            malformed = True
            coverage[field] = []
        else:
            coverage[field] = list(coverage[field])

    if "recovered_requirement_ids" in review:
        if not isinstance(review["recovered_requirement_ids"], list):
            malformed = True
            review["recovered_requirement_ids"] = []
        else:
            review["recovered_requirement_ids"] = list(
                review["recovered_requirement_ids"]
            )

    for section_name, fields in {
        "extraction": ("model",),
        "classification": ("primary_model", "fallback_model"),
        "completeness": ("primary_model", "fallback_model"),
    }.items():
        section = sections[section_name]
        for field in fields:
            if field in section and section[field] is not None and not isinstance(
                section[field], str
            ):
                malformed = True
                section[field] = ""

    for field in ("known_review_cost_usd", "estimated_cost_usd"):
        if field not in review:
            continue
        review[field], valid = _coerce_cost(review[field])
        malformed = malformed or not valid

    if malformed:
        review.update(
            _invalid_review(
                "Stored requirements-review details are invalid. Retry intake."
            )
        )
    return review


__all__ = ["normalize_requirements_review"]
