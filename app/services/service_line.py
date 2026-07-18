"""Service-line registry — selects cost-flow + data-file behavior per proposal.

A service line is the broad category of work the proposal addresses.
The default is `it_services` (custom software, cloud, modernization,
IV&V) which uses the labor-catalog cost flow (Cost Analyst H/M/L
scenarios → Cost Reviewer dual pass → Cost Writer narrating labor
totals). Adding a new category like `payment_systems` (card processing,
ACH/EFT, recurring billing, donation processing, hospital financing)
swaps the cost flow to a fee-schedule narrative drawn from
data/pricing/<service_line>.json + _<service_line>_context.json,
and skips the Cost Analyst / Cost Reviewer entirely (no labor build
to make).

Adding a NEW service line in the future is a registry change — add a
key to SERVICE_LINES below + drop the matching JSON files in
data/pricing/. No model migration, no UI code change.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.db.session import session_scope
from app.models import Proposal

log = logging.getLogger(__name__)


# Canonical service-line ID constants. Use these everywhere instead
# of typing the raw strings — a typo on a raw `"payment_systems"` /
# `"it_services"` would silently route to the wrong flow with no
# error. The strings here ARE the persisted column values; the
# SERVICE_LINES dict below uses them as keys.
SERVICE_LINE_IT_SERVICES = "it_services"
SERVICE_LINE_PAYMENT_SYSTEMS = "payment_systems"

DEFAULT_SERVICE_LINE = SERVICE_LINE_IT_SERVICES


# Canonical pricing-model IDs for the payment_systems service line.
# Used as the persisted value in proposals.selected_pricing_model and
# matched against the agent's recommended pricing_model field. Adding
# a new model = add to this tuple AND update the agent prompt's model-
# selection heuristics in app/agents/payment_market_researcher.py.
PAYMENT_PRICING_MODEL_INTERCHANGE_PLUS = "interchange_plus"
PAYMENT_PRICING_MODEL_FLAT_RATE = "flat_rate"
PAYMENT_PRICING_MODEL_TIERED = "tiered"
PAYMENT_PRICING_MODEL_PERCENTAGE_OF_COLLECTED = "percentage_of_collected"

# Ordered list — drives the UI selector chip order.
PAYMENT_PRICING_MODELS: tuple[dict[str, str], ...] = (
    {
        "id": PAYMENT_PRICING_MODEL_INTERCHANGE_PLUS,
        "label": "Interchange-plus",
        "description": (
            "Cost = (interchange) + (markup bps) + (per-transaction "
            "fee). Most transparent; most common in county / state "
            "procurements."
        ),
    },
    {
        "id": PAYMENT_PRICING_MODEL_FLAT_RATE,
        "label": "Flat rate",
        "description": (
            "Single % + fixed $ per transaction (Stripe / Square "
            "style). Predictable for the merchant; processor "
            "absorbs interchange variance."
        ),
    },
    {
        "id": PAYMENT_PRICING_MODEL_TIERED,
        "label": "Tiered",
        "description": (
            "Qualified / mid-qualified / non-qualified rates. Older model; rare in modern county RFPs."
        ),
    },
    {
        "id": PAYMENT_PRICING_MODEL_PERCENTAGE_OF_COLLECTED,
        "label": "Percentage of collected",
        "description": (
            "% of total recoveries. NAC's standard receivables / "
            "collections model — fits recurring billing portfolios, "
            "not POS."
        ),
    },
)


# Registry of known service lines. Keys are the persisted column
# values; values describe how the system treats that service line.
# Adding a new service line = add a key here + create the JSON files
# at the documented paths. Everything downstream reads from this dict.
SERVICE_LINES: dict[str, dict[str, Any]] = {
    SERVICE_LINE_IT_SERVICES: {
        "label": "IT Services / Custom Application Development",
        "description": (
            "Default flow for custom software, cloud DevSecOps, "
            "modernization, IV&V, PMO services, healthcare/MMIS "
            "systems, etc. Uses labor-catalog cost build (Cost "
            "Analyst → Cost Reviewer → Cost Writer with H/M/L "
            "scenarios)."
        ),
        "uses_labor_catalog": True,
        "uses_payment_fee_schedule": False,
        "shows_cost_analyst": True,
        "shows_cost_reviewer": True,
        "pricing_data_path": None,
        "context_data_path": None,
    },
    SERVICE_LINE_PAYMENT_SYSTEMS: {
        "label": "Payment Systems (card / ACH / billing / donations)",
        "description": (
            "Payment-processing RFPs — card processing, ACH/EFT, "
            "recurring billing, donation processing, subscription "
            "billing, hospital financing, and similar. Uses "
            "fee-schedule cost build (skips Cost Analyst; Cost "
            "Writer renders directly from data/pricing/"
            "payment_systems.json + _payment_systems_context.json)."
        ),
        "uses_labor_catalog": False,
        "uses_payment_fee_schedule": True,
        "shows_cost_analyst": False,
        "shows_cost_reviewer": False,
        "pricing_data_path": "data/pricing/payment_systems.json",
        "context_data_path": "data/pricing/_payment_systems_context.json",
    },
}


def list_service_lines() -> list[dict[str, Any]]:
    """List of {id, label, description} for the New Proposal form
    dropdown. Order is stable (insertion order on the SERVICE_LINES
    dict)."""
    return [{"id": k, "label": v["label"], "description": v["description"]} for k, v in SERVICE_LINES.items()]


def is_valid_service_line(value: str | None) -> bool:
    return value in SERVICE_LINES


def get_service_line(proposal_id: int) -> str:
    """Read the persisted service line for a proposal. Returns
    DEFAULT_SERVICE_LINE ('it_services') for legacy proposals that
    have NULL in the column or any unknown value."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return DEFAULT_SERVICE_LINE
        v = (p.service_line or "").strip()
        if v in SERVICE_LINES:
            return v
        return DEFAULT_SERVICE_LINE


def set_service_line(proposal_id: int, value: str) -> str:
    """Persist the service-line tag for a proposal. Validates against
    SERVICE_LINES — unknown values raise ValueError. Returns the value
    that was stored."""
    target = (value or "").strip()
    if target not in SERVICE_LINES:
        raise ValueError(f"unknown service_line {value!r}; expected one of {list(SERVICE_LINES.keys())}")
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            raise ValueError(f"proposal {proposal_id} not found")
        p.service_line = target
    return target


def get_service_line_config(proposal_id: int) -> dict[str, Any]:
    """Full registry entry for a proposal's service line. Always
    returns a valid dict (falls back to it_services for unknown /
    legacy values)."""
    line_id = get_service_line(proposal_id)
    return SERVICE_LINES[line_id]


# ----- JSON data loaders --------------------------------------------------
#
# Cached at the module level so the JSON parse cost is paid once per
# process. Edits to data/pricing/*.json require a process restart OR
# explicit cache reset (the Final Polish workflow / settings tab can
# wire a "reload" button later if needed).


@lru_cache(maxsize=1)
def load_payment_systems_pricing() -> dict[str, Any]:
    return _load_json_at(SERVICE_LINES[SERVICE_LINE_PAYMENT_SYSTEMS]["pricing_data_path"])


@lru_cache(maxsize=1)
def load_payment_systems_context() -> dict[str, Any]:
    return _load_json_at(SERVICE_LINES[SERVICE_LINE_PAYMENT_SYSTEMS]["context_data_path"])


def _load_json_at(rel_path: str) -> dict[str, Any]:
    """Load a JSON file relative to the project root. Project root is
    the parent of the `app/` package — derived from this file's path
    so it works regardless of cwd when the app is launched."""
    project_root = Path(__file__).resolve().parents[2]
    full = project_root / rel_path
    try:
        return json.loads(full.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("service_line data file missing: %s", full)
        return {}
    except json.JSONDecodeError:
        log.exception("service_line data file invalid JSON: %s", full)
        return {}


def list_payment_pricing_models() -> list[dict[str, str]]:
    """Public copy of PAYMENT_PRICING_MODELS for the UI selector. Each
    entry is {id, label, description}; order is stable."""
    return [dict(m) for m in PAYMENT_PRICING_MODELS]


def is_valid_payment_pricing_model(value: str | None) -> bool:
    if value is None:
        return False
    return any(m["id"] == value for m in PAYMENT_PRICING_MODELS)


def get_agent_recommended_pricing_model(proposal_id: int) -> str | None:
    """Read the pricing_model the Payment Market Researcher recommended
    on the most recent scan. Returns None when the scan hasn't run
    yet, the JSON failed to parse, or the agent left the field blank.
    The UI uses this to render the ★ badge on the matching selector
    chip and to detect override / mismatch state."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return None
        raw = p.payment_market_scan_json
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    model = ((data.get("pricing_structure") or {}).get("pricing_model") or "").strip()
    return model or None


def get_selected_pricing_model(proposal_id: int) -> str | None:
    """Read the user's persisted pricing-model override for this
    proposal. Returns None when no override is set — caller should
    fall back to `get_agent_recommended_pricing_model` and ultimately
    to a sensible default."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return None
        v = (p.selected_pricing_model or "").strip()
    return v if is_valid_payment_pricing_model(v) else None


def get_effective_pricing_model(proposal_id: int) -> str | None:
    """The model the Cost Volume Writer / cost-block formatter should
    treat as canonical for narrative purposes: user override wins;
    falls back to the agent's recommended model from the persisted
    scan; None when neither is set yet."""
    return get_selected_pricing_model(proposal_id) or get_agent_recommended_pricing_model(proposal_id)


def set_selected_pricing_model(
    proposal_id: int,
    value: str | None,
) -> str | None:
    """Persist the user's pricing-model override. Pass None or "" to
    clear the override (system reverts to the agent's recommendation).
    Validates against PAYMENT_PRICING_MODELS — unknown ids raise."""
    cleared = value is None or not str(value).strip()
    if cleared:
        target: str | None = None
    else:
        target = str(value).strip()
        if not is_valid_payment_pricing_model(target):
            raise ValueError(
                f"unknown pricing model {value!r}; expected one of "
                f"{[m['id'] for m in PAYMENT_PRICING_MODELS]}"
            )
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            raise ValueError(f"proposal {proposal_id} not found")
        p.selected_pricing_model = target
    return target


def reload_payment_systems_data() -> None:
    """Invalidate the cached JSON loaders — call after edits to the
    pricing JSON files. Mirrors the pattern of
    core.company_profile.reload_company_profile."""
    load_payment_systems_pricing.cache_clear()
    load_payment_systems_context.cache_clear()


# ---- Cost-basis edit helpers (Cost tab Edit dialog uses these) ----------


def get_payment_cost_basis() -> dict[str, Any]:
    """Read the current `our_cost_basis` section. Returns a copy so
    callers can mutate freely without affecting the lru_cached value."""
    pricing = load_payment_systems_pricing()
    section = (pricing.get("our_cost_basis") or {}).copy()
    return section


def update_payment_cost_basis(
    *,
    sponsor_acquirer_fee_bps: int | float | None = None,
    gateway_per_txn_usd: float | None = None,
    annualized_pci_compliance_usd: float | None = None,
    annualized_support_allocation_usd: float | None = None,
    confirmed_by_ops_finance: bool | None = None,
) -> dict[str, Any]:
    """Persist edits to `our_cost_basis` in payment_systems.json.

    Only fields the caller passes are touched — the four numeric
    fields and the confirmation flag are independent. Comment-style
    `_*_note` keys and the `_purpose` / `_default_disclosure` keys
    are preserved verbatim. Clears the lru_cache on success so the
    next read sees the new values.

    Returns the updated `our_cost_basis` section."""
    project_root = Path(__file__).resolve().parents[2]
    rel_path = SERVICE_LINES[SERVICE_LINE_PAYMENT_SYSTEMS]["pricing_data_path"]
    full = project_root / rel_path

    raw = full.read_text(encoding="utf-8")
    data = json.loads(raw)
    section = data.setdefault("our_cost_basis", {})

    if sponsor_acquirer_fee_bps is not None:
        section["sponsor_acquirer_fee_bps"] = float(sponsor_acquirer_fee_bps)
    if gateway_per_txn_usd is not None:
        section["gateway_per_txn_usd"] = float(gateway_per_txn_usd)
    if annualized_pci_compliance_usd is not None:
        section["annualized_pci_compliance_usd"] = float(annualized_pci_compliance_usd)
    if annualized_support_allocation_usd is not None:
        section["annualized_support_allocation_usd"] = float(annualized_support_allocation_usd)
    if confirmed_by_ops_finance is not None:
        section["_confirmed_by_ops_finance"] = bool(confirmed_by_ops_finance)

    # Pretty-print at indent=2 to match the existing file shape;
    # ensure_ascii=False keeps unicode characters readable in source
    # control diffs.
    full.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    reload_payment_systems_data()
    return section


def recompute_payment_profit_math(proposal_id: int) -> bool:
    """Re-compute and persist profit math for an existing payment-
    market scan, using the CURRENT `our_cost_basis` from JSON.
    Useful after the user edits cost-basis values via the dialog —
    cheap re-run without spawning a new (expensive) Gemini+Claude
    grounded search.

    Returns True if profit math was recomputed and persisted, False
    if the proposal has no payment scan yet (or the JSON is malformed).
    """
    # Lazy imports to avoid a circular dependency at module load
    # time — orchestrator imports service_line; service_line referencing
    # the orchestrator's compute_profit_math would close the cycle.
    # Proposal is already imported at module top, so it's not lazy here.
    from app.agents.payment_market_researcher import (
        ComparableProcessorAward,
        CompetitorProcessor,
        PaymentMarketScanResult,
        PaymentPricingStructure,
        ProfitMath,
        VolumeEstimate,
    )
    from app.jobs.payment_market_researcher import compute_profit_math

    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return False
        raw = p.payment_market_scan_json
    if not raw or not raw.strip():
        return False

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(
            "recompute_payment_profit_math: invalid JSON on proposal %d",
            proposal_id,
        )
        return False

    # Rebuild the result dataclass from the JSON. Only fields
    # compute_profit_math touches need to be accurate — everything
    # else flows through unchanged.
    ps = data.get("pricing_structure") or {}
    ve = data.get("volume_estimate") or {}
    pricing_structure = PaymentPricingStructure(
        pricing_model=ps.get("pricing_model") or "",
        pricing_model_rationale=ps.get("pricing_model_rationale") or "",
        median_market_credit_card_markup_bps=ps.get("median_market_credit_card_markup_bps"),
        proposed_credit_card_markup_bps=ps.get("proposed_credit_card_markup_bps"),
        median_market_per_txn_fee_usd=ps.get("median_market_per_txn_fee_usd"),
        proposed_per_txn_fee_usd=ps.get("proposed_per_txn_fee_usd"),
        median_market_ach_fee_usd=ps.get("median_market_ach_fee_usd"),
        proposed_ach_fee_usd=ps.get("proposed_ach_fee_usd"),
        median_market_monthly_fee_usd=ps.get("median_market_monthly_fee_usd"),
        proposed_monthly_fee_usd=ps.get("proposed_monthly_fee_usd"),
        other_fees_recommended=ps.get("other_fees_recommended") or [],
        rate_positioning=ps.get("rate_positioning") or "match",
    )
    volume_estimate = VolumeEstimate(
        annual_processed_volume_low_usd=ve.get("annual_processed_volume_low_usd"),
        annual_processed_volume_midpoint_usd=ve.get("annual_processed_volume_midpoint_usd"),
        annual_processed_volume_high_usd=ve.get("annual_processed_volume_high_usd"),
        estimated_transaction_count_annual=ve.get("estimated_transaction_count_annual"),
        average_transaction_size_usd=ve.get("average_transaction_size_usd"),
        estimation_basis=ve.get("estimation_basis") or "",
        confidence=ve.get("confidence") or "low",
    )
    awards = [
        ComparableProcessorAward(
            **{k: v for k, v in a.items() if k in ComparableProcessorAward.__dataclass_fields__}
        )
        for a in (data.get("comparable_awards") or [])
    ]
    competitors = [
        CompetitorProcessor(**{k: v for k, v in c.items() if k in CompetitorProcessor.__dataclass_fields__})
        for c in (data.get("competitor_processors") or [])
    ]
    rebuilt = PaymentMarketScanResult(
        pricing_structure=pricing_structure,
        comparable_awards=awards,
        competitor_processors=competitors,
        volume_estimate=volume_estimate,
        profit_math=ProfitMath(),
        insufficient_data_warning=bool(data.get("insufficient_data_warning")),
        citations=data.get("citations") or [],
    )

    # Recompute against the CURRENT JSON cost basis (lru_cache was
    # cleared by the upstream update call, so this picks up the
    # new values).
    rebuilt.profit_math = compute_profit_math(rebuilt)

    # Replace just the profit_math section in the persisted blob —
    # everything else stays as the agent originally produced it.
    data["profit_math"] = {
        "annual_processor_revenue_low_usd": rebuilt.profit_math.annual_processor_revenue_low_usd,
        "annual_processor_revenue_midpoint_usd": rebuilt.profit_math.annual_processor_revenue_midpoint_usd,
        "annual_processor_revenue_high_usd": rebuilt.profit_math.annual_processor_revenue_high_usd,
        "annual_internal_costs_usd": rebuilt.profit_math.annual_internal_costs_usd,
        "annual_net_profit_low_usd": rebuilt.profit_math.annual_net_profit_low_usd,
        "annual_net_profit_midpoint_usd": rebuilt.profit_math.annual_net_profit_midpoint_usd,
        "annual_net_profit_high_usd": rebuilt.profit_math.annual_net_profit_high_usd,
        "profit_margin_pct_at_midpoint": rebuilt.profit_math.profit_margin_pct_at_midpoint,
        "cost_basis_assumptions": rebuilt.profit_math.cost_basis_assumptions,
        "computation_notes": rebuilt.profit_math.computation_notes,
    }

    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return False
        p.payment_market_scan_json = json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    return True


__all__ = [
    "DEFAULT_SERVICE_LINE",
    "SERVICE_LINE_IT_SERVICES",
    "SERVICE_LINE_PAYMENT_SYSTEMS",
    "SERVICE_LINES",
    "PAYMENT_PRICING_MODELS",
    "PAYMENT_PRICING_MODEL_INTERCHANGE_PLUS",
    "PAYMENT_PRICING_MODEL_FLAT_RATE",
    "PAYMENT_PRICING_MODEL_TIERED",
    "PAYMENT_PRICING_MODEL_PERCENTAGE_OF_COLLECTED",
    "list_service_lines",
    "is_valid_service_line",
    "get_service_line",
    "set_service_line",
    "get_service_line_config",
    "load_payment_systems_pricing",
    "load_payment_systems_context",
    "reload_payment_systems_data",
    "get_payment_cost_basis",
    "update_payment_cost_basis",
    "recompute_payment_profit_math",
    "list_payment_pricing_models",
    "is_valid_payment_pricing_model",
    "get_agent_recommended_pricing_model",
    "get_selected_pricing_model",
    "get_effective_pricing_model",
    "set_selected_pricing_model",
]
