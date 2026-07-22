"""Pricing math + persistence for the Cost Analyst pipeline.

Two responsibilities:

1. Deterministic computation of H/M/L scenario cost builds from the
   Cost Analyst's labor judgment. The LLM never returns a dollar
   value; this module applies the wrap rate formula, G&A allocation,
   contingency, and margin formula to produce every number. Errors
   here would be procurement disasters — keep arithmetic in code,
   keep the test harness covering it.

2. Upsert helper that writes 3 PricingPackage rows (one per
   scenario) plus N PricingPackageLine rows per package. Re-running
   the analyst REPLACES existing scenarios for the proposal.

Margin formula: margin-on-price (federal convention). Price = cost
/ (1 - margin_pct). Validated against PMCQA Excel — $149.54 billed
× (1 - 0.271 margin) = $109.03 loaded.

Contingency placement: separate cost-build line (NOT distributed
across labor_lines). Sized as `total_hours × contingency_hours_pct`
at the blended loaded hourly rate. Cleaner accounting + matches
federal cost-buildup convention.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.config import DATA_DIR
from app.core.enums import (
    BidRecommendation,
    MarketPosition,
    PricingScenario,
)
from app.db.session import session_scope
from app.models import (
    PricingPackage,
    PricingPackageLine,
    Proposal,
)
from app.services.proposal_access import ensure_proposal_mutable
from app.services.review_freshness import invalidate_cost_review

log = logging.getLogger(__name__)


_PRICING_RULES_PATH = DATA_DIR / "internal_pricing_rules.json"


# ---- Pricing rules loader -------------------------------------------------

_rules_cache: dict[str, Any] | None = None


def get_pricing_rules() -> dict[str, Any]:
    """Read data/internal_pricing_rules.json. Cached for the process
    lifetime — manual file edits require a restart, matching the
    pydantic-settings pattern."""
    global _rules_cache
    if _rules_cache is None:
        if not _PRICING_RULES_PATH.exists():
            raise RuntimeError(
                f"Pricing rules file missing: {_PRICING_RULES_PATH}. "
                f"Run the cost-analyst pipeline only after the canonical "
                f"rules JSON has been created."
            )
        with _PRICING_RULES_PATH.open("r", encoding="utf-8") as f:
            _rules_cache = json.load(f)
    return _rules_cache


def _coverage_for_scenario(scenario: str) -> str:
    """Map scenario → coverage_level per scenario_definitions in the
    rules JSON. LOW = low coverage, MEDIUM/HIGH = high coverage."""
    rules = get_pricing_rules()
    sc_def = rules["scenario_definitions"].get(scenario.lower())
    if sc_def is None:
        raise ValueError(f"Unknown scenario {scenario!r}; expected one of {list(PricingScenario)}")
    return sc_def["coverage_level"]


def _scenario_params(scenario: str) -> dict[str, float]:
    """Pull the H/M/L scenario's burden + margin + contingency
    parameters from the rules JSON."""
    rules = get_pricing_rules()
    sc_def = rules["scenario_definitions"][scenario.lower()]
    return {
        "coverage_level": sc_def["coverage_level"],
        "profit_margin_pct": float(sc_def["profit_margin_pct"]),
        "contingency_hours_pct": float(sc_def["contingency_hours_pct"]),
    }


def _wage_band_loaded_annual(
    wage_band: str,
    coverage_level: str,
) -> float:
    """Look up the loaded annual cost for a salary + coverage.
    Documented wage_bands in the JSON are returned verbatim. Salary
    values NOT in the JSON (e.g., the 5K-increment values exposed
    in the UI's salary dropdown) compute via the wrap_rate_formula
    instead — same math, just not pre-baked. Same coverage_level
    contract: 'high' or 'low'."""
    rules = get_pricing_rules()
    band = rules["wage_bands"].get(wage_band)
    if band is not None:
        if coverage_level == "high":
            return float(band["loaded_annual_cost_high_coverage_usd"])
        if coverage_level == "low":
            return float(band["loaded_annual_cost_low_coverage_usd"])
        raise ValueError(f"Unknown coverage_level {coverage_level!r}; expected 'high' or 'low'")

    # Fallback: parse the band name to a numeric wage and apply the
    # wrap_rate_formula directly. Lets the UI offer 5K-increment
    # bands without bloating the JSON with 30 pre-baked entries.
    wage = _parse_wage_band(wage_band)
    return _compute_loaded_annual_from_wrap_formula(wage, coverage_level)


def _parse_wage_band(wage_band: str) -> float:
    """Parse a wage_band key like '115k' or '85K' into a numeric
    annual wage in USD. Raises ValueError for unparseable input
    (e.g., 'unknown', empty string).

    This is the inverse of how the JSON keys are constructed —
    integer thousands suffixed with 'k'. Stays compatible with the
    documented bands (lookup hits before this is called)."""
    s = (wage_band or "").strip().lower()
    if s.endswith("k"):
        s = s[:-1]
    try:
        return float(s) * 1000.0
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Cannot parse wage_band {wage_band!r} — expected format like '85k' or '150k'"
        ) from exc


def _compute_loaded_annual_from_wrap_formula(
    wage: float,
    coverage: str,
) -> float:
    """Apply the wrap_rate_formula from the rules JSON to compute
    loaded annual cost for an arbitrary wage. Mirrors the formula
    documented in data/internal_pricing_rules.json so the documented
    and computed values stay in sync — if someone updates the formula
    constants there, this picks them up automatically."""
    rules = get_pricing_rules()
    f = rules["wrap_rate_formula"]["components"]

    fica_rate = float(f["fica_rate"])
    fica_wage_base = float(f["fica_wage_base_2025_usd"])
    medicare_rate = float(f["medicare_rate"])
    futa = float(f["futa_annual_usd"])
    suta_rate = float(f["suta_rate"])
    bonus_rate = float(f["bonus_rate_of_wage"])
    employer_401k_match = float(f["employer_401k_match_rate_of_wage"])
    fixed_other = float(f["fixed_other_benefits_usd"])
    software = float(f["software_overhead_usd"])

    if coverage == "high":
        health = float(f["health_high_coverage_usd"])
        paylocity = float(f["paylocity_overhead_high_usd"])
    elif coverage == "low":
        health = float(f["health_low_coverage_usd"])
        paylocity = float(f["paylocity_overhead_low_usd"])
    else:
        raise ValueError(f"Unknown coverage_level {coverage!r}; expected 'high' or 'low'")

    taxes = wage * medicare_rate + min(wage, fica_wage_base) * fica_rate + futa + wage * suta_rate
    additional_benefits = wage * (bonus_rate + employer_401k_match) + fixed_other

    return wage + health + taxes + paylocity + additional_benefits + software


def _ceiling_rate_for_category(labor_category: str) -> float:
    """GSA OLM ceiling rate. Bid rate must be ≤ ceiling."""
    rules = get_pricing_rules()
    for entry in rules["labor_catalog"]:
        if entry["category"] == labor_category:
            return float(entry["ceiling_hourly_rate_usd"])
    raise ValueError(f"Unknown labor_category {labor_category!r}; not in labor_catalog")


def _ga_hourly_addon(avg_headcount: float) -> float:
    """G&A hourly add-on per Jordan's allocation method (a) —
    annual_office_pool / (avg_headcount × annual_billable_hours).
    Recomputed per bid against the avg_headcount estimate."""
    rules = get_pricing_rules()
    pool = float(rules["ga_overhead"]["annual_office_pool_usd"])
    hours = float(rules["annual_billable_hours"])
    if avg_headcount <= 0:
        # Defensive — agent should always estimate ≥ current
        # headcount. Treat 0 as a flag and let caller raise meaningful
        # error rather than divide-by-zero.
        raise ValueError(f"avg_headcount must be > 0; got {avg_headcount}")
    return pool / (avg_headcount * hours)


# ---- Input dataclasses (from agent) ---------------------------------------


@dataclass
class CostAnalystLaborLine:
    """One labor-line judgment from the Cost Analyst LLM. Same
    structure feeds all three scenarios; the COMPUTED values per
    scenario differ.

    Optional override fields let the Cost tab's editable view (Custom
    slider) bypass the wrap-rate / margin formulas for a single line.
    Both default to None — when None, the standard math runs. When
    set, the override value is used directly (loaded_hourly_override
    bypasses the wage_band lookup; billed_hourly_override bypasses
    the margin formula).
    """

    labor_category: str
    wage_band: str
    hours: float
    rationale: str
    loaded_hourly_override_usd: float | None = None
    billed_hourly_override_usd: float | None = None


@dataclass
class CostAnalystOdc:
    """Other Direct Cost line — travel, equipment, training, etc.

    year_count multiplies the annual amount across the period of
    performance for recurring items (cloud hosting, license fees,
    subscription tools). Default 1 (single-year / one-time spend).
    Total ODC contribution = amount_usd × year_count.
    """

    item: str
    amount_usd: float
    justification: str
    year_count: int = 1


@dataclass
class CostAnalystPhaseAllocation:
    """One labor-category allocation within a lifecycle phase."""

    labor_category: str
    hours: float


@dataclass
class CostAnalystPhase:
    """One lifecycle phase from the Cost Analyst LLM."""

    name: str
    description: str
    start_month: int
    duration_months: float
    labor_allocations: list[CostAnalystPhaseAllocation] = field(
        default_factory=list,
    )


@dataclass
class CostAnalystOutput:
    """Full output of the Cost Analyst LLM call. Labor judgment +
    contextual estimates; NO dollar totals from the LLM."""

    labor_lines: list[CostAnalystLaborLine] = field(default_factory=list)
    avg_headcount_during_pop: float = 0.0
    odcs: list[CostAnalystOdc] = field(default_factory=list)
    subcontractor_costs_usd: float | None = None
    key_risks: list[str] = field(default_factory=list)
    executive_summary: str = ""
    lifecycle_phases: list[CostAnalystPhase] = field(default_factory=list)


# ---- Computed dataclasses (what gets persisted) ---------------------------


@dataclass
class ComputedLine:
    """One labor line within ONE scenario, with all dollars resolved."""

    labor_category: str
    wage_band: str
    coverage_level: str
    hours: float
    loaded_hourly_rate_usd: float
    loaded_cost_usd: float
    ga_allocation_usd: float
    proposed_billing_rate_usd: float
    billed_total_usd: float
    profit_per_hour_usd: float
    rationale: str
    loaded_hourly_override_usd: float | None = None
    billed_hourly_override_usd: float | None = None
    # Set when proposed_billing_rate exceeds the GSA OLM ceiling for
    # this category. Empty string when in-bounds.
    ceiling_violation_note: str = ""


@dataclass
class ComputedPhase:
    """One lifecycle phase with its computed costs for ONE scenario.
    Stored as a dict in pricing_packages.phase_breakdown_json."""

    name: str
    description: str
    start_month: int
    duration_months: float

    # Per-category labor allocations for this phase, with their
    # computed costs. Each allocation: {labor_category, hours,
    # loaded_hourly_rate_usd, loaded_cost_usd, ga_allocation_usd,
    # billed_total_usd}.
    labor_allocations: list[dict[str, Any]]

    # Aggregate phase totals (computed from the allocations).
    phase_total_hours: float
    phase_loaded_cost_usd: float
    phase_ga_usd: float
    phase_contingency_hours: float
    phase_contingency_cost_usd: float
    phase_subtotal_cost_usd: float
    phase_profit_usd: float
    phase_price_usd: float

    # Soft validation flag — sum of allocated hours per category vs
    # the labor_lines total. If under-allocated by a meaningful
    # margin, the LLM "left some hours unallocated" and we'd want to
    # surface that in the UI.
    allocation_warnings: list[str] = field(default_factory=list)


@dataclass
class ComputedScenarioPackage:
    """One scenario's full cost build, ready for persistence."""

    scenario: str  # PricingScenario value (LOW/MEDIUM/HIGH)
    avg_headcount_during_pop: float
    ga_hourly_addon_usd: float
    coverage_level: str
    profit_margin_pct: float
    contingency_hours_pct: float

    lines: list[ComputedLine]

    total_hours: float
    total_loaded_labor_cost_usd: float
    total_ga_allocation_usd: float
    contingency_hours: float
    contingency_cost_usd: float
    odcs_total_usd: float
    subcontractor_costs_usd: float
    total_subtotal_cost_usd: float

    profit_usd: float
    total_proposed_price_usd: float
    blended_hourly_rate_usd: float

    vs_market_position: str  # MarketPosition value
    bid_recommendation: str  # BidRecommendation value
    recommendation_rationale: str

    # Pre-formatted indirect_costs dict + pnl_projection dict for
    # PricingPackage's JSON columns (UI rendering convenience).
    indirect_costs: dict[str, Any]
    pnl_projection: dict[str, Any]
    odcs_persisted: list[dict[str, Any]]

    # Lifecycle phases — empty list if the agent didn't produce a
    # phase breakdown. Each phase is the dict-form of ComputedPhase.
    phases: list[dict[str, Any]] = field(default_factory=list)


# ---- Computation engine ---------------------------------------------------


def compute_scenario_packages(
    *,
    output: CostAnalystOutput,
    market_band_low_usd: float | None,
    market_band_mid_usd: float | None,
    market_band_high_usd: float | None,
) -> list[ComputedScenarioPackage]:
    """Apply the wrap rate formula + scenario_definitions to produce
    three ComputedScenarioPackage rows (LOW, MEDIUM, HIGH).

    Validates wage_band, labor_category, coverage_level against the
    rules JSON. Raises ValueError on unknowns — callers must surface
    these so the user fixes input rather than silently bidding bad
    numbers.
    """
    if not output.labor_lines:
        raise ValueError("Cost Analyst output has no labor_lines — cannot compute a cost build.")
    if output.avg_headcount_during_pop <= 0:
        raise ValueError(
            f"avg_headcount_during_pop must be > 0; agent returned {output.avg_headcount_during_pop}"
        )

    ga_hourly = _ga_hourly_addon(output.avg_headcount_during_pop)

    packages: list[ComputedScenarioPackage] = []
    for scenario_name in (PricingScenario.LOW, PricingScenario.MEDIUM, PricingScenario.HIGH):
        params = _scenario_params(scenario_name.value)
        packages.append(
            _compute_one_scenario_package(
                scenario_name=scenario_name.value,
                output=output,
                coverage=params["coverage_level"],
                margin_pct=params["profit_margin_pct"],
                contingency_pct=params["contingency_hours_pct"],
                ga_hourly=ga_hourly,
                market_band_low_usd=market_band_low_usd,
                market_band_mid_usd=market_band_mid_usd,
                market_band_high_usd=market_band_high_usd,
            )
        )

    return packages


def compute_custom_scenario_package(
    *,
    output: CostAnalystOutput,
    coverage_level: str,
    margin_pct: float,
    contingency_pct: float,
    market_band_low_usd: float | None,
    market_band_mid_usd: float | None,
    market_band_high_usd: float | None,
    scenario_label: str = "CUSTOM",
) -> ComputedScenarioPackage:
    """Compute one ComputedScenarioPackage for arbitrary user-chosen
    margin / contingency / coverage. Used by the Cost tab's slider
    view — same deterministic math, different parameter source.

    Does NOT persist; the caller renders the result in-memory. If
    the user wants to commit a custom posture as the proposed bid,
    that's a separate persist step (future work).
    """
    if not output.labor_lines:
        raise ValueError("Cost Analyst output has no labor_lines — cannot compute a cost build.")
    if output.avg_headcount_during_pop <= 0:
        raise ValueError(f"avg_headcount_during_pop must be > 0; got {output.avg_headcount_during_pop}")
    if coverage_level not in ("low", "high"):
        raise ValueError(f"coverage_level must be 'low' or 'high'; got {coverage_level!r}")
    if not (0.0 <= margin_pct < 1.0):
        raise ValueError(f"margin_pct must be in [0, 1); got {margin_pct}")
    if not (0.0 <= contingency_pct <= 1.0):
        raise ValueError(f"contingency_pct must be in [0, 1]; got {contingency_pct}")

    ga_hourly = _ga_hourly_addon(output.avg_headcount_during_pop)
    return _compute_one_scenario_package(
        scenario_name=scenario_label,
        output=output,
        coverage=coverage_level,
        margin_pct=margin_pct,
        contingency_pct=contingency_pct,
        ga_hourly=ga_hourly,
        market_band_low_usd=market_band_low_usd,
        market_band_mid_usd=market_band_mid_usd,
        market_band_high_usd=market_band_high_usd,
    )


def _compute_one_scenario_package(
    *,
    scenario_name: str,
    output: CostAnalystOutput,
    coverage: str,
    margin_pct: float,
    contingency_pct: float,
    ga_hourly: float,
    market_band_low_usd: float | None,
    market_band_mid_usd: float | None,
    market_band_high_usd: float | None,
) -> ComputedScenarioPackage:
    """Inner per-scenario computation. Same wrap-rate + margin-on-
    price math the policy scenarios use; the params are just supplied
    by the caller rather than pulled from the rules JSON."""
    lines: list[ComputedLine] = []
    total_hours = 0.0
    total_loaded = 0.0
    total_ga = 0.0
    total_billed = 0.0

    for ll in output.labor_lines:
        # Loaded $/hr — salary lookup, unless the line has a
        # manual override (set via the Cost tab's editable view).
        if ll.loaded_hourly_override_usd is not None:
            loaded_hourly = float(ll.loaded_hourly_override_usd)
        else:
            loaded_annual = _wage_band_loaded_annual(
                ll.wage_band,
                coverage,
            )
            loaded_hourly = loaded_annual / 1950.0  # annual_billable_hours
        loaded_cost = loaded_hourly * ll.hours
        ga_alloc = ga_hourly * ll.hours

        # Billed $/hr — margin-on-price formula, unless overridden.
        # When overridden, the user has set the line's rate directly
        # (e.g., to match a competitor's quote or stay under a
        # ceiling). Per-line margin becomes whatever falls out.
        line_cost_per_hour = loaded_hourly + ga_hourly
        if ll.billed_hourly_override_usd is not None:
            billing_rate = float(ll.billed_hourly_override_usd)
        else:
            billing_rate = line_cost_per_hour / (1.0 - margin_pct)
        billed_total = billing_rate * ll.hours
        profit_per_hour = billing_rate - line_cost_per_hour

        # Ceiling check vs GSA OLM rate.
        ceiling = _ceiling_rate_for_category(ll.labor_category)
        ceiling_note = ""
        if billing_rate > ceiling:
            ceiling_note = (
                f"Billing rate ${billing_rate:.2f}/hr exceeds GSA "
                f"OLM ceiling ${ceiling:.2f}/hr for "
                f"{ll.labor_category}. Drop margin or salary."
            )

        lines.append(
            ComputedLine(
                labor_category=ll.labor_category,
                wage_band=ll.wage_band,
                coverage_level=coverage,
                hours=ll.hours,
                loaded_hourly_rate_usd=_round_money(loaded_hourly),
                loaded_cost_usd=_round_money(loaded_cost),
                ga_allocation_usd=_round_money(ga_alloc),
                proposed_billing_rate_usd=_round_money(billing_rate),
                billed_total_usd=_round_money(billed_total),
                profit_per_hour_usd=_round_money(profit_per_hour),
                rationale=ll.rationale,
                loaded_hourly_override_usd=(
                    _round_money(float(ll.loaded_hourly_override_usd))
                    if ll.loaded_hourly_override_usd is not None
                    else None
                ),
                billed_hourly_override_usd=(
                    _round_money(float(ll.billed_hourly_override_usd))
                    if ll.billed_hourly_override_usd is not None
                    else None
                ),
                ceiling_violation_note=ceiling_note,
            )
        )

        total_hours += ll.hours
        total_loaded += loaded_cost
        total_ga += ga_alloc
        total_billed += billed_total

    # ---- Scenario-level aggregates ----
    contingency_hours = total_hours * contingency_pct
    # Contingency costs at the blended loaded hourly rate.
    blended_loaded_hourly = (total_loaded / total_hours) if total_hours > 0 else 0.0
    contingency_cost = contingency_hours * (blended_loaded_hourly + ga_hourly)

    # Each ODC contributes amount_usd × year_count to the cost
    # subtotal — recurring items (hosting, licenses) multiply across
    # the period of performance; one-time items default to year_count=1.
    odcs_total = sum(float(o.amount_usd) * max(1, int(o.year_count or 1)) for o in output.odcs)
    sub_costs = output.subcontractor_costs_usd or 0.0

    # Subtotal = direct labor + G&A + contingency + ODCs + subs.
    subtotal_cost = total_loaded + total_ga + contingency_cost + odcs_total + sub_costs
    # Proposed price has two components:
    #   labor_revenue   = sum of line billed_totals (respects per-line
    #                     billed_hourly_override_usd; without overrides
    #                     this equals (total_loaded + total_ga) /
    #                     (1 - margin_pct), so the result is identical
    #                     to the old "cost × markup" formula)
    #   non_labor_revenue = (contingency + ODCs + subs) priced at the
    #                       scenario margin
    # Net effect: line-level rate overrides propagate into the
    # scenario aggregate. Without overrides the math is unchanged.
    non_labor_cost = contingency_cost + odcs_total + sub_costs
    if margin_pct < 1.0:
        non_labor_revenue = non_labor_cost / (1.0 - margin_pct)
    else:
        non_labor_revenue = non_labor_cost  # avoid div-by-zero
    proposed_price = total_billed + non_labor_revenue
    profit = proposed_price - subtotal_cost

    # Blended billing rate across the bid: total price / total
    # hours. Includes contingency hours since those represent
    # actual delivery time the customer pays for.
    billable_hours = total_hours + contingency_hours
    blended_billing_rate = (proposed_price / billable_hours) if billable_hours > 0 else 0.0

    # ---- Vs-market position (deterministic) ----
    position = _market_position(
        proposed_price,
        market_band_low_usd,
        market_band_high_usd,
    )

    # ---- Bid recommendation (deterministic rules) ----
    recommendation, rec_rationale = _bid_recommendation(
        scenario=scenario_name,
        proposed_price=proposed_price,
        margin_pct=margin_pct,
        position=position,
        band_low=market_band_low_usd,
        band_high=market_band_high_usd,
    )

    # ---- JSON-column payloads for the PricingPackage row ----
    # effective_profit_pct is profit / proposed_price — equals
    # margin_pct when no per-line rate overrides are in play, but
    # diverges when the user has set custom Billed $/hr on any line
    # (the per-line margin doesn't equal the scenario nominal margin).
    # The UI uses this for the Profit metric subtitle so the displayed
    # margin reflects what's actually being bid.
    effective_profit_pct = (profit / proposed_price) if proposed_price > 0 else margin_pct
    indirect_costs = {
        "ga_hourly_addon_usd": _round_money(ga_hourly),
        "ga_total_usd": _round_money(total_ga),
        "contingency_hours": _round_money(contingency_hours),
        "contingency_cost_usd": _round_money(contingency_cost),
        "profit_pct": margin_pct,
        "effective_profit_pct": round(effective_profit_pct, 4),
        "profit_usd": _round_money(profit),
        "total_subtotal_cost_usd": _round_money(subtotal_cost),
    }
    pnl_projection = {
        "revenue": _round_money(proposed_price),
        "cogs": _round_money(subtotal_cost),
        "gross_margin": _round_money(profit),
        "gross_margin_pct": round(margin_pct, 4),
        "blended_hourly_rate": _round_money(blended_billing_rate),
        "total_billable_hours": _round_money(billable_hours),
        "vs_market_band_low_usd": _maybe_money(market_band_low_usd),
        "vs_market_band_mid_usd": _maybe_money(market_band_mid_usd),
        "vs_market_band_high_usd": _maybe_money(market_band_high_usd),
    }
    odcs_persisted = [
        {
            "item": o.item,
            "amount_usd": _round_money(o.amount_usd),
            "justification": o.justification,
            "year_count": max(1, int(o.year_count or 1)),
            # Pre-computed extended total for UI rendering convenience —
            # callers don't need to multiply themselves.
            "extended_amount_usd": _round_money(float(o.amount_usd) * max(1, int(o.year_count or 1))),
        }
        for o in output.odcs
    ]

    # Compute phase breakdown for this scenario. Phase definitions
    # are scenario-agnostic (LLM produces them once); the COMPUTED
    # phase costs differ per scenario because coverage / margin /
    # contingency vary.
    computed_phases = _compute_phases_for_scenario(
        phases=output.lifecycle_phases,
        labor_lines=output.labor_lines,
        lines_lookup={ln.labor_category: ln for ln in lines},
        ga_hourly=ga_hourly,
        margin_pct=margin_pct,
        contingency_pct=contingency_pct,
    )

    return ComputedScenarioPackage(
        scenario=scenario_name,
        avg_headcount_during_pop=output.avg_headcount_during_pop,
        ga_hourly_addon_usd=_round_money(ga_hourly),
        coverage_level=coverage,
        profit_margin_pct=margin_pct,
        contingency_hours_pct=contingency_pct,
        lines=lines,
        total_hours=_round_money(total_hours),
        total_loaded_labor_cost_usd=_round_money(total_loaded),
        total_ga_allocation_usd=_round_money(total_ga),
        contingency_hours=_round_money(contingency_hours),
        contingency_cost_usd=_round_money(contingency_cost),
        odcs_total_usd=_round_money(odcs_total),
        subcontractor_costs_usd=_round_money(sub_costs),
        total_subtotal_cost_usd=_round_money(subtotal_cost),
        profit_usd=_round_money(profit),
        total_proposed_price_usd=_round_money(proposed_price),
        blended_hourly_rate_usd=_round_money(blended_billing_rate),
        vs_market_position=position,
        bid_recommendation=recommendation,
        recommendation_rationale=rec_rationale,
        indirect_costs=indirect_costs,
        pnl_projection=pnl_projection,
        odcs_persisted=odcs_persisted,
        phases=computed_phases,
    )


def _compute_phases_for_scenario(
    *,
    phases: list,
    labor_lines: list,
    lines_lookup: dict,
    ga_hourly: float,
    margin_pct: float,
    contingency_pct: float,
) -> list[dict[str, Any]]:
    """Compute per-phase costs for ONE scenario. The lines_lookup
    holds the already-computed ComputedLine for each labor_category
    in this scenario, so we can pull loaded_hourly_rate (which differs
    per scenario via coverage_level) without re-running the wrap
    formula.

    Returns a list of phase dicts ready to persist as JSON. Empty
    list when the LLM didn't produce phases — UI handles missing
    data gracefully.
    """
    if not phases:
        return []

    # Total hours per labor_category — used to validate phase
    # allocations don't over-allocate any category.
    total_hours_by_category: dict[str, float] = {}
    for ll in labor_lines:
        total_hours_by_category[ll.labor_category] = (
            total_hours_by_category.get(ll.labor_category, 0.0) + ll.hours
        )

    # First pass: collect allocated hours per category across all
    # phases. We use this for over-allocation warnings and clamping.
    allocated_hours_by_category: dict[str, float] = {}
    for phase in phases:
        for alloc in phase.labor_allocations:
            allocated_hours_by_category[alloc.labor_category] = (
                allocated_hours_by_category.get(alloc.labor_category, 0.0) + alloc.hours
            )

    computed_phases: list[dict[str, Any]] = []
    for phase in phases:
        phase_loaded = 0.0
        phase_ga = 0.0
        phase_hours = 0.0
        phase_billed = 0.0

        phase_warnings: list[str] = []
        allocations_persisted: list[dict[str, Any]] = []

        for alloc in phase.labor_allocations:
            cat = alloc.labor_category
            line = lines_lookup.get(cat)
            if line is None:
                phase_warnings.append(
                    f"Allocation for '{cat}' has no matching labor_line in this scenario — skipped."
                )
                continue
            hrs = float(alloc.hours)
            if hrs <= 0:
                phase_warnings.append(f"Allocation for '{cat}' has hours={hrs}; skipped.")
                continue

            loaded_rate = line.loaded_hourly_rate_usd
            loaded = loaded_rate * hrs
            ga = ga_hourly * hrs
            # Apply margin formula at the phase level too so the per-
            # phase price rolls up cleanly. Same coverage / margin /
            # contingency policy as the scenario-level math.
            billing_rate = (loaded_rate + ga_hourly) / (1.0 - margin_pct)
            billed = billing_rate * hrs

            allocations_persisted.append(
                {
                    "labor_category": cat,
                    "hours": _round_money(hrs),
                    "loaded_hourly_rate_usd": _round_money(loaded_rate),
                    "loaded_cost_usd": _round_money(loaded),
                    "ga_allocation_usd": _round_money(ga),
                    "proposed_billing_rate_usd": _round_money(billing_rate),
                    "billed_total_usd": _round_money(billed),
                }
            )
            phase_loaded += loaded
            phase_ga += ga
            phase_hours += hrs
            phase_billed += billed

        # Phase contingency = phase_hours × contingency_pct, costed at
        # the phase's blended loaded+G&A hourly. Distributed pro rata
        # to each phase rather than dumped into one bucket so the
        # narrative reflects realistic phase-level reserves.
        phase_contingency_hrs = phase_hours * contingency_pct
        phase_blended_loaded = (phase_loaded / phase_hours) if phase_hours > 0 else 0.0
        phase_contingency_cost = phase_contingency_hrs * (phase_blended_loaded + ga_hourly)

        # Phase subtotal + price (margin-on-price, same formula as the
        # scenario-level aggregate).
        phase_subtotal = phase_loaded + phase_ga + phase_contingency_cost
        phase_price = phase_subtotal / (1.0 - margin_pct) if margin_pct < 1.0 else phase_subtotal
        phase_profit = phase_price - phase_subtotal

        computed_phases.append(
            {
                "name": phase.name,
                "description": phase.description,
                "start_month": int(phase.start_month),
                "duration_months": _round_money(phase.duration_months),
                "labor_allocations": allocations_persisted,
                "phase_total_hours": _round_money(phase_hours),
                "phase_loaded_cost_usd": _round_money(phase_loaded),
                "phase_ga_usd": _round_money(phase_ga),
                "phase_contingency_hours": _round_money(phase_contingency_hrs),
                "phase_contingency_cost_usd": _round_money(phase_contingency_cost),
                "phase_subtotal_cost_usd": _round_money(phase_subtotal),
                "phase_profit_usd": _round_money(phase_profit),
                "phase_price_usd": _round_money(phase_price),
                "allocation_warnings": phase_warnings,
            }
        )

    # Append over-allocation warnings as a synthetic last entry so the
    # UI can surface them without traversing every phase. Keys are
    # category-level not phase-level.
    over_allocations: list[str] = []
    under_allocations: list[str] = []
    for cat, allocated in allocated_hours_by_category.items():
        total = total_hours_by_category.get(cat, 0.0)
        if total <= 0:
            continue
        ratio = allocated / total
        if ratio > 1.001:  # ~0.1% slack for rounding
            over_allocations.append(
                f"{cat}: phases allocate {allocated:,.0f} hrs vs "
                f"labor_lines total {total:,.0f} hrs — "
                f"over-allocated by {(ratio - 1.0):.1%}."
            )
        elif ratio < 0.95:
            under_allocations.append(
                f"{cat}: phases allocate {allocated:,.0f} hrs vs "
                f"labor_lines total {total:,.0f} hrs — "
                f"unallocated balance {(1.0 - ratio):.1%}."
            )

    if over_allocations or under_allocations:
        computed_phases.append(
            {
                "_synthetic_summary": True,
                "name": "Allocation balance",
                "over_allocations": over_allocations,
                "under_allocations": under_allocations,
            }
        )

    return computed_phases


def _market_position(
    price: float,
    band_low: float | None,
    band_high: float | None,
) -> str:
    """Deterministic position label based on price vs band edges.
    Uses the explicit band_low / band_high; if either is missing we
    can't classify, return BELOW (most conservative — encourages
    flagging that the band is unknown)."""
    if band_low is None or band_high is None:
        return MarketPosition.IN_BAND.value  # neutral when we don't know
    if price < band_low:
        return MarketPosition.BELOW.value
    if price > band_high:
        return MarketPosition.ABOVE.value
    return MarketPosition.IN_BAND.value


def _bid_recommendation(
    *,
    scenario: str,
    proposed_price: float,
    margin_pct: float,
    position: str,
    band_low: float | None,
    band_high: float | None,
) -> tuple[str, str]:
    """Apply the deterministic recommendation rules. Returns (
    recommendation, rationale_text). Rules:
      - margin < floor (18%) → walk_away
      - price > 1.3× band_high → flag_for_review (priced out)
      - price < 0.7× band_low → flag_for_review (under-pricing)
      - else → bid
    """
    rules = get_pricing_rules()
    floor = float(rules["profit_policy"]["floor_margin_pct"])

    if margin_pct < floor:
        return (
            BidRecommendation.WALK_AWAY.value,
            f"Margin {margin_pct:.1%} is below floor {floor:.0%}. "
            f"Bid is uneconomical at this scenario; walk away or "
            f"adjust labor mix.",
        )

    if band_high is not None and proposed_price > 1.3 * band_high:
        return (
            BidRecommendation.FLAG_FOR_REVIEW.value,
            f"Proposed ${proposed_price:,.0f} is >1.3× market band "
            f"high (${band_high:,.0f}). Likely priced out — review "
            f"scope or salaries.",
        )

    if band_low is not None and proposed_price < 0.7 * band_low:
        return (
            BidRecommendation.FLAG_FOR_REVIEW.value,
            f"Proposed ${proposed_price:,.0f} is <0.7× market band "
            f"low (${band_low:,.0f}). May be under-pricing — verify "
            f"scope coverage before bidding.",
        )

    return (
        BidRecommendation.BID.value,
        f"{scenario} scenario: margin {margin_pct:.1%}, position {position}. Within policy; bid.",
    )


def _round_money(v: float) -> float:
    """Round to cents to match Numeric(*, 2) in the DB."""
    return float(Decimal(str(v)).quantize(Decimal("0.01")))


def _maybe_money(v: float | None) -> float | None:
    return _round_money(v) if v is not None else None


# ---- Persistence ----------------------------------------------------------


def upsert_pricing_packages(
    *,
    proposal_id: int,
    packages: list[ComputedScenarioPackage],
    market_scan_id: int | None,
    agent_run_id: int | None,
    executive_summary: str = "",
) -> list[int]:
    """Replace existing PricingPackage rows for the proposal with the
    new H/M/L set. Cascade on the lines table clears old detail rows.
    Returns list of PricingPackage.id values in scenario order."""
    with session_scope() as db:
        ensure_proposal_mutable(
            db, proposal_id, operation="replace proposal pricing",
        )
        existing_rows = db.execute(
            select(PricingPackage)
            .where(PricingPackage.proposal_id == proposal_id)
        ).scalars().all()
        if existing_rows:
            invalidate_cost_review(
                db,
                proposal_id,
                reason="Pricing packages were replaced; rerun Cost Reviewer.",
            )
        for ex in existing_rows:
            db.delete(ex)
        if existing_rows:
            db.flush()

        new_ids: list[int] = []
        for pkg in packages:
            pp = PricingPackage(
                proposal_id=proposal_id,
                scenario=pkg.scenario,
                market_scan_id=market_scan_id,
                agent_run_id=agent_run_id,
                loaded_labor_cost=pkg.total_loaded_labor_cost_usd,
                odcs_json=pkg.odcs_persisted,
                subcontractor_costs=pkg.subcontractor_costs_usd,
                indirect_costs_json=pkg.indirect_costs,
                total_proposed_price=pkg.total_proposed_price_usd,
                pnl_projection_json=_compose_pnl_projection(
                    pkg,
                    executive_summary,
                ),
                phase_breakdown_json=(pkg.phases or None),
                vs_market_position=pkg.vs_market_position,
                bid_recommendation=pkg.bid_recommendation,
                recommendation_rationale=pkg.recommendation_rationale,
            )
            db.add(pp)
            db.flush()  # need pp.id for the lines FK

            for line in pkg.lines:
                db.add(
                    PricingPackageLine(
                        pricing_package_id=pp.id,
                        labor_category=line.labor_category,
                        wage_band=line.wage_band,
                        coverage_level=line.coverage_level,
                        hours=line.hours,
                        loaded_hourly_rate_usd=line.loaded_hourly_rate_usd,
                        loaded_cost_usd=line.loaded_cost_usd,
                        ga_allocation_usd=line.ga_allocation_usd,
                        proposed_billing_rate_usd=line.proposed_billing_rate_usd,
                        billed_total_usd=line.billed_total_usd,
                        profit_per_hour_usd=line.profit_per_hour_usd,
                        loaded_hourly_override_usd=line.loaded_hourly_override_usd,
                        billed_hourly_override_usd=line.billed_hourly_override_usd,
                        rationale=_compose_line_rationale(line),
                    )
                )

            db.flush()
            new_ids.append(pp.id)

    log.info(
        "pricing_packages: upserted %d scenarios for proposal %d (ids=%s)",
        len(new_ids),
        proposal_id,
        new_ids,
    )
    return new_ids


def _compose_pnl_projection(
    pkg: ComputedScenarioPackage,
    executive_summary: str,
) -> dict[str, Any]:
    """Combine the computed P&L numbers with the agent's narrative
    so both render in the UI without needing a separate column."""
    out = dict(pkg.pnl_projection)
    if executive_summary and executive_summary.strip():
        out["executive_summary"] = executive_summary.strip()
    return out


def _compose_line_rationale(line: ComputedLine) -> str:
    """Append ceiling-violation notes to the LLM-provided rationale
    so the user sees the warning inline on the Cost tab."""
    parts: list[str] = []
    if line.rationale and line.rationale.strip():
        parts.append(line.rationale.strip())
    if line.ceiling_violation_note:
        parts.append(f"WARNING: {line.ceiling_violation_note}")
    return "\n\n".join(parts) if parts else None


def reconstruct_cost_analyst_output(
    pricing_packages_snapshot: list[dict[str, Any]],
) -> CostAnalystOutput | None:
    """Rebuild the agent's CostAnalystOutput from persisted package
    snapshots. Used by the Cost tab's slider view to recompute custom
    scenarios without re-running the LLM.

    Pulls labor_lines + ODCs + phases from any one persisted scenario
    (those fields are scenario-agnostic). Returns None when no
    packages are persisted.
    """
    if not pricing_packages_snapshot:
        return None

    # Pick MEDIUM as the source of truth since it has phases populated
    # and matches the proposed-scenario default. Fall back to whichever
    # scenario is present.
    source = next(
        (p for p in pricing_packages_snapshot if p["scenario"] == "MEDIUM"),
        pricing_packages_snapshot[0],
    )

    # Reconstruct labor_lines from per-FTE rows. category + wage_band +
    # hours + rationale are scenario-agnostic; the computed values are
    # per-scenario but we discard those here (the math layer recomputes).
    # Note: rationale may include the appended "WARNING:" ceiling note;
    # keep it as-is so the same rationale renders in the slider view.
    labor_lines: list[CostAnalystLaborLine] = []
    for ln in source.get("lines") or []:
        try:
            labor_lines.append(
                CostAnalystLaborLine(
                    labor_category=str(ln["labor_category"]),
                    wage_band=str(ln["wage_band"]),
                    hours=float(ln["hours"]),
                    rationale=str(ln.get("rationale") or ""),
                    loaded_hourly_override_usd=(
                        float(ln["loaded_hourly_override_usd"])
                        if ln.get("loaded_hourly_override_usd") is not None
                        else None
                    ),
                    billed_hourly_override_usd=(
                        float(ln["billed_hourly_override_usd"])
                        if ln.get("billed_hourly_override_usd") is not None
                        else None
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    # ODCs come back from odcs_json. year_count defaults to 1 for
    # rows persisted before the multi-year column was added.
    odcs: list[CostAnalystOdc] = []
    for o in source.get("odcs_json") or []:
        try:
            odcs.append(
                CostAnalystOdc(
                    item=str(o["item"]),
                    amount_usd=float(o["amount_usd"]),
                    justification=str(o.get("justification") or ""),
                    year_count=int(o.get("year_count") or 1),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    # avg_headcount_during_pop isn't a column — back-derive from the
    # G&A hourly add-on: ga_hourly = pool / (headcount * 1950).
    indirect = source.get("indirect_costs_json") or {}
    ga_hourly = float(indirect.get("ga_hourly_addon_usd") or 0.0)
    rules = get_pricing_rules()
    pool = float(rules["ga_overhead"]["annual_office_pool_usd"])
    annual_hrs = float(rules["annual_billable_hours"])
    if ga_hourly > 0:
        avg_headcount = pool / (ga_hourly * annual_hrs)
    else:
        avg_headcount = 1.0  # defensive — won't be used since math validates

    # Phases come back from phase_breakdown_json with computed values
    # per scenario. The DEFINITIONS (name, description, start_month,
    # duration_months, labor_allocations.{category, hours}) are
    # scenario-agnostic — pull those.
    phases: list[CostAnalystPhase] = []
    for ph in source.get("phase_breakdown_json") or []:
        if ph.get("_synthetic_summary"):
            continue
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
                except (KeyError, TypeError, ValueError):
                    continue
            phases.append(
                CostAnalystPhase(
                    name=str(ph["name"]),
                    description=str(ph.get("description") or ""),
                    start_month=int(ph.get("start_month") or 1),
                    duration_months=float(ph.get("duration_months") or 0),
                    labor_allocations=allocations,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    # executive_summary is in pnl_projection_json.
    pnl = source.get("pnl_projection_json") or {}
    exec_summary = str(pnl.get("executive_summary") or "")

    sub_costs = source.get("subcontractor_costs")
    sub_costs = float(sub_costs) if sub_costs is not None else None

    return CostAnalystOutput(
        labor_lines=labor_lines,
        avg_headcount_during_pop=avg_headcount,
        odcs=odcs,
        subcontractor_costs_usd=sub_costs,
        key_risks=[],  # not persisted; not needed for recomputation
        executive_summary=exec_summary,
        lifecycle_phases=phases,
    )


def apply_cost_build_edits_to_output(
    output: CostAnalystOutput,
    *,
    labor_edits: dict[str, dict[str, Any]] | None = None,
    odc_edits: list[dict[str, Any]] | None = None,
) -> CostAnalystOutput:
    """Return a copy of a cost build with user-entered labor/ODC edits.

    Labor edits are keyed by zero-based line index so duplicate labor
    categories remain independently editable. Category keys are retained as a
    compatibility fallback for older saved UI state.
    """
    labor_edits = labor_edits or {}
    new_lines: list[CostAnalystLaborLine] = []
    for index, line in enumerate(output.labor_lines):
        edit = labor_edits.get(str(index)) or labor_edits.get(line.labor_category) or {}
        new_lines.append(
            CostAnalystLaborLine(
                labor_category=line.labor_category,
                wage_band=str(edit.get("wage_band") or line.wage_band),
                hours=float(edit["hours"] if edit.get("hours") is not None else line.hours),
                rationale=line.rationale,
                loaded_hourly_override_usd=(
                    edit.get("loaded_override")
                    if "loaded_override" in edit
                    else line.loaded_hourly_override_usd
                ),
                billed_hourly_override_usd=(
                    edit.get("billed_override")
                    if "billed_override" in edit
                    else line.billed_hourly_override_usd
                ),
            )
        )

    if odc_edits is None:
        new_odcs = list(output.odcs)
    else:
        new_odcs: list[CostAnalystOdc] = []
        for edit in odc_edits:
            try:
                year_count = int(edit.get("year_count") or 1)
            except (TypeError, ValueError):
                year_count = 1
            try:
                amount = float(edit.get("amount_usd") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            new_odcs.append(
                CostAnalystOdc(
                    item=str(edit.get("item") or ""),
                    amount_usd=max(0.0, amount),
                    justification=str(edit.get("justification") or ""),
                    year_count=max(1, year_count),
                )
            )

    return CostAnalystOutput(
        labor_lines=new_lines,
        avg_headcount_during_pop=output.avg_headcount_during_pop,
        odcs=new_odcs,
        subcontractor_costs_usd=output.subcontractor_costs_usd,
        key_risks=output.key_risks,
        executive_summary=output.executive_summary,
        lifecycle_phases=output.lifecycle_phases,
    )


_VALID_PROPOSED_SCENARIOS = ("LOW", "MEDIUM", "HIGH")


def get_proposed_scenario(proposal_id: int) -> str:
    """Read the user's persisted scenario choice for this proposal.
    Returns 'MEDIUM' when nothing has been picked yet — that matches
    the legacy hardcoded default and keeps Cost Writer / Cost Reviewer
    behavior identical for proposals from before this column existed.
    """
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return "MEDIUM"
        v = (p.proposed_scenario or "").upper().strip()
        if v in _VALID_PROPOSED_SCENARIOS:
            return v
        return "MEDIUM"


def set_proposed_scenario(proposal_id: int, scenario: str) -> str:
    """Persist the user's scenario choice. Validates against
    LOW / MEDIUM / HIGH — CUSTOM is rejected (CUSTOM is an in-memory
    what-if; no PricingPackage row exists for it, so persisting it
    would break the Cost Writer's lookup). Returns the value that
    was stored so callers can mirror it in their UI state.
    """
    target = (scenario or "").upper().strip()
    if target not in _VALID_PROPOSED_SCENARIOS:
        raise ValueError(
            f"invalid proposed scenario {scenario!r}; expected one of {_VALID_PROPOSED_SCENARIOS}"
        )
    with session_scope() as db:
        p = ensure_proposal_mutable(
            db, proposal_id, operation="select pricing scenario",
        )
        if p is None:
            raise ValueError(f"proposal {proposal_id} not found")
        previous = (p.proposed_scenario or "MEDIUM").upper().strip()
        if previous != target:
            invalidate_cost_review(
                db,
                proposal_id,
                reason=(
                    f"Selected pricing scenario changed from {previous} "
                    f"to {target}; rerun Cost Reviewer."
                ),
            )
        p.proposed_scenario = target
    return target


def _format_payment_systems_cost_block(proposal_id: int) -> str:
    """Render the cost-build block for service_line=payment_systems.
    Pulls fee schedule + brand framing + risk talking points from
    data/pricing/payment_systems.json and _payment_systems_context.
    json. Returns an empty string when both files are missing — the
    writer falls back to NEEDS_HUMAN behavior in that case."""
    from app.services.service_line import (
        load_payment_systems_context,
        load_payment_systems_pricing,
    )

    pricing = load_payment_systems_pricing()
    context = load_payment_systems_context()
    if not pricing and not context:
        return ""

    lines: list[str] = ["=== APPROVED COST BUILD (Payment Systems) ==="]
    lines.append(
        "Service line: payment_systems. The cost narrative MUST be "
        "drawn from the data below, not from a labor catalog. Any "
        "[NEEDS_HUMAN] markers below are fields the user has not yet "
        "filled in — leave them visible in the draft."
    )
    lines.append("")

    bf = context.get("brand_framing") or {}
    if bf:
        lines.append("--- Brand framing ---")
        if bf.get("legal_proposer"):
            lines.append(f"Legal proposer: {bf['legal_proposer']}")
        if bf.get("service_division"):
            lines.append(f"Service division: {bf['service_division']}")
        if bf.get("years_in_payments"):
            lines.append(f"Years in payments operations: {bf['years_in_payments']}")
        if bf.get("positioning_one_liner"):
            lines.append(f"Positioning: {bf['positioning_one_liner']}")
        if bf.get("writer_voice_directive"):
            lines.append(f"WRITER VOICE: {bf['writer_voice_directive']}")
        lines.append("")

    vao = pricing.get("volume_adjusted_offering") or {}
    if vao:
        lines.append("--- Pricing posture ---")
        lines.append(f"Pricing strategy: {vao.get('pricing_strategy', 'market_rate_driven')}")
        if vao.get("preferred_model"):
            lines.append(f"Preferred model: {vao['preferred_model']}")
        if vao.get("_pricing_strategy_directive"):
            lines.append(f"Strategy directive: {vao['_pricing_strategy_directive']}")
        if vao.get("_writer_guidance"):
            lines.append(f"Writer guidance: {vao['_writer_guidance']}")
        lines.append("")

    hw = pricing.get("hardware_options") or {}
    if hw:
        lines.append("--- Hardware ---")
        if hw.get("_status"):
            lines.append(f"Status: {hw['_status']}")
        if hw.get("qualified_partner_brands"):
            lines.append(f"Qualified partner brands: {', '.join(hw['qualified_partner_brands'])}")
        if hw.get("partner_selection_guidance"):
            lines.append(f"Selection guidance: {hw['partner_selection_guidance']}")
        if hw.get("_writer_guidance"):
            lines.append(f"Writer guidance: {hw['_writer_guidance']}")
        lines.append("")

    ca = pricing.get("compliance_attestations") or {}
    if ca:
        lines.append("--- Compliance attestations ---")
        if ca.get("pci_dss_level"):
            target = ca.get("pci_dss_target_level")
            months = ca.get("pci_dss_target_attainment_within_months")
            roadmap = f" — roadmap to Level {target} within {months} months" if target and months else ""
            lines.append(f"PCI DSS: Level {ca['pci_dss_level']}{roadmap}")
        if ca.get("nacha_member"):
            lines.append("NACHA: Active member (40+ years of ACH/EFT operations).")
        if ca.get("tokenization_supported") is False and ca.get("tokenization_target_date"):
            lines.append(f"Tokenization: in development, target launch {ca['tokenization_target_date']}.")
        elif ca.get("tokenization_supported") is True:
            lines.append("Tokenization: supported.")
        if ca.get("encryption_end_to_end"):
            lines.append("Encryption: end-to-end (TLS 1.2+ in transit, AES-256 at rest).")
        if ca.get("data_residency") == "us_only":
            lines.append("Data residency: U.S.-only — no cross-border storage or processing.")
        if ca.get("emv_status_explanation"):
            lines.append(f"EMV: {ca['emv_status_explanation']}")
        if ca.get("p2pe_status_explanation"):
            lines.append(f"P2PE: {ca['p2pe_status_explanation']}")
        if ca.get("pci_dss_disclosure_guidance"):
            lines.append(f"PCI disclosure guidance: {ca['pci_dss_disclosure_guidance']}")
        lines.append("")

    sfs = pricing.get("standard_fee_schedule") or {}
    if sfs:
        lines.append("--- Standard fee schedule (small-portfolio reference, NOT the proposed offer) ---")
        rates = sfs.get("payment_collection_rates_pct") or {}
        for channel, rate in rates.items():
            if channel.startswith("_"):
                continue
            try:
                lines.append(f"  {channel}: {float(rate):.2%}")
            except (TypeError, ValueError):
                lines.append(f"  {channel}: {rate}")
        if sfs.get("minimum_per_item_usd"):
            lines.append(f"  Minimum per item: ${sfs['minimum_per_item_usd']}")
        if sfs.get("_source"):
            lines.append(f"  Source: {sfs['_source']}")
        lines.append("")

    risks = (context.get("fit_risk_talking_points") or {}).get("risks") or []
    if risks:
        lines.append("--- Fit-risk talking points (writer MUST address head-on, not dance around) ---")
        for r in risks:
            rid = r.get("risk_id", "")
            lines.append(f"[{rid}] {r.get('risk', '')}")
            if r.get("address_in"):
                lines.append(f"  Address in: {r['address_in']}")
            for tp in r.get("talking_points") or []:
                lines.append(f"  • {tp}")
            lines.append("")

    cs = context.get("capability_spotlight") or {}
    if cs:
        lines.append("--- Capability spotlight ---")
        if cs.get("lead_with"):
            lines.append(f"Lead with: {', '.join(cs['lead_with'])}")
        if cs.get("support_with"):
            lines.append(f"Support with: {', '.join(cs['support_with'])}")
        if cs.get("downplay"):
            lines.append(f"Downplay: {', '.join(cs['downplay'])}")
        lines.append("")

    ps = context.get("personnel_spotlight") or {}
    if ps:
        lines.append("--- Personnel spotlight ---")
        for entry in ps.get("feature_for_government_credibility") or []:
            lines.append(f"  • {entry}")
        for entry in ps.get("feature_for_payments_operations") or []:
            lines.append(f"  • {entry}")
        lines.append("")

    pp = context.get("past_performance_signals") or {}
    if pp:
        lines.append("--- Past performance signals ---")
        for ga in pp.get("government_adjacent") or []:
            client = ga.get("client", "?")
            svc = ga.get("service", "")
            caveat = ga.get("framing_caveat", "")
            lines.append(f"  • {client} ({svc}) — CAVEAT: {caveat}")
        for ri in pp.get("regulated_industry") or []:
            vert = ri.get("vertical", "?")
            rel = ri.get("relationship_type", "")
            lines.append(f"  • {vert}: {rel}")
        if pp.get("_past_performance_directive"):
            lines.append(f"Directive: {pp['_past_performance_directive']}")
        lines.append("")

    na = context.get("narrative_anchors") or {}
    if na:
        if na.get("elevator_pitch_30_words"):
            lines.append(f"Elevator pitch: {na['elevator_pitch_30_words']}")
        if na.get("company_history_one_paragraph"):
            lines.append(f"Company history (one paragraph): {na['company_history_one_paragraph']}")
        lines.append("")

    # Payment market scan — when the orchestrator has run the Payment
    # Market Researcher, its output (recommended pricing structure +
    # volume estimate + profit math) gets rendered here so the writer
    # can cite specific market data in narrative instead of falling
    # back to the typical_county_tier_ranges_for_fallback midpoints.
    scan_block = _format_payment_market_scan(proposal_id)
    if scan_block:
        lines.append(scan_block)

    return "\n".join(lines).rstrip() + "\n"


def _format_payment_market_scan(proposal_id: int) -> str:
    """Render the Payment Market Researcher's persisted output (if any)
    as a block the writer can cite. Returns empty string when the scan
    hasn't been run yet — the writer falls back to fallback ranges in
    that case."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return ""
        raw = p.payment_market_scan_json
    if not raw or not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(
            "payment_market_scan_json invalid JSON on proposal %d",
            proposal_id,
        )
        return ""

    # Honor the user's pricing-model override when set. The agent's
    # researched rates remain — but the model LABEL the writer
    # narrates uses the user's choice. When user selection differs
    # from agent recommendation, the cost block surfaces an explicit
    # OVERRIDE NOTICE so the writer narrates the user's chosen model
    # without contradicting itself.
    from app.services.service_line import get_selected_pricing_model

    parts: list[str] = [
        "--- Payment Market Researcher output (use these specifics, not the fallback ranges) ---"
    ]

    ps = data.get("pricing_structure") or {}
    agent_recommended_model = (ps.get("pricing_model") or "").strip()
    user_selected_model = get_selected_pricing_model(proposal_id)
    effective_model = user_selected_model or agent_recommended_model
    if user_selected_model and agent_recommended_model and user_selected_model != agent_recommended_model:
        parts.append(
            f"USER PRICING-MODEL OVERRIDE: agent recommended "
            f"`{agent_recommended_model}`, but user selected "
            f"`{user_selected_model}` for this bid. Narrate the "
            f"proposal as `{user_selected_model}`. Specific rate "
            f"values below were researched at the agent's "
            f"recommended model — disclose in narrative if the user "
            f"hasn't yet re-run the scan with their model focus."
        )
        parts.append("")
    if ps:
        parts.append("Pricing recommendation:")
        if effective_model:
            parts.append(f"  Model: {effective_model}")
        if ps.get("pricing_model_rationale"):
            parts.append(f"  Rationale: {ps['pricing_model_rationale']}")
        if ps.get("rate_positioning"):
            parts.append(f"  Rate positioning: {ps['rate_positioning']}")
        if (
            ps.get("median_market_credit_card_markup_bps") is not None
            and ps.get("proposed_credit_card_markup_bps") is not None
        ):
            parts.append(
                f"  Credit-card markup: {ps['proposed_credit_card_markup_bps']} bps "
                f"(market median {ps['median_market_credit_card_markup_bps']} bps)"
            )
        if ps.get("proposed_per_txn_fee_usd") is not None:
            median = ps.get("median_market_per_txn_fee_usd")
            median_str = f" (market median ${median:.2f})" if median is not None else ""
            parts.append(f"  Per-transaction fee: ${ps['proposed_per_txn_fee_usd']:.2f}{median_str}")
        if ps.get("proposed_ach_fee_usd") is not None:
            median = ps.get("median_market_ach_fee_usd")
            median_str = f" (market median ${median:.2f})" if median is not None else ""
            parts.append(f"  ACH fee: ${ps['proposed_ach_fee_usd']:.2f}{median_str}")
        if ps.get("proposed_monthly_fee_usd") is not None:
            median = ps.get("median_market_monthly_fee_usd")
            median_str = f" (market median ${median:.2f})" if median is not None else ""
            parts.append(f"  Monthly fee: ${ps['proposed_monthly_fee_usd']:.2f}{median_str}")
        for fee in ps.get("other_fees_recommended") or []:
            name = fee.get("name", "?")
            amt = fee.get("amount_usd")
            amt_str = f" ${amt:.2f}" if amt is not None else " (custom)"
            note = f" — {fee['notes']}" if fee.get("notes") else ""
            parts.append(f"  Other fee: {name}{amt_str}{note}")
        parts.append("")

    ve = data.get("volume_estimate") or {}
    if ve and any(
        ve.get(k) is not None
        for k in (
            "annual_processed_volume_low_usd",
            "annual_processed_volume_midpoint_usd",
            "annual_processed_volume_high_usd",
        )
    ):
        parts.append("Annual processed-volume estimate:")
        if ve.get("annual_processed_volume_low_usd") is not None:
            parts.append(f"  Low: ${ve['annual_processed_volume_low_usd']:,.0f}")
        if ve.get("annual_processed_volume_midpoint_usd") is not None:
            parts.append(f"  Midpoint: ${ve['annual_processed_volume_midpoint_usd']:,.0f}")
        if ve.get("annual_processed_volume_high_usd") is not None:
            parts.append(f"  High: ${ve['annual_processed_volume_high_usd']:,.0f}")
        if ve.get("estimated_transaction_count_annual"):
            parts.append(
                f"  Estimated annual transaction count: {ve['estimated_transaction_count_annual']:,}"
            )
        if ve.get("average_transaction_size_usd"):
            parts.append(f"  Average transaction size: ${ve['average_transaction_size_usd']:.2f}")
        if ve.get("estimation_basis"):
            parts.append(f"  Estimation basis: {ve['estimation_basis']}")
        if ve.get("confidence"):
            parts.append(f"  Confidence: {ve['confidence']}")
        parts.append("")

    pm = data.get("profit_math") or {}
    if pm and pm.get("annual_processor_revenue_midpoint_usd") is not None:
        parts.append("Profit projection (volume × rate − cost basis):")
        if pm.get("annual_processor_revenue_low_usd") is not None:
            parts.append(
                f"  Annual revenue (low/mid/high): ${pm['annual_processor_revenue_low_usd']:,.0f} / ${pm.get('annual_processor_revenue_midpoint_usd', 0):,.0f} / ${pm.get('annual_processor_revenue_high_usd', 0):,.0f}"
            )
        else:
            parts.append(f"  Annual revenue (midpoint): ${pm['annual_processor_revenue_midpoint_usd']:,.0f}")
        if pm.get("annual_internal_costs_usd") is not None:
            parts.append(f"  Annual internal costs (midpoint): ${pm['annual_internal_costs_usd']:,.0f}")
        if pm.get("annual_net_profit_midpoint_usd") is not None:
            margin = pm.get("profit_margin_pct_at_midpoint")
            margin_str = f" ({margin:.1%} margin)" if margin is not None else ""
            parts.append(f"  Net profit (midpoint): ${pm['annual_net_profit_midpoint_usd']:,.0f}{margin_str}")
        if pm.get("computation_notes"):
            parts.append(f"  Computation: {pm['computation_notes']}")
        for caveat in pm.get("cost_basis_assumptions") or []:
            parts.append(f"  CAVEAT: {caveat}")
        parts.append("")

    def _provenance_chip(entry: dict) -> str:
        """Render the dual-pipeline provenance as an inline chip the
        writer can cite. Empty string when single-pipeline (no chips
        clutter narrative)."""
        confirmed = entry.get("confirmed_by") or []
        if len(confirmed) >= 2:
            return " [CONSENSUS — Gemini + Claude]"
        if confirmed == ["gemini"]:
            return " [Gemini only — verify before citing]"
        if confirmed == ["claude"]:
            return " [Claude only — verify before citing]"
        return ""

    awards = data.get("comparable_awards") or []
    if awards:
        n_consensus = sum(1 for a in awards if len(a.get("confirmed_by") or []) >= 2)
        n_needs_review = sum(1 for a in awards if a.get("needs_review"))
        summary_chip = ""
        if n_consensus or n_needs_review:
            summary_chip = f" ({n_consensus} consensus, {n_needs_review} need verification)"
        parts.append(f"Comparable processor awards ({len(awards)} found){summary_chip}:")
        for a in awards[:8]:  # cap to keep prompt size sane
            parts.append(
                f"  • {a.get('processor_name', '?')} → "
                f"{a.get('customer_name', '?')} "
                f"({a.get('award_year') or '?'}): "
                f"{a.get('disclosed_credit_card_rate_text', '')}"
                f"{_provenance_chip(a)}"
            )
            if a.get("source_url"):
                parts.append(f"    Source: {a['source_url']}")
            if a.get("notes"):
                parts.append(f"    Notes: {a['notes']}")
        if len(awards) > 8:
            parts.append(f"  ... ({len(awards) - 8} more not shown to preserve prompt size)")
        parts.append("")

    competitors = data.get("competitor_processors") or []
    if competitors:
        n_consensus = sum(1 for c in competitors if len(c.get("confirmed_by") or []) >= 2)
        n_needs_review = sum(1 for c in competitors if c.get("needs_review"))
        summary_chip = ""
        if n_consensus or n_needs_review:
            summary_chip = f" ({n_consensus} consensus, {n_needs_review} need verification)"
        parts.append(f"Likely competitor processors ({len(competitors)}){summary_chip}:")
        for c in competitors[:8]:
            parts.append(
                f"  • {c.get('name', '?')} "
                f"({c.get('market_position', '?')}) "
                f"— likelihood {c.get('likelihood_to_bid', '?')}: "
                f"{c.get('typical_pricing_summary', '')}"
                f"{_provenance_chip(c)}"
            )
        parts.append("")

    if data.get("insufficient_data_warning"):
        parts.append(
            "INSUFFICIENT DATA WARNING: the market scan found fewer "
            "than 3 comparable rate disclosures. Treat the proposed "
            "rates as informed by industry-typical ranges; cite this "
            "limitation in narrative if the buyer asks for "
            "comparable-award benchmarking."
        )
        parts.append("")

    return "\n".join(parts).rstrip()


def format_cost_build_block_for_writer(
    proposal_id: int,
    *,
    scenario: str | None = None,
) -> str:
    """Render the cost-build block for the Writer Team's cached prefix.
    Branches by service_line — both branches return empty strings
    when their upstream data isn't ready, so the writer falls back to
    NEEDS_HUMAN markers gracefully.

    payment_systems branch (delegates to _format_payment_systems_cost_block):
      Pulls fee-schedule + brand-framing + risk talking points from
      data/pricing/payment_systems.json + _payment_systems_context.
      json. When the Payment Market Researcher has run, also injects
      the consolidated pricing recommendation, comparable-award
      table, volume estimate, and profit math from
      proposals.payment_market_scan_json. Returns "" only when both
      JSON files are missing — JSON-only flow still produces a useful
      block before the agent runs. The `scenario` argument is ignored
      in this branch (it has no LOW/MEDIUM/HIGH semantics).

    it_services branch (default — original behavior):
      Renders the proposed-scenario labor-totals view: total proposed
      price, effective margin, vs-market position, ODC line items,
      lifecycle phases. Does NOT repeat per-role staffing detail —
      that lives in the APPROVED TEAM ROSTER block. `scenario`
      overrides the user's persisted choice; when omitted, reads
      `get_proposed_scenario(proposal_id)` (fallback MEDIUM). Returns
      "" when no PricingPackage rows exist yet (Cost Analyst
      hasn't run)."""
    # Service-line branch — payment_systems skips the labor flow.
    from app.services.service_line import (
        SERVICE_LINE_PAYMENT_SYSTEMS,
        get_service_line,
    )

    if get_service_line(proposal_id) == SERVICE_LINE_PAYMENT_SYSTEMS:
        return _format_payment_systems_cost_block(proposal_id)

    packages = get_pricing_packages_snapshot(proposal_id)
    if not packages:
        return ""
    if scenario is None:
        target = get_proposed_scenario(proposal_id)
    else:
        target = scenario.upper().strip() or "MEDIUM"
    pkg = next(
        (p for p in packages if (p.get("scenario") or "").upper() == target),
        None,
    )
    if pkg is None:
        # Fallback to the first persisted package — keeps the block
        # usable when the proposed scenario isn't named MEDIUM.
        pkg = packages[0]
        target = pkg.get("scenario") or "?"

    lines: list[str] = [
        "=== APPROVED COST BUILD ===",
        f"Proposed scenario: {target}",
        "Use these values DIRECTLY in your prose. Do NOT emit "
        "[NEEDS_HUMAN] for any pricing component, ODC, or phase "
        "structure already committed below.",
        "",
    ]
    total_price = pkg.get("total_proposed_price")
    if total_price is not None:
        lines.append(f"Total proposed price: ${float(total_price):,.0f}")
    indirect = pkg.get("indirect_costs_json") or {}
    profit_pct = indirect.get("effective_profit_pct") or indirect.get("profit_pct")
    if profit_pct is not None:
        try:
            lines.append(f"Effective margin: {float(profit_pct):.1%}")
        except (TypeError, ValueError):
            pass
    vs_market = pkg.get("vs_market_position")
    if vs_market:
        lines.append(f"Vs market: {vs_market}")
    rec = pkg.get("bid_recommendation")
    if rec:
        lines.append(f"Bid recommendation: {rec}")

    odcs = pkg.get("odcs_json") or []
    if odcs:
        lines.append("")
        lines.append("ODCs (Other Direct Costs — committed; reference in cost-narrative prose):")
        for o in odcs:
            item = (o.get("item") or "?").strip()
            try:
                amt = float(o.get("amount_usd") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            year_count = int(o.get("year_count") or 1)
            if year_count > 1:
                total_odc = amt * year_count
                amt_str = f"${amt:,.0f}/yr × {year_count} yrs = ${total_odc:,.0f}"
            else:
                amt_str = f"${amt:,.0f}"
            justification = (o.get("justification") or "").strip()
            if justification:
                short_just = justification if len(justification) <= 140 else justification[:137] + "..."
                lines.append(f"  - {item}: {amt_str} — {short_just}")
            else:
                lines.append(f"  - {item}: {amt_str}")

    phases = pkg.get("phase_breakdown_json") or []
    if phases:
        lines.append("")
        lines.append(
            "Lifecycle phases (use these names, durations, and month ranges in project-narrative prose):"
        )
        for ph in phases:
            name = (ph.get("name") or "?").strip()
            try:
                start = int(ph.get("start_month") or 0)
            except (TypeError, ValueError):
                start = 0
            try:
                duration = float(ph.get("duration_months") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            month_range = f"M{start}-M{start + int(round(duration))}" if start and duration else ""
            duration_str = f"{duration:g} months" if duration else "?"
            header = f"  - {name}"
            if month_range:
                header += f" ({month_range}, {duration_str})"
            elif duration_str != "?":
                header += f" ({duration_str})"
            lines.append(header)
            desc = (ph.get("description") or "").strip()
            if desc:
                short_desc = desc if len(desc) <= 200 else desc[:197] + "..."
                lines.append(f"      {short_desc}")

    return "\n".join(lines).rstrip() + "\n"


def get_pricing_packages_snapshot(proposal_id: int) -> list[dict[str, Any]]:
    """Read-only snapshot of the proposal's pricing packages + lines.
    Returns plain dicts so callers can use the data after the session
    closes (no DetachedInstanceError)."""
    from sqlalchemy.orm import selectinload

    with session_scope() as db:
        pkgs = (
            db.execute(
                select(PricingPackage)
                .where(PricingPackage.proposal_id == proposal_id)
                .options(selectinload(PricingPackage.lines))
                .order_by(PricingPackage.scenario)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": p.id,
                "scenario": p.scenario,
                "market_scan_id": p.market_scan_id,
                "agent_run_id": p.agent_run_id,
                "loaded_labor_cost": (
                    float(p.loaded_labor_cost) if p.loaded_labor_cost is not None else None
                ),
                "odcs_json": list(p.odcs_json or []),
                "subcontractor_costs": (
                    float(p.subcontractor_costs) if p.subcontractor_costs is not None else None
                ),
                "indirect_costs_json": dict(p.indirect_costs_json or {}),
                "total_proposed_price": (
                    float(p.total_proposed_price) if p.total_proposed_price is not None else None
                ),
                "pnl_projection_json": dict(p.pnl_projection_json or {}),
                "vs_market_position": p.vs_market_position,
                "bid_recommendation": p.bid_recommendation,
                "recommendation_rationale": p.recommendation_rationale,
                "phase_breakdown_json": list(p.phase_breakdown_json or []),
                "lines": [
                    {
                        "id": ln.id,
                        "labor_category": ln.labor_category,
                        "wage_band": ln.wage_band,
                        "coverage_level": ln.coverage_level,
                        "hours": float(ln.hours),
                        "loaded_hourly_rate_usd": float(ln.loaded_hourly_rate_usd),
                        "loaded_cost_usd": float(ln.loaded_cost_usd),
                        "ga_allocation_usd": float(ln.ga_allocation_usd),
                        "proposed_billing_rate_usd": float(ln.proposed_billing_rate_usd),
                        "billed_total_usd": float(ln.billed_total_usd),
                        "profit_per_hour_usd": float(ln.profit_per_hour_usd),
                        "loaded_hourly_override_usd": (
                            float(ln.loaded_hourly_override_usd)
                            if ln.loaded_hourly_override_usd is not None
                            else None
                        ),
                        "billed_hourly_override_usd": (
                            float(ln.billed_hourly_override_usd)
                            if ln.billed_hourly_override_usd is not None
                            else None
                        ),
                        "rationale": ln.rationale,
                    }
                    for ln in p.lines
                ],
            }
            for p in pkgs
        ]


__all__ = [
    "CostAnalystLaborLine",
    "CostAnalystOdc",
    "CostAnalystOutput",
    "CostAnalystPhase",
    "CostAnalystPhaseAllocation",
    "ComputedLine",
    "ComputedScenarioPackage",
    "apply_cost_build_edits_to_output",
    "compute_custom_scenario_package",
    "compute_scenario_packages",
    "get_pricing_packages_snapshot",
    "get_pricing_rules",
    "reconstruct_cost_analyst_output",
    "upsert_pricing_packages",
]
