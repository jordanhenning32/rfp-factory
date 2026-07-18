"""Cost Review Consolidator — merges findings from two reviewers
(Gemini 2.5 Pro + GPT-5.5) into a tiered output:

  - CONSENSUS findings (BOTH reviewers raised the same underlying
    issue): kept at synthesized severity, with the higher of the
    two reviewers' severities when they disagree.
  - MINORITY findings (only ONE reviewer raised it): kept too, but
    severity is FORCED to MINOR and the finding_text is prefixed
    with provenance ("[Single-reviewer flag from gemini-2.5-pro
    …]"). The user sees minorities for completeness but with
    clear "not corroborated" framing so they're weighted appropriately.

Why both? Earlier design dropped minorities entirely; in practice
useful findings (e.g., security hours under-allocation) only one
reviewer caught were lost. Persisting as MINOR gives the user
visibility without elevating uncorroborated flags above genuinely
agreed-upon issues.

Cost: 2 reviewer calls + 1 consolidator + (optional) strategist
per Cost Review run. ~$0.40-1.00 total.

Single Sonnet 4.6 call. Returns a CostReviewResult-shaped object so
the orchestrator can persist via the existing path.
"""

from __future__ import annotations

import logging

from app.agents.cost_reviewer import (
    AlternativeScenario,
    CostReviewFinding,
    CostReviewResult,
)
from app.config import get_settings
from app.core.enums import FindingSeverity
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


_TOOL: dict = {
    "name": "report_consensus_findings",
    "description": (
        "Take two reviewers' finding sets and emit ALL findings, "
        "tagged by whether they are consensus (both reviewers "
        "raised) or minority (only one raised). Match findings by "
        "SEMANTIC OVERLAP, not exact text — the two reviewers will "
        "phrase the same issue differently. For each consensus "
        "finding, synthesize the strongest version from both "
        "reviewers' wording. For each minority finding, pass it "
        "through with the raising reviewer's wording. CRITICAL: "
        "minority findings (consensus=false) MUST have "
        "severity='MINOR' regardless of what the raising reviewer "
        "originally said — they lack a second-reviewer corroboration "
        "and may be hallucinations or low-confidence. Consensus "
        "findings keep the higher severity when reviewers disagree."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "description": (
                    "Consensus findings ordered by severity "
                    "(CRITICAL > MAJOR > MINOR). Each entry must "
                    "be a finding raised by BOTH reviewers about "
                    "the same underlying issue."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "consensus": {
                            "type": "boolean",
                            "description": (
                                "True iff BOTH reviewers raised "
                                "this finding about the same "
                                "underlying issue. False iff only "
                                "ONE reviewer raised it (minority)."
                            ),
                        },
                        "raised_by": {
                            "type": "string",
                            "enum": ["both", "primary", "secondary"],
                            "description": (
                                "Provenance: 'both' iff "
                                "consensus=true. 'primary' iff "
                                "consensus=false and only Reviewer "
                                "A raised it. 'secondary' iff "
                                "consensus=false and only Reviewer "
                                "B raised it."
                            ),
                        },
                        "severity": {
                            "type": "string",
                            "enum": [s.value for s in FindingSeverity],
                            "description": (
                                "For consensus findings: pick the "
                                "higher severity if the two "
                                "reviewers disagree (CRITICAL > "
                                "MAJOR > MINOR). For minority "
                                "findings (consensus=false): MUST "
                                "be MINOR regardless of the "
                                "raising reviewer's original "
                                "severity — uncorroborated flags "
                                "do not warrant CRITICAL or MAJOR "
                                "weight."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "description": ("Pick whichever category fits best from either reviewer."),
                        },
                        "subject": {
                            "type": "string",
                            "description": (
                                "Synthesize a clear short label "
                                "(3-8 words) from both reviewers' "
                                "subject lines."
                            ),
                        },
                        "finding_text": {
                            "type": "string",
                            "description": (
                                "Synthesize the finding text by "
                                "combining the strongest evidence "
                                "and quoted excerpts from both "
                                "reviewers. If both reviewers cite "
                                "different compliance items / "
                                "phases / numbers about the same "
                                "issue, include all of them — the "
                                "consensus is RICHER than either "
                                "alone. Specific, numeric, "
                                "audit-ready."
                            ),
                        },
                        "recommended_change": {
                            "type": "string",
                            "description": (
                                "Pick the simpler / more concrete "
                                "of the two reviewers' "
                                "recommendations, OR synthesize a "
                                "merged recommendation if they're "
                                "complementary. Quantified and "
                                "actionable."
                            ),
                        },
                        "scenarios_affected": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["LOW", "MEDIUM", "HIGH"],
                            },
                            "description": (
                                "Union of the two reviewers' "
                                "scenarios_affected — if either "
                                "raised it for a scenario, "
                                "include it."
                            ),
                        },
                        "alternative_scenarios": {
                            "type": "array",
                            "description": ("Combined alternatives from both reviewers, deduped."),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "total_price_usd": {
                                        "type": "number",
                                    },
                                    "rationale": {"type": "string"},
                                    "margin_delta_usd": {
                                        "type": "number",
                                    },
                                },
                                "required": ["label", "rationale"],
                            },
                        },
                    },
                    "required": [
                        "consensus",
                        "raised_by",
                        "severity",
                        "category",
                        "subject",
                        "finding_text",
                        "recommended_change",
                        "scenarios_affected",
                    ],
                },
            },
        },
        "required": ["findings"],
    },
}


_SYSTEM = """You are a Cost Review Consolidator. You receive findings from TWO independent Cost Reviewers (Reviewer A = primary, Reviewer B = secondary) that reviewed the same cost build. Your job is to emit ALL findings from both reviewers, properly tagged as consensus (both raised) or minority (only one raised), with severity adjusted accordingly.

CONSENSUS-DETECTION DISCIPLINE:
- Match findings by SEMANTIC OVERLAP, not exact text. The two reviewers WILL phrase the same issue differently. "Security Consultant under-staffed for NIST 800-53" and "650 hours insufficient for SSP development" are the SAME finding — both about under-allocated security labor for the NIST scope.
- Match by what's at issue, not by category labels. Reviewer A might call something "missed_scope"; Reviewer B might call it "phase_gap". If they're pointing at the same problem (training under-allocated, security tools missing, etc.), they're consensus.

CONSENSUS findings (both reviewers raised the same underlying issue):
- consensus=true, raised_by="both"
- severity = HIGHER of the two reviewers' severities (CRITICAL > MAJOR > MINOR). Federal cost-bid errors should err toward more scrutiny.
- Synthesize finding_text by combining the strongest evidence from both reviewers. If Reviewer A cites REQ-010 and Reviewer B cites REQ-014 for the same issue, include both — the consensus is RICHER than either alone.
- Synthesize recommended_change by picking the simpler/more concrete of the two, OR merging if they're complementary. One clean recommendation.

MINORITY findings (only ONE reviewer raised it):
- consensus=false, raised_by="primary" (Reviewer A only) or "secondary" (Reviewer B only)
- severity = "MINOR" — ALWAYS. Do not preserve the raising reviewer's original severity. Uncorroborated flags don't warrant CRITICAL or MAJOR weight even when the raising reviewer was confident — the second reviewer's silence is informative.
- Pass through the raising reviewer's wording for finding_text, subject, recommended_change, scenarios_affected, alternative_scenarios. Light cleanup is fine; do NOT invent new content.
- DO NOT drop minority findings. The user wants visibility into what each reviewer flagged independently, with the right "not corroborated" framing. Persisting as MINOR gives them visibility without elevating uncorroborated flags above genuine consensus.

OUTPUT — call the report_consensus_findings tool with EVERY finding from both reviewers, tagged consensus/minority. The total finding count should equal: (#consensus) + (#A-only) + (#B-only). Don't drop any. Don't fabricate any."""


_USER_TEMPLATE = """Identify consensus and minority findings across these two reviewers' outputs. Emit ALL findings, tagged.

=== Reviewer A (PRIMARY: {reviewer_a_model}) — {n_a} findings ===
{reviewer_a_block}

=== Reviewer B (SECONDARY: {reviewer_b_model}) — {n_b} findings ===
{reviewer_b_block}

Call report_consensus_findings now. Output every finding from both reviewers, with consensus=true/false and raised_by=both/primary/secondary tagged correctly. Minority findings (consensus=false) MUST have severity='MINOR'."""


def _minority_prefix(model_name: str, other_model: str) -> str:
    """Plaintext provenance prefix appended to finding_text on
    minority (single-reviewer) findings. Tells the user the flag
    came from one model and was downgraded to MINOR because the
    second model didn't corroborate."""
    return (
        f"[Single-reviewer flag from {model_name}; not corroborated "
        f"by {other_model}. Downgraded to MINOR - weigh accordingly.] "
    )


def _wrap_as_minority(
    findings: list[CostReviewFinding],
    raising_model: str,
    other_model: str,
) -> list[CostReviewFinding]:
    """Pass-through helper for the early-return path: when only one
    reviewer returned non-empty findings (the other returned empty),
    we skip the LLM and downgrade everything from the raising
    reviewer to MINOR with provenance prefix. The LLM has no useful
    work to do here — there's no second set to compare against."""
    out: list[CostReviewFinding] = []
    prefix = _minority_prefix(raising_model, other_model)
    for f in findings:
        out.append(
            CostReviewFinding(
                severity="MINOR",
                category=f.category,
                subject=f.subject,
                finding_text=prefix + (f.finding_text or ""),
                recommended_change=f.recommended_change,
                scenarios_affected=list(f.scenarios_affected),
                alternative_scenarios=list(f.alternative_scenarios),
            )
        )
    return out


def consolidate_cost_review_findings(
    *,
    proposal_id: int,
    reviewer_a_result: CostReviewResult,
    reviewer_a_model: str,
    reviewer_b_result: CostReviewResult,
    reviewer_b_model: str,
) -> CostReviewResult:
    """Run the consolidator. Returns a CostReviewResult containing
    BOTH consensus findings (raised by both reviewers, severity =
    higher of the two) AND minority findings (raised by one,
    severity forced to MINOR, finding_text prefixed with provenance).

    Returns empty only when neither reviewer raised any finding
    (clean build). When one reviewer is empty and the other has
    findings, skips the LLM and passes the survivor's findings
    through as MINOR-tagged minorities — there's nothing to
    consolidate against.
    """
    settings = get_settings()
    if not reviewer_a_result.findings and not reviewer_b_result.findings:
        log.info("cost_review_consolidator: both reviewers clean — skipping consolidator call")
        return CostReviewResult(findings=[])
    if not reviewer_a_result.findings:
        log.info(
            "cost_review_consolidator: reviewer A empty — "
            "passing %d B-only findings through as MINOR minorities",
            len(reviewer_b_result.findings),
        )
        return CostReviewResult(
            findings=_wrap_as_minority(
                reviewer_b_result.findings,
                reviewer_b_model,
                reviewer_a_model,
            )
        )
    if not reviewer_b_result.findings:
        log.info(
            "cost_review_consolidator: reviewer B empty — "
            "passing %d A-only findings through as MINOR minorities",
            len(reviewer_a_result.findings),
        )
        return CostReviewResult(
            findings=_wrap_as_minority(
                reviewer_a_result.findings,
                reviewer_a_model,
                reviewer_b_model,
            )
        )

    user_prompt = _USER_TEMPLATE.format(
        reviewer_a_model=reviewer_a_model,
        reviewer_b_model=reviewer_b_model,
        n_a=len(reviewer_a_result.findings),
        n_b=len(reviewer_b_result.findings),
        reviewer_a_block=_format_findings_for_consolidator(reviewer_a_result.findings),
        reviewer_b_block=_format_findings_for_consolidator(reviewer_b_result.findings),
    )

    tool_input, usage = call_tool_for_model(
        model=settings.model_cost_review_consolidator,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=12000,
        agent_name="cost_review_consolidator",
        proposal_id=proposal_id,
    )

    if usage.get("stop_reason") in ("max_tokens", "length"):
        n_partial = len(tool_input.get("findings") or [])
        raise RuntimeError(
            f"cost_review_consolidator: output truncated at "
            f"max_tokens (in={usage['input_tokens']}, "
            f"out={usage['output_tokens']}). Got {n_partial} partial "
            f"finding(s) before truncation. Bump max_tokens."
        )

    out: list[CostReviewFinding] = []
    n_consensus = 0
    n_minority_a = 0
    n_minority_b = 0
    for f in tool_input.get("findings") or []:
        try:
            scenarios = [
                str(s).upper()
                for s in (f.get("scenarios_affected") or [])
                if str(s).upper() in ("LOW", "MEDIUM", "HIGH")
            ]
            if not scenarios:
                scenarios = ["LOW", "MEDIUM", "HIGH"]

            consensus_flag = bool(f.get("consensus", True))
            raised_by = str(f.get("raised_by") or "").lower()
            if raised_by not in ("both", "primary", "secondary"):
                # Defensive: infer from consensus flag if the LLM
                # forgot or sent something off-spec. Default minority
                # to "primary" — without a model attribution we
                # have to pick one; the prefix still tells the user
                # it's uncorroborated.
                raised_by = "both" if consensus_flag else "primary"

            severity = str(f.get("severity") or "MINOR").upper()
            if severity not in (s.value for s in FindingSeverity):
                severity = "MINOR"
            # Hard rule: minority findings ALWAYS MINOR, regardless
            # of what the LLM emitted. Prompt instructs this; we
            # enforce it post-hoc as a safety net.
            if not consensus_flag:
                severity = "MINOR"

            alts: list[AlternativeScenario] = []
            for a in f.get("alternative_scenarios") or []:
                try:
                    total_p = a.get("total_price_usd")
                    margin_d = a.get("margin_delta_usd")
                    alts.append(
                        AlternativeScenario(
                            label=str(a["label"]),
                            total_price_usd=(float(total_p) if total_p is not None else None),
                            rationale=str(a.get("rationale") or ""),
                            margin_delta_usd=(float(margin_d) if margin_d is not None else None),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue

            finding_text = str(f.get("finding_text") or "")
            if not consensus_flag:
                if raised_by == "primary":
                    n_minority_a += 1
                    finding_text = (
                        _minority_prefix(
                            reviewer_a_model,
                            reviewer_b_model,
                        )
                        + finding_text
                    )
                else:  # "secondary"
                    n_minority_b += 1
                    finding_text = (
                        _minority_prefix(
                            reviewer_b_model,
                            reviewer_a_model,
                        )
                        + finding_text
                    )
            else:
                n_consensus += 1

            out.append(
                CostReviewFinding(
                    severity=severity,
                    category=str(f.get("category") or "consistency_issue"),
                    subject=str(f.get("subject") or "(no subject)"),
                    finding_text=finding_text,
                    recommended_change=str(f.get("recommended_change") or ""),
                    scenarios_affected=scenarios,
                    alternative_scenarios=alts,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "cost_review_consolidator: skipping malformed finding %r: %s",
                f,
                exc,
            )

    log.info(
        "cost_review_consolidator: a=%d findings + b=%d findings → "
        "%d total (%d consensus + %d A-only + %d B-only) (%s)",
        len(reviewer_a_result.findings),
        len(reviewer_b_result.findings),
        len(out),
        n_consensus,
        n_minority_a,
        n_minority_b,
        fmt_llm_usage(usage),
    )
    return CostReviewResult(findings=out)


def _format_findings_for_consolidator(
    findings: list[CostReviewFinding],
) -> str:
    """Compact rendering of one reviewer's findings for the
    consolidator's prompt. Numbered for easy reference; severity +
    category + subject + body + recommended_change + scenarios in
    a tight block."""
    if not findings:
        return "  (no findings)"
    rows: list[str] = []
    for i, f in enumerate(findings, 1):
        rows.append(f"  [{i}] {f.severity} · {f.category} · affects {','.join(f.scenarios_affected)}")
        rows.append(f"      subject: {f.subject}")
        body = (f.finding_text or "").strip()
        if len(body) > 600:
            body = body[:597] + "..."
        rows.append(f"      body: {body}")
        rec = (f.recommended_change or "").strip()
        if rec:
            if len(rec) > 300:
                rec = rec[:297] + "..."
            rows.append(f"      recommended: {rec}")
        rows.append("")
    return "\n".join(rows)


__all__ = ["consolidate_cost_review_findings"]
