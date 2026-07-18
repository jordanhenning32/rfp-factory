"""Compliance Matrix validation pass.

Runs after the Compliance Matrix Agent's extraction. Re-reads each
extracted item with Haiku to catch mis-categorizations the bigger
drafter model occasionally lets through (e.g., requirement_type=
'certification' which is a category value, not a type, or a clearly
"shall" requirement tagged 'should').

Output is a list of `ValidationResult`s, one per FLAGGED item — clean
items don't appear. The intake job applies HIGH-confidence corrections
in-place and surfaces lower-confidence flags as warnings, so data
quality issues get caught at intake time rather than downstream.

Per-item cost is ~$0.0003 (Haiku, ~150 tokens in / ~30 tokens out
batched 50 per call). Total intake addition: ~$0.05 + ~30s on a
typical 142-item RFP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings
from app.core.enums import RequirementCategory, RequirementType
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


# Items per Haiku call. 50 keeps each call's input + output within
# Haiku's friendly response window without splitting too small.
_BATCH_SIZE = 50

# Cap requirement_text in the prompt so a pathologically long single
# item can't blow the per-call input budget. Set high enough that real
# RFP requirements (typically <1500 chars) pass through verbatim — the
# validator MUST see the actual text ending to judge whether the
# UPSTREAM agent truncated. A previous, smaller cap (400 chars + "…")
# self-induced "text_is_truncated_or_incomplete" false positives because
# the LLM saw OUR display ellipsis and reported the data as truncated.
_MAX_TEXT_CHARS = 4000


_TOOL: dict = {
    "name": "report_validation_results",
    "description": (
        "For each compliance item provided, report ONLY items where "
        "the assigned requirement_type or category looks wrong given "
        "the requirement_text, OR the item itself looks malformed "
        "(empty / truncated / a header rather than a real requirement). "
        "Skip items that look fine — do NOT fabricate findings."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "description": (
                    "Items with issues. Empty array if every item in the batch looked correctly classified."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement_id": {
                            "type": "string",
                            "description": (
                                "Echo the REQ-ID exactly as provided so the caller can match the result back."
                            ),
                        },
                        "issue": {
                            "type": "string",
                            "enum": [
                                "type_misclassified",
                                "category_misclassified",
                                "type_and_category_misclassified",
                                "text_is_a_header_not_a_requirement",
                                "text_is_truncated_or_incomplete",
                                "duplicate_of_other_item",
                                "other_concern",
                            ],
                        },
                        "suggested_type": {
                            "type": "string",
                            "enum": [t.value for t in RequirementType],
                            "description": (
                                "Corrected requirement_type. OMIT "
                                "this field entirely (do not set it "
                                "to null) if the issue is unrelated "
                                "to type. Gemini's schema validator "
                                "rejects array-form types and null "
                                "in enum lists, so optionality is "
                                "expressed by omission, not nullable."
                            ),
                        },
                        "suggested_category": {
                            "type": "string",
                            "enum": [c.value for c in RequirementCategory],
                            "description": (
                                "Corrected category. OMIT this field "
                                "entirely (do not set it to null) if "
                                "the issue is unrelated to category. "
                                "Same Gemini-compat rationale as "
                                "suggested_type — optionality via "
                                "omission, not nullable."
                            ),
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["HIGH", "MEDIUM", "LOW"],
                            "description": (
                                "HIGH = clear-cut, safe to auto-apply "
                                "(use only when the original is "
                                "obviously wrong and the suggestion is "
                                "unambiguous). MEDIUM = probably right, "
                                "log as warning. LOW = just flagging "
                                "for human review."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "1 short sentence explaining the issue. "
                                "Quote the offending phrase if helpful."
                            ),
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


_SYSTEM = """You audit RFP compliance matrix extractions for mis-categorizations. The upstream agent (a Sonnet-class model) extracted these items from RFP source text — you double-check the requirement_type and category labels.

Valid `requirement_type` values: shall, must, should, submission_format, evaluation_criterion, mandatory_form.

Valid `category` values: technical, management, past_performance, personnel, pricing, administrative, certification.

ERRORS TO CATCH:

1. **requirement_type confused with category.** The drafter model sometimes returns a category value where a type is expected. Common drift:
   - 'certification' appears as a TYPE → it's a category. Suggested type is usually 'mandatory_form' (cert needs to be submitted) or 'shall' (cert is required to bid).
   - 'administrative' appears as a TYPE → it's a category. Suggest 'should' or 'submission_format'.
   - 'past_performance' / 'personnel' / 'pricing' / 'technical' as TYPES → all are categories.

1b. **Category confused with requirement_type (inverse drift).** The same model also sometimes returns a TYPE value where a category is expected. Watch for:
   - 'submission_format' appears as a CATEGORY → it's a type. Suggested category: 'administrative' (page limits / font specs / due dates are administrative).
   - 'mandatory_form' appears as a CATEGORY → it's a type. Suggested category: 'administrative' (forms are administrative submissions) — unless the form is a certification form, in which case 'certification'.
   - 'evaluation_criterion' appears as a CATEGORY → it's a type. Suggested category depends on what's being evaluated (technical / management / past_performance / personnel / pricing / certification).
   - 'shall' / 'must' / 'should' appear as CATEGORIES → all are types. Suggested category depends on subject matter; default to 'technical' if uncertain.

2. **Type wrong given the verb in the text.** The strictest verb wins:
   - "shall" / "must" / "is required to" → type=shall (or 'must' — they're equivalent here).
   - "should" / "will be evaluated on" → type=should or evaluation_criterion.
   - Page limits / font size / margin / file format / page count / submission deadline / signature-before-submittal procedural rules → type=submission_format. ONLY procedural rules about HOW to submit, NEVER content prompts.
   - "Submit Form X" / "Provide W-9 / COI" / "Attach VRAR at submission" → type=mandatory_form.
   - Scoring criteria / weights / "evaluators will rate" → type=evaluation_criterion.

   CRITICAL: do NOT flip "Describe X" / "Provide a description of X" / "Explain your approach to X" prompts to `submission_format`. Those are CONTENT requirements (the vendor must respond substantively) — they stay `shall` or `should` per their verb. `submission_format` is reserved for procedural formatting rules ABOUT the submission (font, page limits, due date format, file types, signing rules), NOT for prompts that simply ask the vendor to describe something in their response. "Describe…" text triggers `shall`/`should` on Quadratic; never `submission_format`.

   CRITICAL — verb-strictness flips: do NOT flip type between {shall, must, should} unless the requirement_text EXPLICITLY contains the target mandatory verb. The upstream agent saw the full PDF context (e.g., a parent heading like "The Vendor shall describe the following:" before a bulleted list); you only see the extracted snippet. When the visible text lacks "shall" / "must" / "is required to", DEFER to the upstream classification — do not rationalize "this is a content prompt requiring a substantive response, therefore shall." That is the wrong heuristic. Bare imperatives like "Describe X" / "Provide Y" / "Explain Z" / "Supply W" without an explicit mandatory verb stay as the upstream agent assigned them. If you genuinely believe the upstream call is wrong but the verb isn't in the visible text, downgrade to MEDIUM and let a human review.

3. **Category wrong given the topic.** Fast checks:
   - Cybersecurity / FedRAMP / NIST 800-53 / FISMA → category=technical OR certification (depends on whether the requirement is "have certification X" vs "implement control Y").
   - Resumes / staffing / key personnel / labor categories → category=personnel.
   - Past performance citations / references / prior contracts → category=past_performance.
   - Pricing / labor rates / cost volumes → category=pricing.
   - Project management / governance / status reporting → category=management.
   - 8(a) / HUBZone / SDVOSB / WOSB / W-9 / COI / DUNS / SAM → category=certification or administrative.

4. **Non-requirement items.** Flag if requirement_text:
   - Is empty or whitespace-only.
   - Is just a section heading ("Section 3.2 Technical Approach", "Volume II — Management").
   - Is clearly truncated (ends mid-sentence, "...", or starts mid-sentence with no subject).
   - EXCEPTION: if a text ends with "[DISPLAY-CAPPED — validator's display cut-off, not data truncation; do not flag as text_is_truncated_or_incomplete]", that's THIS validator's own display cap, NOT data truncation — IGNORE that ending. Any "…" character preceding "[DISPLAY-CAPPED…]" is also display, not data.

5. **Duplicates.** If two items in this batch have near-identical text (>80% overlap), flag the second as duplicate.

CONFIDENCE LEVELS:
- HIGH = the original is clearly wrong AND your suggestion is unambiguous. Safe to auto-apply. Use SPARINGLY — when in doubt, downgrade to MEDIUM.
- MEDIUM = probably right but reasonable people might disagree. Log as a warning, don't auto-apply.
- LOW = just flagging it for human review.

OUTPUT: Call report_validation_results with ONLY items that have issues. If every item in the batch looks correctly classified, return an empty results array. Do NOT fabricate findings — false positives waste the user's time and erode trust in the validator."""


_USER_TEMPLATE = """Audit these {n} compliance items extracted from an RFP. Return only items with issues; clean items should not appear in the results.

{items_text}"""


@dataclass
class ValidationResult:
    """One audit finding from the validator. Clean items don't get a
    result; only flagged items appear in the output list."""

    requirement_id: str
    issue: str
    suggested_type: str | None
    suggested_category: str | None
    confidence: str
    reason: str


def _format_items_for_validation(items: list[dict]) -> str:
    """Compact one-block-per-item format. Caps requirement_text only
    when it exceeds the (high) _MAX_TEXT_CHARS budget; the marker is
    deliberately verbose-and-bracketed so the validator never confuses
    the validator's own display cap with data-side truncation."""
    lines: list[str] = []
    for it in items:
        text = (it.get("requirement_text") or "").strip()
        if len(text) > _MAX_TEXT_CHARS:
            text = (
                text[:_MAX_TEXT_CHARS].rstrip() + " [DISPLAY-CAPPED — validator's display cut-off, not "
                "data truncation; do not flag as text_is_truncated_or_incomplete]"
            )
        lines.append(
            f"REQ-ID: {it.get('requirement_id', '?')}\n"
            f"  Type: {it.get('requirement_type', '')}\n"
            f"  Category: {it.get('category', '')}\n"
            f"  Text: {text}"
        )
    return "\n\n".join(lines)


def validate_compliance_items(
    items: list[dict],
    *,
    proposal_id: int | None = None,
) -> list[ValidationResult]:
    """Audit a list of extracted compliance items via Haiku. Returns
    only the items with issues — clean items don't appear in results.

    `items` is a list of dicts with keys: requirement_id,
    requirement_text, requirement_type, category. Anything else is
    ignored.

    Best-effort: a per-batch failure is logged and skipped (the rest of
    the batches still run). Caller decides what to do with the results.
    """
    if not items:
        return []

    settings = get_settings()
    out: list[ValidationResult] = []

    for i in range(0, len(items), _BATCH_SIZE):
        batch = items[i : i + _BATCH_SIZE]
        user_prompt = _USER_TEMPLATE.format(
            n=len(batch),
            items_text=_format_items_for_validation(batch),
        )
        try:
            tool_input, usage = call_tool_for_model(
                model=settings.model_compliance_validator,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
                tool=_TOOL,
                max_tokens=4000,
                agent_name="compliance_validator",
                proposal_id=proposal_id,
            )
        except Exception:
            log.exception(
                "compliance_validator: batch %d-%d failed; skipping",
                i,
                i + len(batch),
            )
            continue

        raw = tool_input.get("results") or []
        log.info(
            "compliance_validator: batch of %d items -> %d issue(s), %s",
            len(batch),
            len(raw),
            fmt_llm_usage(usage),
        )
        for r in raw:
            try:
                out.append(
                    ValidationResult(
                        requirement_id=str(r["requirement_id"]),
                        issue=str(r.get("issue", "other_concern")),
                        suggested_type=r.get("suggested_type"),
                        suggested_category=r.get("suggested_category"),
                        confidence=str(r.get("confidence", "MEDIUM")).upper(),
                        reason=str(r.get("reason", "")),
                    )
                )
            except (KeyError, TypeError) as exc:
                log.warning(
                    "compliance_validator: skipping malformed result %r: %s",
                    r,
                    exc,
                )

    return out


__all__ = ["validate_compliance_items", "ValidationResult"]
