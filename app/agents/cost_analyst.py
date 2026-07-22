"""Cost Analyst — synthesizes market scan + internal pricing rules +
scope context into per-scenario LABOR JUDGMENT for the H/M/L cost
build.

Single LLM call (GPT-5.5 by default) returning structured tool input:

  - labor_lines[]: per-FTE judgment (category, wage_band, hours,
    rationale). Same labor structure feeds all three scenarios; what
    differs across scenarios is coverage / margin / contingency.
  - avg_headcount_during_pop: company-wide FTE estimate for G&A
    allocation per Jordan's allocation method (a).
  - odcs[]: Other Direct Costs (travel, equipment, training).
  - subcontractor_costs_usd: optional sub passthrough.
  - key_risks: judgment commentary the user surfaces on the Cost tab.
  - executive_summary: 2-3 paragraph narrative for the P&L view.

The LLM NEVER returns a dollar total. All per-scenario arithmetic
(loaded rates, G&A allocation, contingency, profit, proposed price)
lives in app.services.pricing.compute_scenario_packages, gated by
the wrap_rate_formula in data/internal_pricing_rules.json.

Inputs the agent gets:
  - Market scan summary (band, top comparable awards, top
    competitor rates).
  - Pricing rules summary (labor_catalog category list with ceiling
    rates + experience years; wage_bands list; scenario_definitions).
  - RFP scope (compliance items + section briefs from outline).
  - Quadratic profile summary (so it doesn't propose Software
    Engineer V on a Drupal CMS bid).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings
from app.services.llm import call_tool_for_model, fmt_llm_usage
from app.services.pricing import (
    CostAnalystLaborLine,
    CostAnalystOdc,
    CostAnalystOutput,
    CostAnalystPhase,
    CostAnalystPhaseAllocation,
    get_pricing_rules,
)

log = logging.getLogger(__name__)


# ---- Tool schema ----------------------------------------------------------

_TOOL: dict = {
    "name": "report_cost_analysis",
    "description": (
        "Report a labor estimate + scope-cost context for the proposal. "
        "Return ONLY judgment fields — labor categories, salaries, "
        "hours, ODCs, headcount, risks, narrative. Do NOT return any "
        "dollar totals, G&A amounts, profit numbers, or proposed prices. "
        "Downstream Python applies the wrap rate formula and scenario "
        "definitions to compute every dollar value. The LLM judging "
        "the labor mix is the value-add; the math is deterministic."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "labor_lines": {
                "type": "array",
                "description": (
                    "One entry per FTE allocation needed to deliver "
                    "the proposed work. Same structure feeds LOW / "
                    "MEDIUM / HIGH scenarios — coverage and margin "
                    "are scenario-level (handled downstream), but the "
                    "labor MIX (which categories, how many hours each) "
                    "is your judgment and is shared across scenarios. "
                    "Be SPECIFIC: a $1M website-CMS bid does not need "
                    "Software Engineer V or Solutions Architect; it "
                    "needs a Project Manager + a couple of mid-level "
                    "Drupal engineers + a tester."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "labor_category": {
                            "type": "string",
                            "description": (
                                "MUST match a category in the "
                                "labor_catalog you were given (e.g., "
                                "'Project Manager II', 'Software "
                                "Engineer III', 'Test Engineer III'). "
                                "Categories not in the catalog will "
                                "be rejected by the math layer — the "
                                "user can't bid a category Quadratic's "
                                "GSA OLM schedule doesn't list."
                            ),
                        },
                        "wage_band": {
                            "type": "string",
                            "description": (
                                "MUST match a key in the wage_bands "
                                "dict you were given (e.g., '125k', "
                                "'150k', '170k'). The band represents "
                                "the typical hire's annual base wage "
                                "for this role; downstream looks up "
                                "the loaded annual cost. Pick the "
                                "default_wage_band from the catalog "
                                "unless the role is more / less senior "
                                "than typical for THIS scope — then "
                                "explain in rationale."
                            ),
                        },
                        "hours": {
                            "type": "number",
                            "description": (
                                "TOTAL billable hours over the full "
                                "period of performance for THIS FTE "
                                "line. A full-time PM on a 12-month "
                                "PoP = 1950 hrs (1 FTE × 1 year). A "
                                "half-time SME on a 6-month PoP = "
                                "488 hrs (0.5 FTE × 0.5 yr × 1950). "
                                "Be conservative — over-estimating "
                                "hours kills margin, under-estimating "
                                "kills delivery."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "1-2 sentences justifying this line: "
                                "WHY this category at this band for "
                                "this scope. E.g., 'Solo PM for the "
                                "12-month PoP — mid-band hire fits "
                                "a 3-year-experience CO/CTR interface "
                                "role; Senior PM unnecessary for "
                                "routine status reporting.' Renders "
                                "on the Cost tab next to the line."
                            ),
                        },
                    },
                    "required": [
                        "labor_category",
                        "wage_band",
                        "hours",
                        "rationale",
                    ],
                },
            },
            "avg_headcount_during_pop": {
                "type": "number",
                "description": (
                    "Estimated COMPANY-WIDE FTE count of Quadratic "
                    "during the contract's period of performance — "
                    "NOT just the project team. Used for G&A "
                    "allocation: ga_hourly_addon = annual_office_pool "
                    "÷ (avg_headcount × 1950). Default to current "
                    "Quadratic headcount as a conservative proxy if "
                    "no growth signal. Must be > 0."
                ),
            },
            "odcs": {
                "type": "array",
                "description": (
                    "Other Direct Costs — non-labor expenses billed "
                    "to the customer (travel, equipment, training, "
                    "third-party software licenses, cloud hosting, "
                    "subscription fees). Empty array when not "
                    "applicable; many bids have no ODCs."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "item": {"type": "string"},
                        "amount_usd": {
                            "type": "number",
                            "description": (
                                "ANNUAL amount when year_count > 1, "
                                "otherwise total spend over the PoP. "
                                "Recurring items (cloud hosting, "
                                "license subscriptions) should be "
                                "set as the per-year cost with "
                                "year_count = number of years; "
                                "one-time items use year_count = 1."
                            ),
                        },
                        "justification": {"type": "string"},
                        "year_count": {
                            "type": "integer",
                            "description": (
                                "Number of years this ODC recurs. "
                                "1 for one-time spend (default). "
                                "N for recurring items (e.g., 3-year "
                                "hosting, 5-year license). The cost "
                                "build multiplies amount_usd × "
                                "year_count to get the ODC's full "
                                "contribution to the bid total."
                            ),
                        },
                    },
                    "required": ["item", "amount_usd", "justification"],
                },
            },
            "subcontractor_costs_usd": {
                "type": ["number", "null"],
                "description": (
                    "Total subcontractor passthrough for the bid, "
                    "USD. Null when self-perform-everywhere (most "
                    "Quadratic bids). When non-null, this is the "
                    "BASE sub cost — markup is NOT applied here; "
                    "downstream rolls subs into the cost subtotal "
                    "and applies margin uniformly."
                ),
            },
            "key_risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "3-6 bullet-style strings flagging risks that "
                    "could affect cost certainty: scope ambiguity, "
                    "agency/CO unfamiliarity, unfamiliar tech stack, "
                    "tight PoP, dependency on government-furnished "
                    "data, etc. Surfaced on the Cost tab so the user "
                    "can decide whether to bid the LOW (competitive) "
                    "scenario or HIGH (protective)."
                ),
            },
            "executive_summary": {
                "type": "string",
                "description": (
                    "2-3 paragraph narrative for the P&L view. "
                    "Cover: (1) the labor approach (team size, "
                    "seniority mix, why this composition), (2) the "
                    "pricing posture (where vs the market band, "
                    "what differentiates Quadratic — typically the "
                    "AI-accelerated delivery edge), (3) the bid "
                    "recommendation across scenarios. Will appear "
                    "verbatim in the Cost tab and feeds the Cost "
                    "Volume Writer's narrative."
                ),
            },
            "lifecycle_phases": {
                "type": "array",
                "description": (
                    "Break the proposed work into 4-7 lifecycle "
                    "phases (federal cost-proposal convention "
                    "expects a Basis of Estimate by phase, not just "
                    "by labor category). Pick a lifecycle model "
                    "appropriate to the scope: SDLC for build work "
                    "(Discovery, Design, Build, Test, Deploy, "
                    "Operations), ITSM for managed services "
                    "(Transition, Steady State, Continuous "
                    "Improvement), or PMBOK for pure project work "
                    "(Initiation, Planning, Execution, Closing). "
                    "Phases must cover the FULL period of "
                    "performance — overlapping is fine where work "
                    "runs in parallel."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Short phase name (e.g., 'Discovery "
                                "& Planning', 'Build & Integration', "
                                "'Operations & Steady State'). "
                                "Reads as a section header in the "
                                "Cost Volume narrative."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "1-2 sentences describing what work "
                                "happens in this phase and what "
                                "deliverables it produces. Used in "
                                "the Cost Volume Writer's BOE "
                                "narrative."
                            ),
                        },
                        "start_month": {
                            "type": "integer",
                            "description": (
                                "1-indexed month of the period of "
                                "performance when this phase starts. "
                                "1 = first month, etc. Phases CAN "
                                "overlap when work runs in parallel "
                                "(e.g., Operations starting in "
                                "month 6 while Build is still "
                                "wrapping)."
                            ),
                        },
                        "duration_months": {
                            "type": "number",
                            "description": (
                                "How many months this phase runs. "
                                "Fractional values (e.g., 1.5) "
                                "allowed for short transition "
                                "phases."
                            ),
                        },
                        "labor_allocations": {
                            "type": "array",
                            "description": (
                                "Hours-per-labor-category allocated "
                                "to THIS phase. The labor_category "
                                "MUST match an entry in your "
                                "labor_lines[] above. Sum across "
                                "all phases per category should "
                                "equal that category's total hours "
                                "(or less; under-allocation is "
                                "treated as 'general support time' "
                                "spread across all phases). Do NOT "
                                "exceed the total per category."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "labor_category": {
                                        "type": "string",
                                        "description": (
                                            "MUST match one of the labor_lines[].labor_category values."
                                        ),
                                    },
                                    "hours": {
                                        "type": "number",
                                        "description": (
                                            "Hours of THIS category allocated to THIS phase. Must be > 0."
                                        ),
                                    },
                                },
                                "required": ["labor_category", "hours"],
                            },
                        },
                    },
                    "required": [
                        "name",
                        "description",
                        "start_month",
                        "duration_months",
                        "labor_allocations",
                    ],
                },
            },
        },
        "required": [
            "labor_lines",
            "avg_headcount_during_pop",
            "odcs",
            "subcontractor_costs_usd",
            "key_risks",
            "executive_summary",
            "lifecycle_phases",
        ],
    },
}


# ---- System prompt --------------------------------------------------------

_SYSTEM = """You are the Cost Analyst for Quadratic Digital. Your one job is to translate an RFP scope into a defensible LABOR ESTIMATE — which categories, at which salaries, for how many hours each. Downstream code applies the wrap rate formula, G&A allocation, scenario-specific margin, and contingency to produce three price points (Low / Medium / High). YOU NEVER RETURN DOLLAR TOTALS — that's the math layer's job, and a $50K math error on a $500K bid ends contracting careers.

APPROVED TEAM ROSTER — when an "=== APPROVED TEAM ROSTER (USE THESE LABOR LINES VERBATIM) ===" block appears in the user prompt, the user has already locked in the team composition. Your labor_lines output MUST mirror that roster: same labor_category strings, same wage_band values, same hours. Do NOT add extra labor categories, do NOT adjust hours up or down, do NOT pick different salary bands. Reference the EXACT labor_category strings in lifecycle_phases.labor_allocations so the math layer can tie hours back to lines. Your remaining value-add: refine each line's rationale, allocate hours into phases (typical SDLC / ITSM / PMBOK pattern as before), choose ODCs, write key_risks and executive_summary. When NO roster block is present, decide the labor mix yourself per the rules below.

DO:
- Match labor_category to ONE of the catalog entries you're shown. Categories not in the catalog get rejected by the math layer — the user can't bid a category Quadratic's GSA OLM schedule doesn't list.
- Match wage_band to ONE of the documented salary brackets. Default to the labor_catalog entry's default_wage_band unless THIS scope justifies a different salary, then explain in rationale.
- Be REALISTIC about team size. A $1M state-agency website CMS contract is a 3-5 person team for a 12-month PoP, not a 15-person enterprise integration. Read the scope; size accordingly.
- Use the market scan to gut-check team size. The competitor rates show what the market expects to bill at your scale. If your blended rate would be 30% above the highest competitor's, the team is too senior or you're padding hours.
- Cover the work. Every compliance item that requires labor needs an FTE allocation that can deliver it. Common minimums for state IT services: 1 PM, 1-2 ICs, 1 QA / Test, 1 SME or part-time technical lead. Skim the section briefs to make sure no scope is unstaffed.
- Be transparent in rationale. "Solo SE III for 1950 hrs handles all Drupal development; SE II is too junior for security-controlled state data, SE IV would price us out vs Forum One at $250/hr." That's the kind of line a federal CO can read and respect.

DO NOT:
- Return any dollar amounts in labor_lines. No `loaded_cost`, no `billed_total`, no `profit`. The schema doesn't allow them; the math layer computes them.
- Fabricate labor categories. ONLY use the catalog you were given. Do NOT invent "Senior Drupal Developer" — pick "Software Engineer III" + write the Drupal experience into the rationale.
- Over-staff. Federal evaluators look for cost realism; bloated teams get scored down. If the scope is "build and host a CMS site", you don't need a Solutions Architect AND a DevSecOps Engineer AND a Senior Tech Lead.
- Under-staff. Bid teams that look unrealistically small ("two Software Engineer III's for a 12-month $1M contract — really?") fail evaluator credibility checks even if cheap.
- Quote subcontractor markups. The schema's subcontractor_costs_usd is the BASE pass-through; downstream applies margin uniformly.
- Treat the LOW / MEDIUM / HIGH scenarios as needing different labor lines. THEY DON'T. Same labor mix; only coverage / margin / contingency vary across scenarios. Return ONE labor_lines structure.

OUTPUT DISCIPLINE:
- Call the report_cost_analysis tool. Do not preamble.
- labor_lines: 3-8 entries typical. More than 12 entries on a sub-$2M bid is over-built; refactor.
- rationale on every line, brief but specific.
- avg_headcount_during_pop: estimate company-wide Quadratic FTEs during the PoP. If unsure, use the current employee count from the profile as a conservative default.
- key_risks: 3-6 bullets. Real risks, not boilerplate. "Scope contains 'integration with NCIC' but no NCIC-specific requirements — clarification needed before realistic LOE" beats "general scope ambiguity".
- executive_summary: 2-3 short paragraphs. Defensible, specific, no marketing fluff.

LIFECYCLE PHASES — REQUIRED:
- Break the work into 4-7 phases that cover the full PoP. Pick a model that fits the scope: SDLC (Discovery / Design / Build / Test / Deploy / Operations) for build work, ITSM (Transition / Steady State / Improvement) for managed services, or PMBOK (Initiation / Planning / Execution / Closing) for pure project work.
- Phases CAN overlap — Operations and Build often run in parallel toward end of PoP. Use start_month + duration_months to express overlap.
- For each phase, allocate hours from your labor_lines[] entries. The sum of allocated hours per labor_category across ALL phases should equal that category's labor_lines.hours value (a small under-allocation is OK and reads as "general support time"; over-allocation is rejected).
- Allocations are integers OR floats. Use realistic distributions — Discovery is typically 60-70% BA / PM, Build is 70%+ engineers, Operations is light steady-state.
- Phases drive the Basis of Estimate in the Cost Volume narrative. Realistic phase mass + duration is what evaluators score for cost realism.
- When buyer cost-matrix rows are shown, treat their exact labels as required reporting context. If a row genuinely represents a lifecycle/work phase, use that buyer-authored label as the phase name so deterministic pricing can map cleanly. Do NOT force unlike rows (unit rates, option years, fees, quantities, totals, or metadata) into phases, and never invent allocations or dollar totals merely to fill a matrix."""


# ---- User template --------------------------------------------------------

_USER_TEMPLATE = """Estimate the labor mix for this proposal.

=== RFP context ===
Title: {rfp_title}
Customer agency: {rfp_agency}
NAICS: {naics}
Period of performance: ~{pop_months} months
Estimated value range (rough, from intake): ${est_value_low_usd:,.0f} - ${est_value_high_usd:,.0f}

=== Scope summary (from compliance matrix) ===
{scope_summary}

=== Outline / section briefs ===
{outline_briefs}

=== Buyer cost-matrix reporting rows (template-specific; may be empty) ===
{cost_matrix_requirements_block}

=== Market scan (Agent 1's output) ===
Market band: ${market_band_low} / ${market_band_mid} / ${market_band_high} (low / mid / high)
Methodology: {market_methodology}

Comparable awards (top 5):
{comparable_awards_block}

Likely competitors and their estimated rates:
{competitors_block}

=== Internal pricing context ===
Annual billable hours per FTE: {annual_billable_hours}

Labor catalog you may pick from (category | ceiling rate | min experience | default salary):
{labor_catalog_block}

Salary brackets (key | annual base | loaded annual high coverage | loaded annual low coverage):
{wage_bands_block}

Scenario definitions (used by downstream — do not encode in your output):
{scenario_definitions_block}

Profit policy: floor {profit_floor_pct:.0%} | target {profit_target_pct:.0%} | ceiling {profit_ceiling_pct:.0%}
G&A pool: ${ga_pool:,.0f}/yr (allocated as ga_hourly_addon = pool ÷ avg_headcount ÷ {annual_billable_hours})

=== Quadratic context ===
{quadratic_summary}
{team_roster_block}
Call report_cost_analysis with your labor judgment. Remember: NO dollar totals in your output — only categories, salaries, hours, headcount, ODCs (with their amounts but not totals), and narrative."""


# ---- Public entry point ---------------------------------------------------


def analyze_costs(
    *,
    proposal_id: int,
    rfp_title: str,
    rfp_agency: str,
    naics: str,
    pop_months: int,
    est_value_low_usd: float,
    est_value_high_usd: float,
    scope_summary: str,
    outline_briefs: str,
    market_scan_snapshot: dict[str, Any] | None,
    quadratic_summary: str,
    team_roster_block: str = "",
    cost_matrix_requirements: list[dict[str, Any]] | None = None,
) -> CostAnalystOutput:
    """Run the Cost Analyst LLM call. Returns a structured output the
    orchestrator hands to compute_scenario_packages.

    `team_roster_block` is the rendered "APPROVED TEAM ROSTER" block
    from app.services.team.format_team_roster_for_cost_analyst —
    empty string when the user hasn't approved a team yet, in which
    case the agent decides labor mix as before. When non-empty, the
    agent is constrained to use the roster's categories, salaries,
    and hours verbatim; the orchestrator additionally replaces the
    agent's labor_lines with the deterministic roster-derived ones
    after the call (defense in depth).

    Caller MUST handle exceptions — GPT-5.5 can fail on rate limits,
    tool-call truncation, or schema-validation issues from the API.
    """
    settings = get_settings()
    rules = get_pricing_rules()

    # Bracket non-empty roster block with blank lines so it reads
    # as a distinct section in the user prompt; empty string passes
    # through cleanly.
    if team_roster_block.strip():
        team_block = "\n" + team_roster_block.rstrip() + "\n"
    else:
        team_block = ""

    user_prompt = _USER_TEMPLATE.format(
        rfp_title=rfp_title or "(untitled)",
        rfp_agency=rfp_agency or "(unknown)",
        naics=naics or "(unknown)",
        pop_months=pop_months,
        est_value_low_usd=est_value_low_usd,
        est_value_high_usd=est_value_high_usd,
        scope_summary=scope_summary or "(no scope summary)",
        outline_briefs=outline_briefs or "(no outline available)",
        cost_matrix_requirements_block=(
            json.dumps(cost_matrix_requirements, indent=2, ensure_ascii=False)
            if cost_matrix_requirements
            else "(no cost matrix supplied)"
        ),
        market_band_low=_fmt_band(
            market_scan_snapshot,
            "market_band_low_usd",
        ),
        market_band_mid=_fmt_band(
            market_scan_snapshot,
            "market_band_mid_usd",
        ),
        market_band_high=_fmt_band(
            market_scan_snapshot,
            "market_band_high_usd",
        ),
        market_methodology=((market_scan_snapshot or {}).get("methodology") or "(no methodology)"),
        comparable_awards_block=_format_awards_block(market_scan_snapshot),
        competitors_block=_format_competitors_block(market_scan_snapshot),
        annual_billable_hours=rules["annual_billable_hours"],
        labor_catalog_block=_format_labor_catalog(rules),
        wage_bands_block=_format_wage_bands(rules),
        scenario_definitions_block=_format_scenarios(rules),
        profit_floor_pct=rules["profit_policy"]["floor_margin_pct"],
        profit_target_pct=rules["profit_policy"]["target_margin_pct"],
        profit_ceiling_pct=rules["profit_policy"]["ceiling_margin_pct"],
        ga_pool=rules["ga_overhead"]["annual_office_pool_usd"],
        quadratic_summary=quadratic_summary or "(no profile)",
        team_roster_block=team_block,
    )

    tool_input, usage = call_tool_for_model(
        model=settings.model_cost_analyst,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=8000,
        agent_name="cost_analyst",
        proposal_id=proposal_id,
    )

    if usage.get("stop_reason") in ("max_tokens", "length"):
        # Truncated mid-tool-call. Same silent-zero defense as
        # ConsistencyChecker / MarketResearcher.
        n_partial_lines = len(tool_input.get("labor_lines") or [])
        raise RuntimeError(
            f"cost_analyst: output truncated at max_tokens "
            f"(in={usage['input_tokens']}, out={usage['output_tokens']}). "
            f"Got {n_partial_lines} partial labor_line(s) before "
            f"truncation. Bump max_tokens or split inputs."
        )

    log.info(
        "cost_analyst: proposal %d — %d labor_lines, headcount=%s, %d odcs, %s",
        proposal_id,
        len(tool_input.get("labor_lines") or []),
        tool_input.get("avg_headcount_during_pop"),
        len(tool_input.get("odcs") or []),
        fmt_llm_usage(usage),
    )

    return _build_output(tool_input)


def _build_output(tool_input: dict[str, Any]) -> CostAnalystOutput:
    """Convert the validated tool input into the structured dataclass.
    Keeps schema-vs-dataclass coupling local."""
    labor_lines: list[CostAnalystLaborLine] = []
    for ll in tool_input.get("labor_lines") or []:
        try:
            labor_lines.append(
                CostAnalystLaborLine(
                    labor_category=str(ll["labor_category"]),
                    wage_band=str(ll["wage_band"]),
                    hours=float(ll["hours"]),
                    rationale=str(ll.get("rationale") or ""),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "cost_analyst: skipping malformed labor_line %r: %s",
                ll,
                exc,
            )

    odcs: list[CostAnalystOdc] = []
    for o in tool_input.get("odcs") or []:
        try:
            year_count = o.get("year_count")
            odcs.append(
                CostAnalystOdc(
                    item=str(o["item"]),
                    amount_usd=float(o["amount_usd"]),
                    justification=str(o.get("justification") or ""),
                    year_count=int(year_count) if year_count else 1,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "cost_analyst: skipping malformed odc %r: %s",
                o,
                exc,
            )

    sub_costs = tool_input.get("subcontractor_costs_usd")
    if sub_costs is not None:
        try:
            sub_costs = float(sub_costs)
        except (TypeError, ValueError):
            sub_costs = None

    phases: list[CostAnalystPhase] = []
    for ph in tool_input.get("lifecycle_phases") or []:
        try:
            allocations: list[CostAnalystPhaseAllocation] = []
            for alloc in ph.get("labor_allocations") or []:
                try:
                    allocations.append(
                        CostAnalystPhaseAllocation(
                            labor_category=str(alloc["labor_category"]),
                            hours=float(alloc["hours"]),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning(
                        "cost_analyst: skipping malformed phase allocation %r: %s",
                        alloc,
                        exc,
                    )
            phases.append(
                CostAnalystPhase(
                    name=str(ph["name"]),
                    description=str(ph.get("description") or ""),
                    start_month=int(ph.get("start_month") or 1),
                    duration_months=float(ph.get("duration_months") or 0),
                    labor_allocations=allocations,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "cost_analyst: skipping malformed phase %r: %s",
                ph,
                exc,
            )

    return CostAnalystOutput(
        labor_lines=labor_lines,
        avg_headcount_during_pop=float(
            tool_input.get("avg_headcount_during_pop") or 0,
        ),
        odcs=odcs,
        subcontractor_costs_usd=sub_costs,
        key_risks=[str(r) for r in (tool_input.get("key_risks") or [])],
        executive_summary=str(tool_input.get("executive_summary") or ""),
        lifecycle_phases=phases,
    )


# ---- Prompt helpers -------------------------------------------------------


def _fmt_band(snap: dict[str, Any] | None, key: str) -> str:
    if snap is None:
        return "(unknown)"
    v = snap.get(key)
    if v is None:
        return "(unknown)"
    return f"{float(v):,.0f}"


def _format_awards_block(snap: dict[str, Any] | None) -> str:
    if snap is None or not snap.get("comparable_awards"):
        return "  (no comparable awards persisted)"
    rows: list[str] = []
    for a in (snap.get("comparable_awards") or [])[:5]:
        title = (a.get("award_title") or "").strip()[:80]
        val = a.get("award_value_usd")
        val_str = f"${float(val):,.0f}" if val is not None else "$?"
        pop = a.get("period_of_performance_months")
        pop_str = f"{pop}mo" if pop is not None else "?mo"
        awardee = a.get("awardee_name") or "?"
        rows.append(f"  - {title} | {val_str} | {pop_str} | awardee={awardee}")
    return "\n".join(rows)


def _format_competitors_block(snap: dict[str, Any] | None) -> str:
    if snap is None or not snap.get("competitors"):
        return "  (no competitors persisted)"
    rows: list[str] = []
    for c in snap.get("competitors") or []:
        name = (c.get("competitor_name") or "?").strip()
        rate_low = c.get("estimated_rate_low_usd")
        rate_high = c.get("estimated_rate_high_usd")
        rate_low_s = f"${float(rate_low):.0f}/hr" if rate_low is not None else "$?/hr"
        rate_high_s = f"${float(rate_high):.0f}/hr" if rate_high is not None else "$?/hr"
        likelihood = c.get("likelihood_to_bid") or "?"
        rows.append(f"  - {name} ({likelihood}): {rate_low_s} - {rate_high_s}")
    return "\n".join(rows)


def _format_labor_catalog(rules: dict[str, Any]) -> str:
    rows: list[str] = []
    for entry in rules["labor_catalog"]:
        rows.append(
            f"  - {entry['category']:<32} | "
            f"${entry['ceiling_hourly_rate_usd']:>7.2f}/hr | "
            f"{entry['min_experience_years']}+ yrs | "
            f"default wage_band={entry['default_wage_band']}"
        )
    return "\n".join(rows)


def _format_wage_bands(rules: dict[str, Any]) -> str:
    rows: list[str] = []
    # Sort by base wage so the agent sees them in order.
    items = sorted(
        rules["wage_bands"].items(),
        key=lambda kv: float(kv[1]["annual_base_wage_usd"]),
    )
    for key, band in items:
        validated = "validated" if band.get("validated") else "extrapolated"
        rows.append(
            f"  - {key:<6} | base=${band['annual_base_wage_usd']:,} | "
            f"loaded high=${band['loaded_annual_cost_high_coverage_usd']:,.0f} | "
            f"loaded low=${band['loaded_annual_cost_low_coverage_usd']:,.0f} "
            f"({validated})"
        )
    return "\n".join(rows)


def _format_scenarios(rules: dict[str, Any]) -> str:
    out = []
    for name in ("low", "medium", "high"):
        sd = rules["scenario_definitions"][name]
        out.append(
            f"  {name.upper()}: coverage={sd['coverage_level']}, "
            f"margin={sd['profit_margin_pct']:.0%}, "
            f"contingency={sd['contingency_hours_pct']:.0%}"
        )
    return "\n".join(out)


__all__ = ["analyze_costs"]
