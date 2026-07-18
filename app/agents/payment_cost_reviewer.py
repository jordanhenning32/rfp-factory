"""Payment-Systems Cost Reviewer — adversarial fact-check of the
drafted fee narrative for service_line=payment_systems proposals.

The labor-flow Cost Reviewer (app/agents/cost_reviewer.py + dual-
pipeline orchestration in app/jobs/cost_reviewer.py) reviews
PricingPackage H/M/L scenarios. Payment_systems has no PricingPackage
rows — there's no labor build to scenario-review. This agent fills
the equivalent role differently:

  - Reads the drafted fee narrative (SEC-005 or whichever section
    requires_cost_analysis=True for the payment proposal) verbatim.
  - Reads the persisted Payment Market Scan
    (proposals.payment_market_scan_json) — recommended pricing
    structure, comparable awards, competitor processors, profit math.
  - Reads the company's payment_systems data files
    (data/pricing/payment_systems.json + _payment_systems_context.json)
    for compliance posture, brand framing, fit-risk talking points.
  - Adversarially flags drift:
      * RATE_DRIFT — narrative quotes a rate that doesn't match the
        scan's recommendation
      * HALLUCINATED_COMPARABLE — narrative cites a comparable award
        that doesn't appear in the scan
      * MISSING_DISCLOSURE — narrative omits a required disclosure
        (PCI Level 3, no-in-house-hardware, US-only data residency,
        etc.)
      * BRAND_VOICE_DRIFT — narrative pitches as a fitness vendor or
        otherwise violates the brand_framing.writer_voice_directive
      * UNADDRESSED_RISK — narrative doesn't address one of the
        fit_risk_talking_points head-on
      * NUMERIC_DRIFT — profit math, volume estimate, or rate
        positioning numbers don't match the persisted scan
      * COMPLIANCE_OVERCLAIM — narrative claims a PCI level we
        don't have, or claims in-house hardware / EMV / P2PE we
        don't hold

Single Sonnet 4.6 call (no dual-pipeline for MVP; can be added later
if quality suffers). Cost ~$0.05-0.10 per review.

Output: structured list of findings the orchestrator persists to
proposals.payment_cost_review_findings_json. Each finding includes
severity (CRITICAL / MAJOR / MINOR), category (one of the codes
above), finding_text, suggested_fix, and the section ID it applies
to.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from app.config import get_settings
from app.services.llm import call_tool_for_model

log = logging.getLogger(__name__)


# ---- Output dataclasses ---------------------------------------------------


@dataclass
class PaymentCostReviewFinding:
    """One adversarial flag against the drafted fee narrative."""

    finding_id: str  # PCR-001, PCR-002, ... (assembled by orchestrator)
    section_id: str  # e.g. SEC-005
    section_title: str
    severity: str  # CRITICAL | MAJOR | MINOR
    category: str  # one of the codes documented in the module docstring
    finding_text: str
    suggested_fix: str
    cited_quote: str  # verbatim snippet from the section that triggered the flag
    # User triage state — mirrors the labor flow's CostReviewFinding
    # user_action enum. Defaults to "pending" on a fresh review pass;
    # the user clicks Accept / Reject in the UI to flip it. user_note
    # holds either an edited suggested_fix (when user_action=accepted
    # via Edit) or a rejection reason (when user_action=rejected).
    user_action: str = "pending"  # pending | accepted | rejected
    user_note: str | None = None


@dataclass
class PaymentCostReviewResult:
    findings: list[PaymentCostReviewFinding]
    overall_assessment: str  # 1-2 sentence summary of whether the narrative is bid-ready
    bid_ready: bool
    sections_reviewed: list[str]  # section_id list

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaymentCostReviewInputs:
    rfp_title: str
    rfp_agency: str
    sections: list[dict[str, Any]]  # [{section_id, section_title, draft_markdown}]
    payment_market_scan: dict[str, Any]  # parsed payment_market_scan_json
    payment_systems_pricing: dict[str, Any]  # data/pricing/payment_systems.json
    payment_systems_context: dict[str, Any]  # data/pricing/_payment_systems_context.json


# ---- Tool schema ----------------------------------------------------------

_TOOL: dict = {
    "name": "report_payment_cost_review",
    "description": (
        "Report adversarial findings against the drafted fee "
        "narrative. Be RUTHLESS — the goal is to catch every "
        "fabrication, every overclaim, every missed disclosure "
        "before the proposal goes to the buyer. Only flag real "
        "drift; do not invent findings. If the narrative is clean, "
        "return findings=[] and bid_ready=true."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "section_id": {"type": "string"},
                        "section_title": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "description": "CRITICAL | MAJOR | MINOR",
                        },
                        "category": {
                            "type": "string",
                            "description": (
                                "RATE_DRIFT | HALLUCINATED_COMPARABLE | "
                                "MISSING_DISCLOSURE | BRAND_VOICE_DRIFT | "
                                "UNADDRESSED_RISK | NUMERIC_DRIFT | "
                                "COMPLIANCE_OVERCLAIM"
                            ),
                        },
                        "finding_text": {
                            "type": "string",
                            "description": ("1-3 sentences explaining what's wrong and why it matters."),
                        },
                        "suggested_fix": {
                            "type": "string",
                            "description": (
                                "Concrete actionable change for the "
                                "writer. Cite source data when "
                                "applicable: 'Use 22 bps from "
                                "pricing_structure.proposed_credit_card_"
                                "markup_bps, not the 25 bps the "
                                "narrative quotes.'"
                            ),
                        },
                        "cited_quote": {
                            "type": "string",
                            "description": (
                                "Verbatim snippet from the drafted "
                                "section that triggered the flag — "
                                "max ~200 chars. Lets the user locate "
                                "the issue without re-reading the "
                                "whole section."
                            ),
                        },
                    },
                    "required": [
                        "section_id",
                        "severity",
                        "category",
                        "finding_text",
                        "suggested_fix",
                        "cited_quote",
                    ],
                },
            },
            "overall_assessment": {
                "type": "string",
                "description": (
                    "1-2 sentence verdict on whether the narrative is "
                    "bid-ready. Be specific: 'Bid-ready pending one "
                    "MAJOR rate-drift fix' beats 'Looks mostly fine.'"
                ),
            },
            "bid_ready": {
                "type": "boolean",
                "description": (
                    "True only when no CRITICAL findings exist AND the "
                    "narrative cleanly addresses every fit-risk talking "
                    "point. Default to false if uncertain."
                ),
            },
        },
        "required": ["findings", "overall_assessment", "bid_ready"],
    },
}


_SYSTEM = """You are the Payment-Systems Cost Reviewer for Quadratic Digital LLC. Your job: adversarially fact-check the drafted fee narrative for ONE proposal against the persisted Payment Market Scan, the company's compliance posture, the brand framing rules, and the fit-risk talking points. You report findings — you do NOT rewrite the section.

Be RUTHLESS but PRECISE. Real drift gets flagged; speculation does not. Each finding cites a verbatim quote from the section so the writer can locate the issue.

CATEGORIES YOU FLAG:

1. RATE_DRIFT — the narrative quotes a rate that doesn't match the scan's recommendation. Example: scan says 22 bps proposed; narrative says "we propose 30 bps." Flag MAJOR. The recommended fix names the source field (`pricing_structure.proposed_credit_card_markup_bps`) and the correct value.

2. HALLUCINATED_COMPARABLE — the narrative cites a competitor or comparable award that doesn't appear in the scan's `comparable_awards` or `competitor_processors` arrays. Cross-check every named processor / contract / county / agency in the narrative against the scan. CRITICAL — fabricated citations get the proposal disqualified.

3. MISSING_DISCLOSURE — required disclosures missing from the narrative:
   - PCI DSS Level 3 (current state) + Level 2 roadmap (12 months)
   - No in-house POS terminal hardware (subcontract / partner approach)
   - U.S.-only data residency
   - End-to-end encryption (TLS 1.2+ in transit, AES-256 at rest)
   - Tokenization status (in development, target 2026-06-30)
   - NACHA member
   Flag MAJOR for any compliance disclosure missing from a section that legitimately needs it.

4. BRAND_VOICE_DRIFT — narrative pitches Quadratic Financial as a fitness/membership vendor or otherwise violates the `brand_framing.writer_voice_directive` (lead with combined integrator + payments-operator capability; Quadratic Digital LLC is the legal proposer; NAC's 40-year history is the payments backbone). Flag MAJOR — the buyer should never finish reading thinking "this is a fitness biller stretching into government."

5. UNADDRESSED_RISK — narrative doesn't address one of the `fit_risk_talking_points.risks` head-on. Example: the section is the Compliance / Security section but doesn't disclose PCI Level 3 + roadmap. Or the Solution Architecture section claims in-house hardware. Flag MAJOR (CRITICAL when the missed risk would obviously read as misleading to a procurement officer).

6. NUMERIC_DRIFT — profit math, volume estimate, transaction-count, or rate-positioning numbers don't match the persisted scan. Example: scan says $41M annual volume midpoint; narrative says "$50M annual volume." Flag MAJOR. The recommended fix names the source field.

7. COMPLIANCE_OVERCLAIM — narrative claims compliance posture beyond what we hold:
   - Claims PCI Level 1 or Level 2 (we hold Level 3)
   - Claims in-house EMV / P2PE certification (we inherit those from the hardware partner)
   - Claims in-house POS terminal hardware
   - Claims tokenization launched (still in development)
   Flag CRITICAL — overclaiming compliance is grounds for buyer disqualification + reputational damage.

DISCIPLINE:
- Quote verbatim. Every finding includes a `cited_quote` snippet from the section. If you can't find the offending phrase verbatim, the finding is speculation — drop it.
- Cite source fields. The `suggested_fix` always names the JSON field the writer should consult: `pricing_structure.proposed_credit_card_markup_bps`, `compliance_attestations.pci_dss_level`, `brand_framing.writer_voice_directive`, etc. The writer can then act on the fix without ambiguity.
- Severity discipline. CRITICAL = bid-disqualifying or proposal-credibility-destroying drift. MAJOR = real fix needed before submission. MINOR = polish-pass cleanup.
- Don't double-flag. If the same drift shows up in three sentences of the same section, one finding suffices.
- Don't invent. If the narrative is clean, return `findings=[]` with `bid_ready=true`. The reviewer's value is precision, not finding-padding.

OUTPUT: Use the `report_payment_cost_review` tool with the full findings list, an overall_assessment, and a bid_ready boolean.
"""


_USER_TEMPLATE = """=== Drafted fee narrative section(s) ===

{sections_block}

=== Payment Market Scan (recommended rates, comparable awards, competitors, volume estimate, profit math) ===

{scan_block}

=== Company payment-systems pricing data (compliance attestations, hardware approach, brand framing) ===

{pricing_block}

=== Service-line context (brand framing rules, fit-risk talking points, narrative anchors) ===

{context_block}

=== RFP context ===
Title: {rfp_title}
Customer agency: {rfp_agency}

Adversarially fact-check the drafted section(s) above. Cross-check every rate, every named competitor, every comparable-award citation, every compliance disclosure against the scan + pricing + context data. Quote verbatim from the section in each finding's cited_quote field. Use the report_payment_cost_review tool."""


# ---- Public entry point ---------------------------------------------------


def review_payment_cost(
    *,
    proposal_id: int,
    inputs: PaymentCostReviewInputs,
) -> PaymentCostReviewResult:
    """Run the Sonnet adversarial review. Returns a structured result
    the orchestrator persists. Caller MUST handle exceptions —
    transient API failures should not leave the proposal half-
    reviewed."""
    settings = get_settings()

    user_prompt = _USER_TEMPLATE.format(
        sections_block=_format_sections(inputs.sections),
        scan_block=_format_scan(inputs.payment_market_scan),
        pricing_block=_format_pricing(inputs.payment_systems_pricing),
        context_block=_format_context(inputs.payment_systems_context),
        rfp_title=inputs.rfp_title or "(untitled)",
        rfp_agency=inputs.rfp_agency or "(agency unknown)",
    )

    tool_input, usage = call_tool_for_model(
        model=settings.model_drafter,  # Sonnet 4.6
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=8000,
        agent_name="payment_cost_reviewer",
        proposal_id=proposal_id,
    )

    if usage.get("stop_reason") == "max_tokens":
        n_partial = len(tool_input.get("findings") or [])
        raise RuntimeError(
            f"payment_cost_reviewer: tool call truncated at "
            f"max_tokens (in={usage['input_tokens']}, "
            f"out={usage['output_tokens']}). Got {n_partial} partial "
            f"finding(s). Bump max_tokens or split sections."
        )

    findings: list[PaymentCostReviewFinding] = []
    section_titles_by_id = {s["section_id"]: s["section_title"] for s in inputs.sections}
    for i, f in enumerate(tool_input.get("findings") or [], start=1):
        sid = (f.get("section_id") or "").strip()
        findings.append(
            PaymentCostReviewFinding(
                finding_id=f"PCR-{i:03d}",
                section_id=sid,
                section_title=(f.get("section_title") or section_titles_by_id.get(sid, "")),
                severity=(f.get("severity") or "MINOR").upper().strip(),
                category=(f.get("category") or "OTHER").upper().strip(),
                finding_text=(f.get("finding_text") or "").strip(),
                suggested_fix=(f.get("suggested_fix") or "").strip(),
                cited_quote=(f.get("cited_quote") or "").strip(),
            )
        )

    return PaymentCostReviewResult(
        findings=findings,
        overall_assessment=(tool_input.get("overall_assessment") or "").strip(),
        bid_ready=bool(tool_input.get("bid_ready") or False),
        sections_reviewed=[s["section_id"] for s in inputs.sections],
    )


# ---- Prompt-block formatters ---------------------------------------------


def _format_sections(sections: list[dict[str, Any]]) -> str:
    if not sections:
        return "(no cost-deferred sections drafted yet — nothing to review)"
    parts: list[str] = []
    for s in sections:
        parts.append(f"--- {s.get('section_id', '?')} {s.get('section_title', '')} ---")
        parts.append(s.get("draft_markdown") or "(empty draft)")
        parts.append("")
    return "\n".join(parts)


def _format_scan(scan: dict[str, Any]) -> str:
    if not scan:
        return "(no payment market scan persisted — reviewer can only check brand / compliance drift)"
    import json as _json

    return _json.dumps(scan, indent=2, default=str)[:20000]


def _format_pricing(pricing: dict[str, Any]) -> str:
    if not pricing:
        return "(payment_systems.json missing or empty)"
    import json as _json

    return _json.dumps(pricing, indent=2, default=str)[:15000]


def _format_context(context: dict[str, Any]) -> str:
    if not context:
        return "(_payment_systems_context.json missing or empty)"
    import json as _json

    return _json.dumps(context, indent=2, default=str)[:15000]


__all__ = [
    "PaymentCostReviewFinding",
    "PaymentCostReviewResult",
    "PaymentCostReviewInputs",
    "review_payment_cost",
]
