"""Independent classification review for extracted compliance requirements.

The Compliance Matrix Agent performs source-level extraction with Sonnet.  This
module gives those extracted rows a second look with the independently
configured review model (Gemini by default).  Failed structured-output calls
are retried in smaller batches; Haiku is used only as a bounded leaf fallback.

The important contract is :class:`ComplianceValidationReport`: an empty
``findings`` list means "clean" only when ``reviewed_count == total_count``.
Callers must never infer coverage from the findings list alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Literal

from app.config import get_settings
from app.core.enums import RequirementCategory, RequirementType
from app.services.llm import (
    call_tool_for_model,
    fmt_llm_usage,
    is_transient_provider_error,
)

log = logging.getLogger(__name__)


_INITIAL_BATCH_SIZE = 25
_MIN_PRIMARY_BATCH_SIZE = 5
_MAX_SPLIT_DEPTH = 3
_MAX_TEXT_CHARS = 4000

_VALID_ISSUES = {
    "type_misclassified",
    "category_misclassified",
    "type_and_category_misclassified",
    "text_is_a_header_not_a_requirement",
    "text_is_truncated_or_incomplete",
    "duplicate_of_other_item",
    "other_concern",
}
_VALID_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
_VALID_TYPES = {item.value for item in RequirementType}
_VALID_CATEGORIES = {item.value for item in RequirementCategory}


_TOOL: dict = {
    "name": "report_validation_results",
    "description": (
        "Report ONLY extracted compliance items with a classification or "
        "text-quality issue. Return an empty results array when every item "
        "in this batch is sound."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "description": (
                    "Items with issues. Empty array if every item in the batch "
                    "looked correctly classified."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement_id": {
                            "type": "string",
                            "description": (
                                "Echo the REQ-ID exactly as provided so the caller "
                                "can match the result back."
                            ),
                        },
                        "issue": {
                            "type": "string",
                            "enum": sorted(_VALID_ISSUES),
                        },
                        "suggested_type": {
                            "type": "string",
                            "enum": sorted(_VALID_TYPES),
                            "description": (
                                "Corrected type. Omit when the issue is not "
                                "about requirement_type."
                            ),
                        },
                        "suggested_category": {
                            "type": "string",
                            "enum": sorted(_VALID_CATEGORIES),
                            "description": (
                                "Corrected category. Omit when the issue is "
                                "not about category."
                            ),
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["HIGH", "MEDIUM", "LOW"],
                        },
                        "reason": {
                            "type": "string",
                            "description": "One short sentence explaining the issue.",
                        },
                    },
                    "required": [
                        "requirement_id",
                        "issue",
                        "confidence",
                        "reason",
                    ],
                },
            },
        },
        "required": ["results"],
    },
}


_SYSTEM = """You are the independent quality reviewer for an RFP compliance matrix. The upstream Sonnet-class agent read the full source document and extracted the rows below. Audit only the visible rows for classification, malformed text, truncation, headings presented as requirements, and within-batch duplicates.

Valid requirement_type values: shall, must, should, submission_format, evaluation_criterion, mandatory_form.
Valid category values: technical, management, past_performance, personnel, pricing, administrative, certification.

Rules:
- Explicit shall/must/is required to language is mandatory. "Should" is advisory.
- submission_format is only for procedural submission rules such as page limits, fonts, deadlines, signatures, and file formats. A prompt to Describe/Provide/Explain substantive content is not submission_format.
- mandatory_form is for a form, certificate, attachment, or other required submission artifact.
- evaluation_criterion is for scoring, weights, evaluator ratings, and award factors.
- Do not flip among shall/must/should unless the target mandatory verb is visible in the supplied text. The upstream extractor may have inherited a mandatory parent heading you cannot see; when uncertain, defer or use MEDIUM confidence.
- Flag blank text, a bare heading, genuine truncation, and the second of near-identical duplicate rows.
- HIGH means unambiguous and safe to auto-correct. Use it sparingly. MEDIUM/LOW are review flags only.
- Return ONLY rows with issues. A genuinely clean batch is {"results": []}.
"""


_USER_TEMPLATE = """Audit these {n} compliance items. Return only items with issues.

{items_text}"""


ValidationState = Literal["complete", "degraded", "partial", "failed"]


@dataclass(frozen=True)
class ValidationResult:
    """One classification or text-quality finding from the validator.

    Clean items do not produce a result; only flagged items appear.
    """

    requirement_id: str
    issue: str
    suggested_type: str | None
    suggested_category: str | None
    confidence: str
    reason: str
    review_role: Literal["primary", "fallback"] = "primary"


@dataclass(frozen=True)
class ValidationAttempt:
    """One provider attempt against an exact requirement subset."""

    model: str
    role: Literal["primary", "fallback"]
    requirement_ids: tuple[str, ...]
    depth: int
    success: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error_kind: str | None = None


@dataclass
class ComplianceValidationReport:
    """Coverage-aware result of the independent item review."""

    total_count: int
    primary_model: str
    fallback_model: str
    findings: list[ValidationResult] = field(default_factory=list)
    attempts: list[ValidationAttempt] = field(default_factory=list)
    reviewed_requirement_ids: list[str] = field(default_factory=list)
    unresolved_requirement_ids: list[str] = field(default_factory=list)
    auto_applied_count: int = 0
    flagged_for_review_count: int = 0
    blocked_correction_count: int = 0
    noop_finding_count: int = 0
    manual_review_findings: list[ValidationResult] = field(default_factory=list)
    auto_applied_changes: list[dict] = field(default_factory=list)

    @property
    def reviewed_count(self) -> int:
        return len(dict.fromkeys(self.reviewed_requirement_ids))

    @property
    def fallback_used(self) -> bool:
        return any(a.role == "fallback" and a.success for a in self.attempts)

    @property
    def retry_used(self) -> bool:
        return any(a.role == "primary" and a.depth > 0 for a in self.attempts)

    @property
    def primary_failed_attempts(self) -> int:
        return sum(1 for a in self.attempts if a.role == "primary" and not a.success)

    @property
    def fallback_reviewed_count(self) -> int:
        ids: list[str] = []
        for attempt in self.attempts:
            if attempt.role == "fallback" and attempt.success:
                ids.extend(attempt.requirement_ids)
        return len(dict.fromkeys(ids))

    @property
    def state(self) -> ValidationState:
        if self.total_count == 0:
            return "complete"
        if self.reviewed_count == 0:
            return "failed"
        if self.unresolved_requirement_ids:
            return "partial"
        if self.fallback_used:
            return "degraded"
        return "complete"

    @property
    def input_tokens(self) -> int:
        return sum(a.input_tokens for a in self.attempts)

    @property
    def output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.attempts)

    @property
    def cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.attempts)

    def as_public_dict(self) -> dict:
        """Sanitized, durable summary suitable for ``structure_json``."""

        return {
            "state": self.state,
            "total_count": self.total_count,
            "reviewed_count": self.reviewed_count,
            "unresolved_requirement_ids": list(self.unresolved_requirement_ids),
            "findings_count": len(self.findings),
            "auto_applied_count": self.auto_applied_count,
            "auto_applied_change_count": len(self.auto_applied_changes),
            "flagged_for_review_count": self.flagged_for_review_count,
            "blocked_correction_count": self.blocked_correction_count,
            "noop_finding_count": self.noop_finding_count,
            "primary_model": self.primary_model,
            "fallback_model": self.fallback_model,
            "primary_attempts": sum(a.role == "primary" for a in self.attempts),
            "primary_failed_attempts": self.primary_failed_attempts,
            "retry_used": self.retry_used,
            "fallback_used": self.fallback_used,
            "fallback_reviewed_count": self.fallback_reviewed_count,
            "manual_review_count": len(self.manual_review_findings),
            "manual_review": [
                {
                    "requirement_id": finding.requirement_id,
                    "issue": finding.issue,
                    "confidence": finding.confidence,
                    "reason": finding.reason,
                    "suggested_type": finding.suggested_type,
                    "suggested_category": finding.suggested_category,
                    "review_role": finding.review_role,
                }
                for finding in self.manual_review_findings[:50]
            ],
            "auto_applied": [dict(change) for change in self.auto_applied_changes[:50]],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.cost_usd, 6),
        }


class ValidationProtocolError(RuntimeError):
    """The provider returned a tool payload that cannot prove coverage."""


class ComplianceValidationIncompleteError(RuntimeError):
    """Legacy list caller requested findings from an incomplete review."""


def _format_items_for_validation(items: list[dict]) -> str:
    lines: list[str] = []
    for item in items:
        text = str(item.get("requirement_text") or "").strip()
        if len(text) > _MAX_TEXT_CHARS:
            text = (
                text[:_MAX_TEXT_CHARS].rstrip()
                + " [DISPLAY-CAPPED: validator display limit, not source "
                "truncation; do not flag as text_is_truncated_or_incomplete]"
            )
        lines.append(
            f"REQ-ID: {item.get('requirement_id', '?')}\n"
            f"  Type: {item.get('requirement_type', '')}\n"
            f"  Category: {item.get('category', '')}\n"
            f"  Text: {text}"
        )
    return "\n\n".join(lines)


def _usage_numbers(usage: dict | None) -> tuple[int, int, float]:
    usage = usage or {}
    return (
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        float(usage.get("cost_usd") or 0.0),
    )


def _parse_results_strict(payload: dict, batch: list[dict]) -> list[ValidationResult]:
    """Validate one tool response atomically.

    A malformed row makes the whole attempt invalid so it can be retried.  It
    is never silently dropped and then mistaken for a clean review.
    """

    if "results" not in payload:
        raise ValidationProtocolError("tool payload is missing required 'results'")
    raw = payload["results"]
    if not isinstance(raw, list):
        raise ValidationProtocolError("tool payload 'results' is not an array")

    allowed_ids = {str(item.get("requirement_id") or "") for item in batch}
    seen_ids: set[str] = set()
    parsed: list[ValidationResult] = []
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ValidationProtocolError(f"result {index} is not an object")
        requirement_id = str(row.get("requirement_id") or "")
        if requirement_id not in allowed_ids:
            raise ValidationProtocolError(
                f"result {index} references unknown requirement_id {requirement_id!r}"
            )
        if requirement_id in seen_ids:
            raise ValidationProtocolError(
                f"multiple results returned for requirement_id {requirement_id!r}"
            )
        seen_ids.add(requirement_id)

        issue = str(row.get("issue") or "")
        confidence = str(row.get("confidence") or "").upper()
        reason = str(row.get("reason") or "").strip()
        if issue not in _VALID_ISSUES:
            raise ValidationProtocolError(f"invalid issue {issue!r}")
        if confidence not in _VALID_CONFIDENCE:
            raise ValidationProtocolError(f"invalid confidence {confidence!r}")
        if not reason:
            raise ValidationProtocolError("finding reason is empty")

        suggested_type = row.get("suggested_type")
        suggested_category = row.get("suggested_category")
        if "suggested_type" in row and suggested_type not in _VALID_TYPES:
            raise ValidationProtocolError(f"invalid suggested_type {suggested_type!r}")
        if "suggested_category" in row and suggested_category not in _VALID_CATEGORIES:
            raise ValidationProtocolError(
                f"invalid suggested_category {suggested_category!r}"
            )
        if issue in {"type_misclassified", "type_and_category_misclassified"} and not suggested_type:
            raise ValidationProtocolError(f"{issue} is missing suggested_type")
        if issue in {"category_misclassified", "type_and_category_misclassified"} and not suggested_category:
            raise ValidationProtocolError(f"{issue} is missing suggested_category")
        if issue == "type_misclassified" and suggested_category is not None:
            raise ValidationProtocolError(
                "type_misclassified must not include suggested_category"
            )
        if issue == "category_misclassified" and suggested_type is not None:
            raise ValidationProtocolError(
                "category_misclassified must not include suggested_type"
            )
        if issue not in {
            "type_misclassified",
            "category_misclassified",
            "type_and_category_misclassified",
        } and (suggested_type is not None or suggested_category is not None):
            raise ValidationProtocolError(
                f"{issue} must not include classification suggestions"
            )

        parsed.append(
            ValidationResult(
                requirement_id=requirement_id,
                issue=issue,
                suggested_type=str(suggested_type) if suggested_type else None,
                suggested_category=(
                    str(suggested_category) if suggested_category else None
                ),
                confidence=confidence,
                reason=reason,
            )
        )
    return parsed


def _error_kind(exc: Exception) -> str:
    return type(exc).__name__


def _is_provider_wide_failure(exc: Exception) -> bool:
    """Avoid pointless bisection for credentials/model/provider outages."""

    # Protocol errors describe a provider response that reached us but failed
    # this agent's structured-output contract.  Their diagnostic text may
    # legitimately echo model output containing words such as "timeout" or
    # "credential"; that must not turn a batch-local parse failure into a
    # provider-wide outage and skip the smaller-batch recovery path.
    if isinstance(exc, ValidationProtocolError):
        return False
    if is_transient_provider_error(exc):
        return True
    message = str(exc).lower()
    markers = (
        "api key",
        "credential",
        "unauthorized",
        "permission denied",
        "forbidden",
        "model not found",
        "status code: 401",
        "status code: 403",
        "status code: 404",
        "quota exceeded",
        "rate limit",
        "status code: 429",
        "status code: 500",
        "status code: 502",
        "status code: 503",
        "status code: 504",
        "service unavailable",
        "connection error",
        "connection reset",
        "connection refused",
        "timed out",
        "timeout",
    )
    return any(marker in message for marker in markers)


def validate_compliance_items_report(
    items: list[dict],
    *,
    proposal_id: int | None = None,
) -> ComplianceValidationReport:
    """Review extracted items with coverage-aware retry/fallback reporting."""

    settings = get_settings()
    primary_model = settings.model_compliance_validator
    fallback_model = settings.model_compliance_validator_fallback
    report = ComplianceValidationReport(
        total_count=len(items),
        primary_model=primary_model,
        fallback_model=fallback_model,
    )
    if not items:
        return report

    item_ids = [str(item.get("requirement_id") or "") for item in items]
    if not all(item_ids) or len(set(item_ids)) != len(item_ids):
        raise ValueError("compliance validation requires unique, non-empty requirement IDs")

    findings_by_key: dict[tuple[str, str], ValidationResult] = {}

    def _call_batch(
        batch: list[dict],
        *,
        model: str,
        role: Literal["primary", "fallback"],
        depth: int,
    ) -> tuple[list[ValidationResult], dict]:
        prompt = _USER_TEMPLATE.format(
            n=len(batch),
            items_text=_format_items_for_validation(batch),
        )
        if role == "fallback":
            agent_name = "compliance_validator_fallback"
        elif depth:
            agent_name = "compliance_validator_retry"
        else:
            agent_name = "compliance_validator"
        payload, usage = call_tool_for_model(
            model=model,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tool=_TOOL,
            max_tokens=4000,
            agent_name=agent_name,
            proposal_id=proposal_id,
        )
        return _parse_results_strict(payload, batch), usage

    def _record_attempt(
        batch: list[dict],
        *,
        model: str,
        role: Literal["primary", "fallback"],
        depth: int,
        success: bool,
        usage: dict | None = None,
        exc: Exception | None = None,
    ) -> None:
        in_tok, out_tok, cost = _usage_numbers(usage)
        report.attempts.append(
            ValidationAttempt(
                model=model,
                role=role,
                requirement_ids=tuple(str(item["requirement_id"]) for item in batch),
                depth=depth,
                success=success,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                error_kind=_error_kind(exc) if exc is not None else None,
            )
        )

    def _accept(
        batch: list[dict],
        batch_findings: list[ValidationResult],
        *,
        role: Literal["primary", "fallback"],
    ) -> None:
        report.reviewed_requirement_ids.extend(
            str(item["requirement_id"]) for item in batch
        )
        for finding in batch_findings:
            finding = replace(finding, review_role=role)
            key = (finding.requirement_id, finding.issue)
            if key not in findings_by_key or role == "primary":
                findings_by_key[key] = finding

    def _fallback(batch: list[dict], depth: int) -> None:
        try:
            batch_findings, usage = _call_batch(
                batch,
                model=fallback_model,
                role="fallback",
                depth=depth,
            )
        except Exception as exc:
            _record_attempt(
                batch,
                model=fallback_model,
                role="fallback",
                depth=depth,
                success=False,
                exc=exc,
            )
            report.unresolved_requirement_ids.extend(
                str(item["requirement_id"]) for item in batch
            )
            log.exception(
                "compliance_validator: fallback failed for %d item(s)",
                len(batch),
            )
            return
        _record_attempt(
            batch,
            model=fallback_model,
            role="fallback",
            depth=depth,
            success=True,
            usage=usage,
        )
        _accept(batch, batch_findings, role="fallback")
        log.warning(
            "compliance_validator: Haiku fallback reviewed %d item(s) -> %d finding(s), %s",
            len(batch),
            len(batch_findings),
            fmt_llm_usage(usage),
        )

    def _review_primary(batch: list[dict], depth: int = 0) -> None:
        try:
            batch_findings, usage = _call_batch(
                batch,
                model=primary_model,
                role="primary",
                depth=depth,
            )
        except Exception as exc:
            _record_attempt(
                batch,
                model=primary_model,
                role="primary",
                depth=depth,
                success=False,
                exc=exc,
            )
            can_split = (
                len(batch) > _MIN_PRIMARY_BATCH_SIZE
                and depth < _MAX_SPLIT_DEPTH
                and not _is_provider_wide_failure(exc)
            )
            if can_split:
                midpoint = len(batch) // 2
                log.warning(
                    "compliance_validator: primary review failed for %d item(s); "
                    "retrying as %d + %d",
                    len(batch),
                    midpoint,
                    len(batch) - midpoint,
                )
                _review_primary(batch[:midpoint], depth + 1)
                _review_primary(batch[midpoint:], depth + 1)
            else:
                _fallback(batch, depth + 1)
            return

        _record_attempt(
            batch,
            model=primary_model,
            role="primary",
            depth=depth,
            success=True,
            usage=usage,
        )
        _accept(batch, batch_findings, role="primary")
        log.info(
            "compliance_validator: primary reviewed %d item(s) -> %d finding(s), %s",
            len(batch),
            len(batch_findings),
            fmt_llm_usage(usage),
        )

    for start in range(0, len(items), _INITIAL_BATCH_SIZE):
        _review_primary(items[start : start + _INITIAL_BATCH_SIZE])

    report.findings = list(findings_by_key.values())
    report.unresolved_requirement_ids = list(
        dict.fromkeys(report.unresolved_requirement_ids)
    )
    return report


def validate_compliance_items(
    items: list[dict],
    *,
    proposal_id: int | None = None,
) -> list[ValidationResult]:
    """Backward-compatible findings-only wrapper that still fails closed.

    New pipeline code should use :func:`validate_compliance_items_report`.
    This wrapper raises if any requirement remains unreviewed, preventing a
    legacy caller from treating an ambiguous empty list as a clean result.
    """

    report = validate_compliance_items_report(items, proposal_id=proposal_id)
    if report.state != "complete":
        raise ComplianceValidationIncompleteError(
            "independent compliance review did not complete on the primary model"
            + (
                ": " + ", ".join(report.unresolved_requirement_ids)
                if report.unresolved_requirement_ids
                else ""
            )
        )
    return report.findings


__all__ = [
    "ComplianceValidationIncompleteError",
    "ComplianceValidationReport",
    "ValidationAttempt",
    "ValidationProtocolError",
    "ValidationResult",
    "validate_compliance_items",
    "validate_compliance_items_report",
]
