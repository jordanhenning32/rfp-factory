"""Cost Reviewer — adversarial fact-check of the Cost Analyst's
H/M/L cost build.

The reviewer reads the full persisted cost build (3 scenarios with
labor lines, lifecycle phases, ODCs, indirect costs) plus the market
scan and compliance matrix, then surfaces findings about:

  - missed_scope: compliance items with no clear labor allocation
  - unrealistic_hours: under-staffed or over-staffed for the work
  - wage_band_misalignment: wrong seniority for the scope
  - margin_pressure: too thin or too thick vs market band
  - ceiling_violation: billed rate exceeds GSA OLM ceiling
  - phase_gap: required activity missing from any phase
  - odc_missing / odc_excessive: non-labor cost gaps
  - contract_type_mismatch: cost structure doesn't fit FFP/T&M/etc.
  - consistency_issue: data conflicts within the cost build

Single LLM call (Gemini 2.5 Pro by default) with forced tool use.
The agent returns CostReviewFinding-shaped data; the orchestrator
persists each finding to one or more cost_review_findings rows
(one row per affected scenario, since the FK is per-scenario).

Re-running the reviewer REPLACES existing findings for the proposal
— the FK cascade clears stale rows automatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import get_settings
from app.core.enums import FindingSeverity
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


# ---- Output dataclasses ---------------------------------------------------


@dataclass
class AlternativeScenario:
    """One alternative-scenario suggestion attached to a finding.
    The reviewer says 'instead of bidding $X with this team, you
    could bid $Y by doing Z.' Persisted as JSON inside
    CostReviewFinding.alternative_scenarios_json."""

    label: str
    total_price_usd: float | None
    rationale: str
    margin_delta_usd: float | None  # vs current scenario; None if not estimable


@dataclass
class CostReviewFinding:
    """One finding from the Cost Reviewer. Affects one or more
    scenarios — orchestrator persists per-scenario rows."""

    severity: str  # FindingSeverity value (CRITICAL/MAJOR/MINOR)
    category: str
    subject: str
    finding_text: str
    recommended_change: str
    scenarios_affected: list[str]  # subset of [LOW, MEDIUM, HIGH]
    alternative_scenarios: list[AlternativeScenario] = field(
        default_factory=list,
    )


@dataclass
class CostReviewResult:
    """Full output of one review run. Ready for persistence."""

    findings: list[CostReviewFinding] = field(default_factory=list)


# ---- Tool schema ----------------------------------------------------------

_FINDING_CATEGORIES = (
    "missed_scope",
    "unrealistic_hours",
    "wage_band_misalignment",
    "margin_pressure",
    "ceiling_violation",
    "phase_gap",
    "odc_missing",
    "odc_excessive",
    "contract_type_mismatch",
    "consistency_issue",
)


_TOOL: dict = {
    "name": "report_cost_review_findings",
    "description": (
        "Report adversarial findings about the cost build. Each "
        "finding identifies a SPECIFIC issue with quoted excerpts "
        "from the structured input — missed scope items, "
        "unrealistic hour estimates, margin-vs-market tension, "
        "ceiling violations, phase gaps, ODC issues, contract-type "
        "mismatches, or internal inconsistencies. Each finding "
        "indicates which scenarios are affected (LOW/MEDIUM/HIGH) "
        "and may include alternative-scenario suggestions. If the "
        "cost build is solid, return an empty findings array — DO "
        "NOT fabricate findings to look thorough; false positives "
        "erode trust in the reviewer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "description": (
                    "Findings ordered by severity (CRITICAL first, "
                    "then MAJOR, then MINOR). Empty array when the "
                    "cost build is clean. Typical run produces "
                    "0-8 findings on a sub-$2M bid; >12 findings "
                    "indicates the bid has fundamental issues "
                    "the analyst should address before the writer "
                    "drafts narrative."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": [s.value for s in FindingSeverity],
                            "description": (
                                "CRITICAL — the bid as proposed will "
                                "almost certainly be rejected, lose "
                                "money, or violate compliance. "
                                "Examples: ceiling_violation that "
                                "would void a labor row; missed_"
                                "scope on a mandatory deliverable; "
                                "margin below the policy floor "
                                "(walk-away territory). MAJOR — a "
                                "real issue an evaluator will catch "
                                "and ding (under-staffed phase, "
                                "wrong seniority, ODC missing). "
                                "MINOR — refinement opportunity "
                                "(slightly padded hours, ODC "
                                "justification too thin)."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": list(_FINDING_CATEGORIES),
                            "description": (
                                "Pick the BEST-fit category. "
                                "missed_scope: compliance item with "
                                "no labor allocation. unrealistic_"
                                "hours: under/over-staffed. wage_"
                                "band_misalignment: wrong seniority. "
                                "margin_pressure: above market band "
                                "high or below floor. ceiling_"
                                "violation: billed rate > GSA "
                                "ceiling. phase_gap: required "
                                "activity not in any phase. odc_"
                                "missing: non-labor cost expected "
                                "but not in build. odc_excessive: "
                                "ODC seems padded. contract_type_"
                                "mismatch: cost structure wrong for "
                                "FFP/T&M/cost-plus. consistency_"
                                "issue: data conflicts within the "
                                "build."
                            ),
                        },
                        "subject": {
                            "type": "string",
                            "description": (
                                "Short label (3-8 words) summarizing "
                                "WHAT the finding is about. E.g., "
                                "'NIST 800-53 evidence package "
                                "scope', 'SE III ceiling violation', "
                                "'Test Engineer hours below quarterly "
                                "508 cycle'. Used as the finding's "
                                "row header in the UI."
                            ),
                        },
                        "finding_text": {
                            "type": "string",
                            "description": (
                                "Full description with QUOTED "
                                "excerpts from the structured input. "
                                "Format: 'The cost build allocates "
                                "X hours to [category] across all "
                                "phases, but compliance item REQ-051 "
                                "requires [activity] on a quarterly "
                                "cycle. At 4 cycles × Y hours each, "
                                "minimum [activity] hours are Z, "
                                "leaving the bid under-staffed by "
                                "[Z - X] hours.' Specific, numeric, "
                                "and grounded in the input. Vague "
                                "findings ('seems light on testing') "
                                "are not actionable — be concrete."
                            ),
                        },
                        "recommended_change": {
                            "type": "string",
                            "description": (
                                "ONE specific actionable fix the "
                                "user can apply to the cost build "
                                "to address this finding. Concrete "
                                "and quantified. EXAMPLES: "
                                "'Increase Security Consultant "
                                "hours from 650 to 900 across "
                                "Phases 2 and 4 to cover NIST SSP "
                                "development.' / 'Drop Software "
                                "Engineer III salary from $170k "
                                "to $145k — billing rate falls "
                                "below the $159.72 GSA ceiling and "
                                "saves ~$25K loaded cost without "
                                "changing FTE count.' / 'Drop "
                                "MEDIUM scenario margin from 25% to "
                                "22% — proposed price falls back "
                                "into the $1.2M market band high.' "
                                "If multiple fixes are valid, pick "
                                "the SIMPLEST one for "
                                "recommended_change and put others "
                                "in alternative_scenarios."
                            ),
                        },
                        "scenarios_affected": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["LOW", "MEDIUM", "HIGH"],
                            },
                            "description": (
                                "Subset of [LOW, MEDIUM, HIGH]. "
                                "Most findings affect all three "
                                "scenarios since they share a labor "
                                "estimate. Margin-related findings "
                                "may be scenario-specific (e.g., "
                                "'LOW only — margin below floor'). "
                                "At least one scenario."
                            ),
                        },
                        "alternative_scenarios": {
                            "type": "array",
                            "description": (
                                "Optional. Alternative bid postures "
                                "that would address this finding. "
                                "E.g., 'bid SE II at $145K instead "
                                "of SE III at $170K — saves ~$25K, "
                                "still meets scope.' Empty when no "
                                "obvious alternative exists."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "total_price_usd": {
                                        "type": "number",
                                        "description": (
                                            "Estimated price of "
                                            "this alternative. Omit "
                                            "the field entirely if "
                                            "not estimable — do "
                                            "NOT include with a "
                                            "null/zero placeholder."
                                        ),
                                    },
                                    "rationale": {"type": "string"},
                                    "margin_delta_usd": {
                                        "type": "number",
                                        "description": (
                                            "Estimated change in "
                                            "PROFIT (positive = more "
                                            "profit, negative = "
                                            "less) vs the current "
                                            "scenario. Omit the "
                                            "field if not estimable."
                                        ),
                                    },
                                },
                                "required": ["label", "rationale"],
                            },
                        },
                    },
                    "required": [
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


# ---- System prompt --------------------------------------------------------

_SYSTEM = """You are the Cost Reviewer for Quadratic Digital — an adversarial fact-check on the Cost Analyst's H/M/L cost build before submission. Your job is to find PROBLEMS the analyst missed, not to validate the build. Cost mistakes on federal proposals are unrecoverable post-submit, so be skeptical and specific.

REVIEW DISCIPLINE:
- Read the FULL structured input (compliance matrix, market scan, all 3 scenarios with labor lines + phases + ODCs + indirect, technical drafts) before producing findings. Quote specific items by ID and field.
- Be ADVERSARIAL but FAIR. Real findings only. Do not fabricate findings to look thorough — false positives waste the user's time and erode trust. If the cost build is solid, return an empty findings array. The bid TEAM did the work; you're a final-pass check.
- Severity must match consequence. CRITICAL = the bid almost certainly fails / loses money / violates compliance. MAJOR = a real evaluator-visible issue that should be fixed. MINOR = polish opportunity.
- Cite NUMBERS. "Test Engineer III has 780 total hours, but compliance item REQ-051 mandates 4 quarterly 508-accessibility cycles at ~120 hrs each = 480 hrs; the remaining 300 hrs are too thin for full regression testing across 4 portals" beats "Testing seems light".
- Cross-reference scope to staffing. For each compliance item that requires labor, identify which labor category is delivering it. Items with no clear delivery owner are missed_scope findings.
- Cross-reference phase narrative to scope. Required activities (NIST 800-53 control evidence package, IAM federation testing, training delivery) should appear in phase descriptions or labor allocations. Missing activities are phase_gap findings.

WHAT TO LOOK FOR:
1. MISSED SCOPE — compliance items with no clear labor allocation. Walk every TECHNICAL / MANAGEMENT compliance item; ask "who's delivering this? at what hours?" Items that are required but not staffed are findings.
2. UNREALISTIC HOURS — too few hours for the stated scope (under-bid, will lose money or fail to deliver) OR too many hours (over-bid, will lose to competition). Check phase hours against typical scope footprint.
3. SALARY MISALIGNMENT — Senior Tech Lead bidding routine integration work; BA II being the ONLY analyst on a complex requirements-gathering effort. Mismatch with role complexity.
4. MARGIN PRESSURE — proposed price ABOVE the market band high implies pricing out (rare wins, especially small biz vs incumbents). Below floor margin (18%) is walk-away. Compare to specific competitor rates from market_scan.
5. CEILING VIOLATIONS — billed rate exceeding GSA OLM ceiling per labor_catalog. The cost build flags these in line rationale; surface them as findings.
6. PHASE GAPS — required activities (security control evidence, training, deployment readiness reviews, transition support) missing from phase descriptions or under-allocated.
7. ODC GAPS — for federal IT services, expected ODCs include cloud hosting (if applicable), third-party security testing, software licenses, training materials, possibly travel. Missing categories or implausibly low amounts are findings.
8. CONTRACT TYPE MISMATCH — FFP requires complete-scope cost realism; T&M requires ceiling rate competitiveness; cost-plus requires DCAA-compliant indirect rates. Findings when the cost structure doesn't fit the contract type.
9. INTERNAL CONSISTENCY — labor totals between phases vs labor_lines should reconcile (the system reports allocation balance; discrepancies are findings).

WHAT NOT TO WRITE:
- Generic findings with no quoted data ("Testing seems light", "Hours look high"). Be specific.
- Findings derived from world knowledge not in the input ("Federal CMS bids typically use X" — unless that statement is itself a finding about insufficient context). Stick to what's provided.
- Compliments / "this is good" entries. Findings are PROBLEMS only. Empty array is the correct output for a clean build.
- Wishlist features (security training, war games, on-site presence) when they're not RFP requirements.

EVERY FINDING NEEDS A RECOMMENDED CHANGE:
- Each finding MUST include a recommended_change — one concrete actionable fix the user can apply. "Increase X hours from N to M" / "Drop salary from A to B" / "Add ODC for [item]" / "Cut margin from X% to Y%". Quantified and specific.
- Pick the SIMPLEST recommendation. Multi-step or trade-off-heavy alternatives go in alternative_scenarios. The recommended_change is what the user does first.
- Do not write recommended_change as another critique — it's the FIX, not a restatement of the problem.

OUTPUT — call the report_cost_review_findings tool with the structured findings array. Order by severity (CRITICAL > MAJOR > MINOR)."""


# ---- User template --------------------------------------------------------

_USER_TEMPLATE = """Review this cost build for issues.

=== RFP context ===
Title: {rfp_title}
Customer agency: {rfp_agency}
Period of performance: ~{pop_months} months
Contract type signal: {contract_type_signal}

=== Compliance matrix (scope to be delivered) ===
{compliance_block}

=== Market scan (band + competitors) ===
{market_scan_block}

=== Cost build — proposed scenario ({proposed_scenario}) ===
{cost_build_block}

=== Lifecycle phases (proposed scenario) ===
{phases_block}

=== Other Direct Costs ===
{odcs_block}

=== Other scenarios (for cross-scenario margin/coverage check) ===
{other_scenarios_block}

=== Technical-section drafts (what the team is proposing to deliver) ===
{drafts_block}

=== Internal pricing methodology ===
{methodology_block}

=== Quadratic profile ===
{quadratic_summary}

Call report_cost_review_findings now. Empty array when the cost build is clean."""


# ---- Public entry point ---------------------------------------------------


@dataclass
class CostReviewerInputs:
    """Canonical inputs the orchestrator gathers and hands to the agent."""

    rfp_title: str
    rfp_agency: str
    pop_months: int
    contract_type_signal: str
    proposed_scenario: str
    compliance_block: str
    market_scan_block: str
    cost_build_block: str
    phases_block: str
    odcs_block: str
    other_scenarios_block: str
    drafts_block: str
    methodology_block: str
    quadratic_summary: str


def review_cost_build(
    *,
    proposal_id: int,
    inputs: CostReviewerInputs,
    model: str | None = None,
) -> CostReviewResult:
    """Run one Cost Reviewer LLM pass. Returns a structured result.
    Caller MUST handle exceptions — Pro models can fail on rate
    limits, tool-call truncation, or API availability.

    `model` defaults to settings.model_cost_reviewer (Gemini 2.5
    Pro). The orchestrator runs this twice — once with the primary
    model and once with model_cost_reviewer_secondary (GPT-5.5) —
    then consolidates findings via cost_review_consolidator before
    persisting."""
    settings = get_settings()
    chosen_model = model or settings.model_cost_reviewer
    user_prompt = _USER_TEMPLATE.format(
        rfp_title=inputs.rfp_title or "(untitled)",
        rfp_agency=inputs.rfp_agency or "(unknown)",
        pop_months=inputs.pop_months,
        contract_type_signal=(inputs.contract_type_signal or "(unknown)"),
        proposed_scenario=inputs.proposed_scenario,
        compliance_block=inputs.compliance_block or "(no compliance items)",
        market_scan_block=inputs.market_scan_block or "(no market scan)",
        cost_build_block=inputs.cost_build_block,
        phases_block=inputs.phases_block,
        odcs_block=inputs.odcs_block,
        other_scenarios_block=inputs.other_scenarios_block,
        drafts_block=inputs.drafts_block or "(no drafts available)",
        methodology_block=inputs.methodology_block,
        quadratic_summary=inputs.quadratic_summary or "(no profile)",
    )

    # agent_name embeds the model so agent_runs cost tracking can
    # tell the two parallel passes apart in the Spend dashboard.
    agent_name = f"cost_reviewer:{chosen_model}"
    tool_input, usage = call_tool_for_model(
        model=chosen_model,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=12000,
        agent_name=agent_name,
        proposal_id=proposal_id,
    )

    if usage.get("stop_reason") in ("max_tokens", "length"):
        # Same silent-zero defense as the other agents — partial
        # findings array would silently mean "clean build" if we
        # parsed it, which is wrong.
        n_partial = len(tool_input.get("findings") or [])
        raise RuntimeError(
            f"cost_reviewer: output truncated at max_tokens "
            f"(in={usage['input_tokens']}, "
            f"out={usage['output_tokens']}). "
            f"Got {n_partial} partial finding(s) before truncation. "
            f"Bump max_tokens or split inputs."
        )

    findings: list[CostReviewFinding] = []
    for f in tool_input.get("findings") or []:
        try:
            scenarios = [
                str(s).upper()
                for s in (f.get("scenarios_affected") or [])
                if str(s).upper() in ("LOW", "MEDIUM", "HIGH")
            ]
            if not scenarios:
                # Default to all three when agent didn't specify —
                # most findings are proposal-wide.
                scenarios = ["LOW", "MEDIUM", "HIGH"]
            severity = str(f.get("severity") or "MINOR").upper()
            if severity not in (s.value for s in FindingSeverity):
                severity = "MINOR"

            alternatives: list[AlternativeScenario] = []
            for a in f.get("alternative_scenarios") or []:
                try:
                    total_p = a.get("total_price_usd")
                    margin_d = a.get("margin_delta_usd")
                    alternatives.append(
                        AlternativeScenario(
                            label=str(a["label"]),
                            total_price_usd=(float(total_p) if total_p is not None else None),
                            rationale=str(a.get("rationale") or ""),
                            margin_delta_usd=(float(margin_d) if margin_d is not None else None),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning(
                        "cost_reviewer: skipping malformed alternative scenario %r: %s",
                        a,
                        exc,
                    )

            findings.append(
                CostReviewFinding(
                    severity=severity,
                    category=str(f.get("category") or "consistency_issue"),
                    subject=str(f.get("subject") or "(no subject)"),
                    finding_text=str(f.get("finding_text") or ""),
                    recommended_change=str(f.get("recommended_change") or ""),
                    scenarios_affected=scenarios,
                    alternative_scenarios=alternatives,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "cost_reviewer: skipping malformed finding %r: %s",
                f,
                exc,
            )

    log.info(
        "cost_reviewer (%s): proposal %d — %d findings (%s)",
        chosen_model,
        proposal_id,
        len(findings),
        fmt_llm_usage(usage),
    )
    return CostReviewResult(findings=findings)


__all__ = [
    "AlternativeScenario",
    "CostReviewFinding",
    "CostReviewResult",
    "CostReviewerInputs",
    "review_cost_build",
]
