"""Cost Volume Writer — drafts cost-deferred narrative sections from
the Cost Analyst's structured output.

Single LLM call per cost-deferred section (Sonnet 4.6 by default).
Returns the same SectionDraft shape as the existing Writer Team so
the existing persist_section_draft path works unchanged. The Review-
Revise auto-loop, manual regenerate, and exclude-from-draft toggles
all operate uniformly on the cost sections once they're drafted.

Key invariant: NO FABRICATED NUMBERS. Every dollar value in the
draft must come from the structured cost build the orchestrator
provides. The reviewer's downstream citation_check + grounding
checks fact-check this; here we frame the prompt to make it hard
to drift.

Scenario semantics: by default the agent writes the MEDIUM (target)
scenario as "the proposed price". The LOW and HIGH scenarios are
included as risk-context for the narrative ("if scope expands by
N%, our HIGH scenario absorbs the impact at $X"). Future UI work
can let the user override the proposed scenario.

Cached prefix: holds the static-across-sections cost data — full
labor lines, market scan summary, executive summary, internal
methodology. Per-section content (section_id, brief, related
compliance items, page/word limits) lives in the user prompt so
the prefix stays warm across the cost-deferred sections.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.agents.writer_team import SectionDraft
from app.config import get_settings
from app.services.llm import call_tool_for_model, fmt_llm_usage
from app.services.service_line import (
    SERVICE_LINE_IT_SERVICES,
    SERVICE_LINE_PAYMENT_SYSTEMS,
)

log = logging.getLogger(__name__)


# Scenario the customer sees. The other two are surfaced as risk
# context only. UI override is a future capability.
DEFAULT_PROPOSED_SCENARIO = "MEDIUM"


# ---- Tool schema ----------------------------------------------------------

_TOOL: dict = {
    "name": "draft_cost_section",
    "description": (
        "Draft ONE cost-deferred narrative section. Return clean "
        "markdown for the section body, with citations to the "
        "structured cost build for every dollar value. Do NOT "
        "fabricate any numbers — every dollar in the draft must "
        "come from the COST_BUILD block in your input. If the "
        "section needs a number you don't have, surface it as a "
        "needs_human_placeholders entry instead of inventing one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "draft_text_markdown": {
                "type": "string",
                "description": (
                    "The drafted section body in clean markdown. "
                    "Open with the evaluation-criterion citation if "
                    "the section maps to one (matching the existing "
                    "Writer Team convention). Use markdown tables "
                    "where pricing detail benefits — e.g., a Basis "
                    "of Estimate table with FTE + hours + loaded "
                    "rate + billed total per labor category. Honor "
                    "the page/word limit; cost narrative is "
                    "already terse by convention."
                ),
            },
            "citations": {
                "type": "array",
                "description": (
                    "One entry per dollar value or factual claim in "
                    "the draft. Source-prefix conventions:\n"
                    "  - Scenario-level cost build: 'cost_build:"
                    "<scenario>:<field>' (e.g., 'cost_build:MEDIUM:"
                    "total_proposed_price_usd', 'cost_build:MEDIUM:"
                    "indirect_costs.contingency_cost_usd').\n"
                    "  - Per-line labor: 'cost_build:<scenario>:"
                    "labor.<category>' (e.g., 'cost_build:MEDIUM:"
                    "labor.Software Engineer III').\n"
                    "  - Per-PHASE values: 'cost_build:<scenario>:"
                    "phase:<phase_name>:<field>' (e.g., 'cost_build:"
                    "MEDIUM:phase:Discovery & Planning:price', "
                    "'cost_build:MEDIUM:phase:Build & Test:hours').\n"
                    "  - Per-phase labor allocation: 'cost_build:"
                    "<scenario>:phase:<phase_name>:labor.<category>' "
                    "(e.g., 'cost_build:MEDIUM:phase:Build & Test:"
                    "labor.Software Engineer III').\n"
                    "  - Market band claims: 'market_scan:<field>' "
                    "(e.g., 'market_scan:band_high_usd').\n"
                    "  - Methodology claims: 'internal_pricing_rules:"
                    "<key>' (e.g., 'internal_pricing_rules:"
                    "wrap_rate_components').\n"
                    "  - ODCs: 'cost_build:<scenario>:odcs.<item>' "
                    "(e.g., 'cost_build:MEDIUM:odcs.Cloud hosting').\n"
                    "The reviewer will fact-check these against the "
                    "actual data — be specific."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "source": {"type": "string"},
                        "source_section": {
                            "type": ["string", "null"],
                            "description": (
                                "OMIT (return null) unless you have an exact "
                                "section number or page reference."
                            ),
                        },
                    },
                    "required": ["claim", "source"],
                },
            },
            "needs_human_placeholders": {
                "type": "array",
                "description": (
                    "One entry per [NEEDS_HUMAN: …] marker in the "
                    "draft. Schema MUST match the Writer Team's so "
                    "the Needs Human Input tab + resolve_placeholder "
                    "service can act on cost-section placeholders the "
                    "same way they do for narrative-section ones."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "marker_text": {
                            "type": "string",
                            "description": (
                                "Exact text inside the [NEEDS_HUMAN: …] "
                                "brackets in the draft. The UI does "
                                "literal string replacement when the "
                                "user resolves the placeholder, so "
                                "this MUST match the bracket content "
                                "exactly."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": ("One short sentence: what input is needed. No meta-explanation."),
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "pricing",
                                "schedule_commitment",
                                "teaming_confirmation",
                                "specific_personnel",
                                "specific_numbers",
                                "policy_decision",
                                "signature",
                                "other",
                            ],
                            "description": (
                                "Bucket for the Needs Human Input tab. "
                                "Cost narratives most often produce "
                                "'pricing' or 'specific_numbers'."
                            ),
                        },
                    },
                    "required": [
                        "marker_text",
                        "description",
                        "category",
                    ],
                },
            },
            "shortfall_mitigations_applied": {
                "type": "array",
                "description": (
                    "Empty for cost sections in the typical case — "
                    "shortfall narrative belongs in technical "
                    "sections. Include here only if the cost "
                    "narrative explicitly leverages a teaming/"
                    "subcontractor mitigation that affects pricing."
                ),
                "items": {"type": "string"},
            },
        },
        "required": [
            "draft_text_markdown",
            "citations",
            "needs_human_placeholders",
            "shortfall_mitigations_applied",
        ],
    },
}


# ---- System prompt --------------------------------------------------------

_SYSTEM = """You are the Cost Volume Writer for Quadratic Digital. You draft cost-deferred narrative sections (Cost Volume, Basis of Estimate, Pricing Narrative, Cost Realism) for federal proposals. Numbers come from the structured COST_BUILD block in your input — you NEVER invent a dollar value, and you NEVER perform arithmetic. If a number isn't in the COST_BUILD, surface it as a needs_human_placeholders entry.

WRITING DISCIPLINE:
- Cost narrative is audit-ready, not marketing. Federal cost evaluators read for cost realism + completeness + reasonableness. They penalize marketing fluff and unsupported claims. Defensible and specific beats persuasive and vague.
- Mirror evaluation criteria when the section maps to one. Open with the criterion-citation format the existing Writer Team uses ("This section targets [Criterion ID] ([criterion name], [weight]) — [one-line restatement]") if the section_brief includes a target criterion.
- Use the customer's contract type to frame the narrative. Fixed-price → emphasize completeness (we've covered the scope) and cost realism (our hours are realistic). T&M → emphasize ceiling pricing and rate competitiveness. Cost-plus → emphasize transparent indirect rates and DCAA-compliant cost allocation.
- Tables are appropriate for Basis of Estimate, labor cost breakdowns, ODCs, and rate cards. Use markdown table syntax. For each row in a table, every dollar value must be traceable to the COST_BUILD.
- Quadratic's competitive edge is AI-accelerated custom delivery — leaner team, faster cycles, COTS-like delivery speed without COTS rigidity. Reference this where it strengthens cost-realism arguments (e.g., "Our 3.5-FTE team delivers what comparable pursuits staff at 5+ because of AI-assisted code generation and our agentic proposal-development pipeline").
- Risk-driven contingency (HIGH scenario) is included for transparency, not bid posture. The proposed price is the MEDIUM scenario unless a different scenario is explicitly designated. Do not bid LOW or HIGH unless told.
- Position vs market: be honest. If the proposed price is ABOVE the market band, defend the delta with scope or quality differentiators. If BELOW, emphasize cost-realism (we've covered the scope without padding). If IN_BAND, note the alignment as evidence of reasonableness.

MULTI-YEAR / OPTION-YEAR PRICING (when pop_months > 12 OR the RFP references option years):
- The COST_BUILD's totals are BASE YEAR (Year 1) numbers. When the RFP requires multi-year or option-year pricing (Years 2-5, Year 4-5 optional, etc.), you MUST render a fully priced multi-year table in the narrative — do NOT defer this to the human and do NOT emit a [NEEDS_HUMAN] placeholder for option-year pricing, escalation rate, or multi-year table format.
- Apply the canonical default labor escalation: `default_annual_labor_escalation_rate` from the cached internal pricing methodology block (3% unless overridden by the RFP). State it explicitly in the narrative ("Years 2+ priced with a 3% annual labor escalation aligned with federal IT services norms").
- Year-by-year compute (per scenario):
    Year N labor revenue = Year 1 labor revenue × (1 + escalation_rate)^(N-1)
    Year N ODCs = sum of (odc.amount × 1) for ODCs whose year_count covers Year N (a year_count=3 ODC contributes in Years 1-3, not 4-5; an "as-needed" ODC repeats only when year_count says so)
    Year N total = (Year N labor revenue + Year N ODCs) × scenario indirect/margin treatment already in COST_BUILD
- Render a Years 1-N table: Year | Labor | ODCs | Indirect/margin | Total. Footnote with the escalation rate + the source ("3% annual labor escalation per Quadratic's internal pricing methodology, consistent with federal IT services BLS ECI norms; ODCs from COST_BUILD per item-level year_count").
- Cite source='cost_build:<scenario>:multi_year_table:year:<N>' for each row.
- If the RFP specifies a different escalator (e.g., CPI-U-tied, fixed 2%), USE THAT instead. Acknowledge in prose ("per Section X.Y of the RFP, Year 2+ pricing uses [escalator]").
- Attachment D / multi-year cost form prompts: if the RFP references a specific cost form, render the same numbers as a markdown table mirroring the form's columns. The form template itself stays a Submission Checklist item; the narrative provides the priced numbers in advance.

PHASE-BY-PHASE BASIS OF ESTIMATE (when phases are present in the COST_BUILD):
- Federal cost-proposal convention expects a phased BOE for non-trivial work. When the COST_BUILD includes "Lifecycle phases", USE THEM. Do NOT skip the phase view in favor of a labor-category-only BOE.
- Open the Basis of Estimate with a phase summary table: phase name | months | hours | price. Every row's numbers cite back to the corresponding phase entry.
- Then provide one short subsection per phase covering: (1) phase intent — paraphrase the phase description; (2) staffing approach — which labor categories are allocated and why this composition fits the phase work; (3) phase total cost and price.
- Cite phase-level numbers using the format: source='cost_build:<scenario>:phase:<phase_name>:<field>' (e.g., 'cost_build:MEDIUM:phase:Discovery & Planning:price', 'cost_build:MEDIUM:phase:Build & Test:hours'). For per-phase labor allocations: source='cost_build:<scenario>:phase:<phase_name>:labor.<category>'.
- IMPORTANT: phase prices include labor + per-phase G&A + per-phase contingency at margin, but DO NOT include ODCs. ODCs are proposal-level (Cloud hosting, plugin licenses, training materials, etc.) and are added once at the scenario level. So: sum_of_phase_prices ≈ labor revenue ≠ total proposed price. The total proposed price = sum_of_phase_prices + (ODCs / (1 - margin_pct)) + small rounding. Do NOT write "phase prices sum to the proposed total" — they sum to the LABOR portion. If you need a check figure, write something like "Phase prices total $X (labor + indirect at margin); ODCs of $Y add a further $Z; the proposed price is $W."
- The labor-category BOE table (the older view) STILL has narrative value — use it as the cross-cut companion to the phase view, not the primary BOE structure. Federal evaluators want both: phase view shows TIME-PHASED cost realism; category view shows LABOR-MIX realism.

REQUIREMENT-DRIVEN COST CONTENT YOU MUST PROVIDE — DO NOT DEFER TO HUMAN:
The cost-deferred sections you draft commonly trigger four pitfalls where the easy move is to emit a [NEEDS_HUMAN] placeholder. Do NOT emit placeholders for any of the following — the cost build + internal methodology already give you everything needed to write a defensible answer.

1. OPTION-YEAR / MULTI-YEAR PRICING — see the dedicated rules above. Apply the default 3% annual labor escalation, render a fully priced Years 1-N table, surface ODCs per their year_count.

2. AGENCY COST FORM REPLICAS (Attachment D, Pricing Schedule, Cost Worksheet, etc.) — when an RFP references a specific cost form, render a markdown table inside the narrative that mirrors the form's columns and contains the SAME numbers from COST_BUILD. The user fills the actual form template separately as a Submission Checklist item — your job is to produce the priced numbers, not the empty template. Do NOT write "the actual form will be completed by the team with access to the template" — that's deferral and evaluators read it as unreadiness.

3. RFP-SPECIFIED FEE ABSORPTION (E-Procurement fees, transaction fees, vendor management fees, payment processing fees, GSA IFF, agency-imposed surcharges) — when an RFP requires the vendor to absorb a fee at a specific percentage, ASSUME by default that the proposed price already includes the fee absorbed (this is the most defensible position and the federal/state norm). Write something concrete: "Per REQ-XXX, the 1.75% E-Procurement transaction fee is absorbed by Quadratic Digital and is included in the proposed price of $X — no separate fee line is required." If the COST_BUILD shows the fee was NOT actually grossed up, still default to absorbed-and-included framing rather than punting; the math reconciles in indirect/margin treatment.

4. OPTIONAL SERVICES / ADD-ON PRICING — when the section brief references "optional services" or "additional services" as a required cost table element AND the COST_BUILD doesn't enumerate specific add-ons, render a brief framework table with the categories the RFP scope implies (e.g., "Additional portal customization", "Expanded penetration testing", "Content migration assistance", "Enhanced support tiers") priced at the proposed scenario's blended labor rate × estimated hours, with a "Priced on engagement scope at the rates in this volume" footnote. Do NOT defer to the human — federal evaluators expect a structure to be in place even when individual line items are TBD.

When in genuine doubt (RFP truly silent AND no internal methodology covers it), THEN emit a [NEEDS_HUMAN] placeholder — but exhaust the rules above first.

WHAT NOT TO WRITE:
- Marketing superlatives ("world-class", "best-in-class", "industry-leading"). Federal evaluators score these down.
- Unsupported claims about competitors. The market scan shows specific firms and rate inferences — cite those by name; don't fabricate competitive positioning.
- Specific rates, hours, or totals NOT in COST_BUILD. Every dollar must trace back. If you need a number that isn't there, write a needs_human_placeholders entry.
- Promises about cost certainty that the contract type doesn't support. Fixed-price guarantees the price; T&M guarantees the rate, not the total.

OUTPUT — call the draft_cost_section tool. NO PREAMBLE. The draft_text_markdown field is the section body that goes into the proposal."""


# ---- User template --------------------------------------------------------

_USER_TEMPLATE = """Draft this cost-deferred section.

=== Section ===
section_id: {section_id}
section_title: {section_title}
section_order: {section_order}
section_brief:
\"\"\"
{section_brief}
\"\"\"
page_limit: {page_limit}
word_limit: {word_limit}
compliance_items_addressed: {compliance_items}

=== Compliance requirements relevant to this section ===
{compliance_text}

=== RFP context ===
RFP title: {rfp_title}
Customer agency: {rfp_agency}
Period of performance: ~{pop_months} months
Contract type signal (from compliance/scope): {contract_type_signal}

=== Sibling sections (for cross-reference, do not duplicate their content) ===
{outline_snippet}
"""


# Cached-prefix template — shared across all cost-deferred sections in
# one Cost Volume Writer run. Holds the cost build, market scan, and
# Quadratic context. Per-section content lives in the user prompt.
_CACHED_PREFIX_TEMPLATE = """=== COST_BUILD (single source of truth — every dollar in your draft must trace here) ===
PROPOSED scenario: {proposed_scenario}

--- LOW scenario (Competitive: low coverage, 18% margin, no contingency) ---
{low_scenario_block}

--- MEDIUM scenario (Target: high coverage, 25% margin, 5% contingency) ---
{medium_scenario_block}

--- HIGH scenario (Protective: high coverage, 30% margin, 10% contingency) ---
{high_scenario_block}

--- Labor lines (same structure across scenarios; computed values per scenario shown above) ---
{labor_lines_block}

--- Lifecycle phases (Basis of Estimate by phase, for the proposed scenario) ---
{phases_block}

--- Other Direct Costs (ODCs) ---
{odcs_block}

--- Subcontractor passthrough ---
{subcontractor_block}

=== MARKET_SCAN ===
{market_scan_block}

=== EXECUTIVE_SUMMARY (from Cost Analyst — narrative spine for this section) ===
{executive_summary}

=== INTERNAL_PRICING_METHODOLOGY (for transparency claims) ===
{methodology_block}

=== QUADRATIC profile ===
{quadratic_summary}
"""


# ---- Public entry point ---------------------------------------------------


@dataclass
class CostWriterContext:
    """All the static-across-sections data the agent needs. Built
    once by the orchestrator, passed into each draft call.

    `pricing_packages_snapshot` — list of 3 dicts (LOW/MEDIUM/HIGH)
    in the shape returned by services.pricing.get_pricing_packages_
    snapshot. Includes the per-scenario lines.
    `market_scan_snapshot` — dict from services.market_scan.get_
    market_scan_snapshot. May be None if no scan was run; agent
    gracefully omits market commentary when so.
    """

    pricing_packages_snapshot: list[dict[str, Any]]
    market_scan_snapshot: dict[str, Any] | None
    executive_summary: str
    quadratic_summary: str
    proposed_scenario: str = DEFAULT_PROPOSED_SCENARIO
    contract_type_signal: str = "(unknown — likely fixed-price for state IT services)"
    # Service-line tag — see app.services.service_line for the canonical
    # constants. Default mirrors DEFAULT_SERVICE_LINE so single-call sites
    # that don't pass service_line stay on the labor flow.
    service_line: str = SERVICE_LINE_IT_SERVICES
    # Pre-rendered cost-block for non-labor service lines (currently
    # payment_systems). When set, build_cached_prefix uses the
    # payment-systems template instead of the labor-flow template.
    payment_systems_cost_block: str = ""
    # Pre-rendered ACCEPTED REVIEWER DIRECTIVES block — accepted Cost
    # Reviewer findings the writer must apply on this run. Empty
    # string = no findings (first-time draft). When non-empty, the
    # block is injected at the top of the cached prefix so the writer
    # treats it as the highest-priority instruction. Currently
    # populated only for service_line=payment_systems by the
    # orchestrator; the labor flow's analogous fix path goes through
    # the Strategy Implementer, not the cached prefix.
    accepted_directives_block: str = ""


# Cached-prefix template for service_line=payment_systems. The
# payment_systems_cost_block already contains brand framing, fee
# schedule, hardware approach, compliance attestations, fit risks,
# capability spotlight, personnel spotlight, past performance, and
# narrative anchors — so the prefix only needs the cost block,
# market scan, and Quadratic profile.
#
# When the user has accepted reviewer findings on a prior draft, the
# orchestrator pre-renders an ACCEPTED REVIEWER DIRECTIVES block and
# slots it ABOVE the cost build. The writer treats it as the highest-
# priority instruction set: each accepted directive must be applied
# to its named section, replacing the verbatim quote the reviewer
# flagged with the directive's `canonical_fix`. CRITICAL directives
# are non-negotiable; MAJOR fixes get applied verbatim; MINOR fixes
# can be paraphrased to fit narrative flow as long as the underlying
# correction lands.
_PAYMENT_SYSTEMS_CACHED_PREFIX_TEMPLATE = """{accepted_directives_block}=== COST_BUILD (Payment Systems — single source of truth) ===
{payment_systems_cost_block}

=== MARKET_SCAN ===
{market_scan_block}

=== QUADRATIC profile ===
{quadratic_summary}
"""


def build_cached_prefix(ctx: CostWriterContext) -> str:
    """Compose the static cached-prefix block. Same prefix is reused
    across every cost-deferred section in one run, hitting the
    Anthropic prompt cache after the first call.

    Branches by service_line: payment_systems uses the simpler
    fee-schedule-driven template (no LOW/MEDIUM/HIGH labor scenarios,
    no labor lines, no phases). Default it_services flow renders
    the full labor-flow template."""
    if ctx.service_line == SERVICE_LINE_PAYMENT_SYSTEMS:
        return _PAYMENT_SYSTEMS_CACHED_PREFIX_TEMPLATE.format(
            accepted_directives_block=ctx.accepted_directives_block,
            payment_systems_cost_block=(ctx.payment_systems_cost_block or "(no payment-systems data loaded)"),
            market_scan_block=_format_market_scan_block(
                ctx.market_scan_snapshot,
            ),
            quadratic_summary=ctx.quadratic_summary or "(no profile)",
        )

    by_scenario = {p["scenario"]: p for p in ctx.pricing_packages_snapshot}

    return _CACHED_PREFIX_TEMPLATE.format(
        proposed_scenario=ctx.proposed_scenario,
        low_scenario_block=_format_scenario_block(by_scenario.get("LOW")),
        medium_scenario_block=_format_scenario_block(
            by_scenario.get("MEDIUM"),
        ),
        high_scenario_block=_format_scenario_block(by_scenario.get("HIGH")),
        labor_lines_block=_format_labor_lines_table(
            by_scenario.get(ctx.proposed_scenario),
        ),
        phases_block=_format_phases_block(
            by_scenario.get(ctx.proposed_scenario),
        ),
        odcs_block=_format_odcs_block(
            by_scenario.get(ctx.proposed_scenario),
        ),
        subcontractor_block=_format_subcontractor_block(
            by_scenario.get(ctx.proposed_scenario),
        ),
        market_scan_block=_format_market_scan_block(
            ctx.market_scan_snapshot,
        ),
        executive_summary=(ctx.executive_summary or "(no executive summary)").strip(),
        methodology_block=_format_methodology_block(),
        quadratic_summary=ctx.quadratic_summary or "(no profile)",
    )


def draft_cost_section(
    *,
    proposal_id: int,
    section_id: str,
    section_title: str,
    section_order: int,
    section_brief: str,
    compliance_item_ids: list[str],
    compliance_text: str,
    page_limit: int | None,
    word_limit: int | None,
    cached_prefix: str,
    rfp_title: str,
    rfp_agency: str,
    pop_months: int,
    contract_type_signal: str,
    outline_snippet: str,
) -> SectionDraft:
    """Draft ONE cost-deferred section. Same return shape as the
    existing Writer Team's draft_section so persist_section_draft
    works unchanged.

    Caller MUST handle exceptions — Sonnet 4.6 can fail on rate
    limits, tool-call truncation, or schema-validation issues.
    """
    settings = get_settings()

    user_prompt = _USER_TEMPLATE.format(
        section_id=section_id,
        section_title=section_title,
        section_order=section_order,
        section_brief=section_brief or "(no brief)",
        page_limit=(page_limit if page_limit is not None else "none specified"),
        word_limit=(word_limit if word_limit is not None else "none specified"),
        compliance_items=(", ".join(compliance_item_ids) if compliance_item_ids else "(none)"),
        compliance_text=compliance_text or "(no related compliance items)",
        rfp_title=rfp_title or "(untitled)",
        rfp_agency=rfp_agency or "(unknown)",
        pop_months=pop_months,
        contract_type_signal=contract_type_signal or "(unknown)",
        outline_snippet=outline_snippet or "(no sibling sections)",
    )

    tool_input, usage = call_tool_for_model(
        model=settings.model_cost_writer,
        system=_SYSTEM,
        cached_prefix=cached_prefix,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=12000,
        agent_name="cost_writer",
        proposal_id=proposal_id,
    )

    if usage.get("stop_reason") in ("max_tokens", "length"):
        # Same silent-zero defense as the other agents.
        n_chars = len(tool_input.get("draft_text_markdown") or "")
        raise RuntimeError(
            f"cost_writer: output truncated at max_tokens "
            f"(in={usage['input_tokens']}, "
            f"out={usage['output_tokens']}). "
            f"Got {n_chars} chars of draft before truncation. "
            f"Bump max_tokens or split inputs."
        )

    log.info(
        "cost_writer: %s '%s' -> %d markdown chars, %d citations, %d needs_human, %s",
        section_id,
        section_title,
        len(tool_input.get("draft_text_markdown") or ""),
        len(tool_input.get("citations") or []),
        len(tool_input.get("needs_human_placeholders") or []),
        fmt_llm_usage(usage),
    )

    return SectionDraft(
        draft_text_markdown=str(tool_input.get("draft_text_markdown") or ""),
        citations=list(tool_input.get("citations") or []),
        needs_human_placeholders=list(
            tool_input.get("needs_human_placeholders") or [],
        ),
        shortfall_mitigations_applied=[
            str(s) for s in (tool_input.get("shortfall_mitigations_applied") or [])
        ],
    )


# ---- Cached-prefix formatters --------------------------------------------


def _format_scenario_block(pkg: dict | None) -> str:
    """Compact one-line summary of a scenario's totals — what the
    agent reads when it cites a scenario's price."""
    if pkg is None:
        return "  (scenario not available)"
    return (
        f"  proposed_price_usd={pkg.get('total_proposed_price') or 0:,.0f} | "
        f"loaded_labor_cost_usd="
        f"{pkg.get('loaded_labor_cost') or 0:,.0f} | "
        f"vs_market_position={pkg.get('vs_market_position') or '?'} | "
        f"bid_recommendation={pkg.get('bid_recommendation') or '?'} | "
        f"recommendation_rationale={(pkg.get('recommendation_rationale') or '').strip()}\n"
        f"  indirect_costs={json.dumps(pkg.get('indirect_costs_json') or {}, indent=None)}\n"
        f"  pnl={json.dumps(pkg.get('pnl_projection_json') or {}, indent=None)}"
    )


def _format_labor_lines_table(pkg: dict | None) -> str:
    """Render the proposed-scenario labor lines as a fenced markdown-
    style table the agent can transcribe directly into its draft."""
    if pkg is None or not pkg.get("lines"):
        return "  (no labor lines available)"
    rows: list[str] = []
    rows.append(
        "| labor_category | wage_band | hrs | loaded_rate/hr | billed_rate/hr | billed_total | rationale |"
    )
    rows.append("|---|---|---|---|---|---|---|")
    for ln in pkg["lines"]:
        rationale = (ln.get("rationale") or "").strip()
        # Truncate long rationale to keep the table tidy in the prompt.
        if len(rationale) > 200:
            rationale = rationale[:197] + "..."
        rows.append(
            f"| {ln.get('labor_category', '?')} | "
            f"{ln.get('wage_band', '?')} | "
            f"{ln.get('hours') or 0:.0f} | "
            f"${ln.get('loaded_hourly_rate_usd') or 0:.2f} | "
            f"${ln.get('proposed_billing_rate_usd') or 0:.2f} | "
            f"${ln.get('billed_total_usd') or 0:,.2f} | "
            f"{rationale} |"
        )
    return "\n".join(rows)


def _format_phases_block(pkg: dict | None) -> str:
    """Render the proposed scenario's lifecycle phase breakdown for
    the writer's cached prefix. Each phase emits its name, 1-2 sentence
    description, month range, hours, cost decomposition (loaded / G&A
    / contingency / profit), price, and per-category labor allocations
    within the phase. Closes with an aggregate sanity-check line so the
    writer can confirm phase prices roll up to the scenario total.

    Used by the writer to produce a phase-by-phase Basis of Estimate
    when the federal cost-proposal convention calls for one (which is
    most non-trivial work). Returns a "(no phase data)" notice when
    the analyst hasn't populated phases — writer falls back to the
    labor-category BOE in that case.
    """
    if pkg is None:
        return "  (no proposed-scenario data available)"
    phases = [ph for ph in (pkg.get("phase_breakdown_json") or []) if not ph.get("_synthetic_summary")]
    if not phases:
        return (
            "  (no lifecycle phases persisted — re-run Cost Analyst "
            "to populate phase breakdown; for this draft fall back "
            "to labor-category Basis of Estimate)"
        )

    rows: list[str] = []
    for i, ph in enumerate(phases, 1):
        name = ph.get("name") or f"Phase {i}"
        description = (ph.get("description") or "").strip()
        start_m = int(ph.get("start_month") or 1)
        duration = float(ph.get("duration_months") or 0)
        # Use ceiling end month for display so a 1.5-mo phase starting
        # at M1 reads as M1-M2 (covers half of M2), matching the UI.
        import math

        end_m = max(start_m, math.ceil(start_m + duration - 1))
        month_str = f"M{start_m}" if start_m == end_m else f"M{start_m}-M{end_m}"
        hours = float(ph.get("phase_total_hours") or 0)
        loaded = float(ph.get("phase_loaded_cost_usd") or 0)
        ga = float(ph.get("phase_ga_usd") or 0)
        cont = float(ph.get("phase_contingency_cost_usd") or 0)
        subtotal = float(ph.get("phase_subtotal_cost_usd") or 0)
        profit = float(ph.get("phase_profit_usd") or 0)
        price = float(ph.get("phase_price_usd") or 0)

        rows.append(f"  Phase {i}: {name}")
        if description:
            rows.append(f"    description: {description}")
        rows.append(
            f"    period: {month_str} ({duration:g} months) | hours: {hours:,.0f} | price: ${price:,.0f}"
        )
        rows.append(
            f"    cost decomposition: loaded labor ${loaded:,.0f} + "
            f"G&A ${ga:,.0f} + contingency ${cont:,.0f} = "
            f"subtotal ${subtotal:,.0f} | profit ${profit:,.0f}"
        )
        allocations = ph.get("labor_allocations") or []
        if allocations:
            rows.append("    labor allocations:")
            for alloc in allocations:
                cat = alloc.get("labor_category") or "?"
                hrs = float(alloc.get("hours") or 0)
                billed = float(alloc.get("billed_total_usd") or 0)
                rows.append(f"      - {cat}: {hrs:,.0f} hrs, ${billed:,.0f}")
        rows.append("")  # blank line between phases for readability

    # Aggregate sanity-check line — phase prices may sum to the
    # scenario total ± rounding accumulation across N phases.
    total_phase_price = sum(float(ph.get("phase_price_usd") or 0) for ph in phases)
    total_phase_hours = sum(float(ph.get("phase_total_hours") or 0) for ph in phases)
    rows.append(
        f"  Aggregate: {len(phases)} phases | "
        f"{total_phase_hours:,.0f} hrs allocated | "
        f"sum of phase prices ${total_phase_price:,.0f}"
    )
    return "\n".join(rows)


def _format_odcs_block(pkg: dict | None) -> str:
    if pkg is None:
        return "  (no scenario data)"
    odcs = pkg.get("odcs_json") or []
    if not odcs:
        return "  (no ODCs proposed)"
    rows: list[str] = []
    for o in odcs:
        rows.append(
            f"  - {o.get('item') or '?'}: "
            f"${o.get('amount_usd') or 0:,.0f} — "
            f"{(o.get('justification') or '').strip()}"
        )
    return "\n".join(rows)


def _format_subcontractor_block(pkg: dict | None) -> str:
    if pkg is None or pkg.get("subcontractor_costs") in (None, 0, 0.0):
        return "  (none — self-perform)"
    return f"  subcontractor_costs_usd=${float(pkg['subcontractor_costs']):,.0f}"


def _format_market_scan_block(snap: dict | None) -> str:
    if snap is None:
        return "  (no market scan persisted — omit market-comparison narrative)"
    rows: list[str] = []
    rows.append(
        f"  market_band: low=${snap.get('market_band_low_usd') or 0:,.0f} "
        f"| mid=${snap.get('market_band_mid_usd') or 0:,.0f} "
        f"| high=${snap.get('market_band_high_usd') or 0:,.0f}"
    )
    methodology = (snap.get("methodology") or "").strip()
    if methodology:
        rows.append(f"  methodology: {methodology[:600]}{'...' if len(methodology) > 600 else ''}")
    if snap.get("comparable_awards"):
        rows.append("  comparable_awards (top 5):")
        for a in snap["comparable_awards"][:5]:
            v = a.get("award_value_usd")
            v_str = f"${v:,.0f}" if v is not None else "$?"
            rows.append(
                f"    - {(a.get('award_title') or '?')[:80]} | "
                f"{v_str} | "
                f"{a.get('period_of_performance_months') or '?'}mo | "
                f"awardee={a.get('awardee_name') or '?'} | "
                f"url={a.get('source_url') or '?'}"
            )
    if snap.get("competitors"):
        rows.append("  competitors:")
        for c in snap["competitors"]:
            rl = c.get("estimated_rate_low_usd")
            rh = c.get("estimated_rate_high_usd")
            rl_s = f"${rl:.0f}/hr" if rl is not None else "$?/hr"
            rh_s = f"${rh:.0f}/hr" if rh is not None else "$?/hr"
            rows.append(
                f"    - {c.get('competitor_name') or '?'} "
                f"({c.get('likelihood_to_bid') or '?'}): "
                f"{rl_s} - {rh_s}"
            )
    return "\n".join(rows)


def _format_methodology_block() -> str:
    """Read internal_pricing_rules.json + format the methodology bits
    the writer can reference (without dumping the whole file)."""
    from app.services.pricing import get_pricing_rules

    rules = get_pricing_rules()
    escalation = rules.get("default_annual_labor_escalation_rate", 0.03)
    return (
        f"  Annual billable hours per FTE: {rules['annual_billable_hours']}\n"
        f"  Wrap rate components: base wages + health benefits + payroll "
        f"taxes (FICA/Medicare/FUTA/SUTA) + Paylocity overhead + 401K "
        f"match + bonus + education + per-FTE software ($1,336/yr).\n"
        f"  G&A overhead: ${rules['ga_overhead']['annual_office_pool_usd']:,.0f}/yr "
        f"office pool, allocated per FTE per year against avg headcount "
        f"during PoP (recomputed per bid).\n"
        f"  Profit policy: floor "
        f"{rules['profit_policy']['floor_margin_pct']:.0%} | target "
        f"{rules['profit_policy']['target_margin_pct']:.0%} | ceiling "
        f"{rules['profit_policy']['ceiling_margin_pct']:.0%}.\n"
        f"  Default annual labor escalation (option-year / multi-year "
        f"pricing): {float(escalation):.1%}. Apply to Years 2+ unless "
        f"the RFP specifies a different escalator. Use this rate when "
        f"rendering multi-year tables — DO NOT emit a [NEEDS_HUMAN] "
        f"placeholder for option-year pricing.\n"
        f"  Labor categories: GSA OLM schedule, NAICS "
        f"{rules['_meta']['naics']} (Custom Computer Programming Services), "
        f"effective {rules['_meta']['rates_effective_date']}."
    )


__all__ = [
    "CostWriterContext",
    "DEFAULT_PROPOSED_SCENARIO",
    "build_cached_prefix",
    "draft_cost_section",
]
