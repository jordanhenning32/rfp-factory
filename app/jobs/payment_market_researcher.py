"""Payment-Systems Market Researcher orchestrator.

Runs the dual-pipeline payment-processing market scan when
service_line=payment_systems. Mirrors the labor-flow market
researcher's fan-out pattern (Gemini grounded + Claude+web_search in
parallel, then a pure-Python consolidator unions the results with
provenance attribution).

Pipeline:
  1. Build inputs (title + agency + scope summary + fallback ranges).
  2. Fan two providers in a ThreadPoolExecutor:
     - app.agents.payment_market_researcher.research_payment_market
       (Gemini 2.5 Pro grounded + Haiku structuring)
     - app.agents.payment_market_researcher_claude.research_payment_
       market_claude (Sonnet 4.6 + web_search + Haiku structuring)
     Per-provider failure degrades to single-provider via
     `_empty_scan_result()` so a transient outage on one side doesn't
     lose the whole scan.
  3. Consolidate via app.agents.payment_market_consolidator —
     deduplicates by (processor, customer) for awards and by
     canonicalized firm name for competitors; averages medians and
     volume estimates; OR's the insufficient-data warnings; tags
     each row with confirmed_by + needs_review provenance.
  4. Compute profit math from the consolidated pricing
     recommendation × volume estimate − our_cost_basis (loaded from
     data/pricing/payment_systems.json). The cost-basis caveat
     attaches to narrative when `_confirmed_by_ops_finance` is false.
  5. Persist the assembled JSON blob to
     proposals.payment_market_scan_json.

Failure modes surface via _set_stage and the Run Progress page.
Orchestrator catches all exceptions so a transient API failure
doesn't leave the proposal in a half-state.

Cost: ~$0.24 per scan (Gemini grounded ~$0.04 + Haiku structuring
~$0.001 × 2 + Claude+web_search ~$0.20). Wall-clock ~max(60s, 60s)
with parallel fan-out.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from app.agents.payment_market_consolidator import (
    consolidate_payment_market_research,
)
from app.agents.payment_market_researcher import (
    PaymentMarketResearchInputs,
    PaymentMarketScanResult,
    PaymentPricingStructure,
    ProfitMath,
    VolumeEstimate,
    research_payment_market,
)
from app.agents.payment_market_researcher_claude import (
    research_payment_market_claude,
)
from app.db.session import session_scope
from app.models import Proposal
from app.services.proposal_access import (
    acquire_proposal_write_fence,
    ensure_proposal_mutable,
    proposal_write_lock,
    require_proposal_mutable,
)
from app.services.review_freshness import (
    current_payment_cost_basis_provenance,
    payment_cost_basis_provenance,
    stamp_payment_market_scan_provenance,
)
from app.services.service_line import (
    SERVICE_LINE_PAYMENT_SYSTEMS,
    get_service_line,
    load_payment_systems_pricing,
    payment_cost_basis_lock,
)
from app.services.stages import record_stage as _set_stage

log = logging.getLogger(__name__)


# Default cost-basis numbers when payment_systems.json doesn't have
# our_cost_basis filled in. Industry-typical small-processor numbers,
# documented as approximate so the writer surfaces the assumption.
_DEFAULT_COST_BASIS = {
    "sponsor_acquirer_fee_bps": 8,
    "gateway_per_txn_usd": 0.03,
    "annualized_pci_compliance_usd": 15000.0,
    "annualized_support_allocation_usd": 20000.0,
}


def _empty_scan_result() -> PaymentMarketScanResult:
    """Return-value placeholder for a provider that errored out, so
    the consolidator can still union the surviving provider's data
    instead of dropping the whole scan."""
    return PaymentMarketScanResult(
        pricing_structure=PaymentPricingStructure(
            pricing_model="",
            pricing_model_rationale="",
            median_market_credit_card_markup_bps=None,
            proposed_credit_card_markup_bps=None,
            median_market_per_txn_fee_usd=None,
            proposed_per_txn_fee_usd=None,
            median_market_ach_fee_usd=None,
            proposed_ach_fee_usd=None,
            median_market_monthly_fee_usd=None,
            proposed_monthly_fee_usd=None,
            other_fees_recommended=[],
            rate_positioning="match",
        ),
        comparable_awards=[],
        competitor_processors=[],
        volume_estimate=VolumeEstimate(
            annual_processed_volume_low_usd=None,
            annual_processed_volume_midpoint_usd=None,
            annual_processed_volume_high_usd=None,
            estimated_transaction_count_annual=None,
            average_transaction_size_usd=None,
            estimation_basis="",
            confidence="low",
        ),
        profit_math=ProfitMath(),
        insufficient_data_warning=False,
        citations=[],
    )


def spawn_payment_market_research(
    proposal_id: int,
    *,
    model_focus: str | None = None,
) -> None:
    """Fire the payment-systems market researcher in a daemon thread.
    Mirrors the spawn_market_research / spawn_intake pattern — RQ is
    pyproject-listed but not wired up, so daemon threads it is.

    `model_focus` overrides the agent's pricing-model recommendation
    when set. Used by the 'Re-run scan with <model> focus' UI button
    after the user has changed their selected_pricing_model."""
    t = threading.Thread(
        target=run_payment_market_research,
        args=(proposal_id,),
        kwargs={"model_focus": model_focus},
        name=f"payment-market-research-{proposal_id}",
        daemon=True,
    )
    t.start()


def run_payment_market_research(
    proposal_id: int,
    *,
    model_focus: str | None = None,
) -> None:
    """Sync entry point. Builds inputs, runs the agent, computes
    profit math, persists. All exceptions surface via stage banner."""
    require_proposal_mutable(
        proposal_id, operation="run payment market research",
    )
    log.info(
        "payment_market_researcher starting for proposal %d",
        proposal_id,
    )
    try:
        if get_service_line(proposal_id) != SERVICE_LINE_PAYMENT_SYSTEMS:
            _set_stage(
                proposal_id,
                "Payment Market Researcher: proposal is not service_line=payment_systems; skipping.",
            )
            return

        _set_stage(
            proposal_id,
            "Payment Market Researcher: building research inputs…",
        )
        inputs = _snapshot_inputs(proposal_id)
        if inputs is None:
            _set_stage(
                proposal_id,
                f"Payment Market Researcher: proposal {proposal_id} not found.",
                status="failed",
            )
            return

        # Apply model_focus override (set by the 'Re-run scan with
        # <model> focus' UI button). When empty, the agent picks its
        # own model from the four supported options.
        if model_focus:
            inputs.model_focus = model_focus

        focus_suffix = f" (model focus: {model_focus})" if model_focus else ""
        _set_stage(
            proposal_id,
            f"Payment Market Researcher (Gemini grounded + Claude+web "
            f"dual-pipeline){focus_suffix}: researching comparable "
            f"processor awards, rate band, and processed-volume "
            f"estimate…",
        )

        # Fan both providers concurrently. Wall-clock per scan is
        # max(A, B) ~= 60-90s. Per-provider failure degrades
        # gracefully — the consolidator unions whatever results we
        # got, so a transient outage on one side doesn't lose the scan.
        empty = _empty_scan_result()
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"payment-market-research-{proposal_id}",
        ) as inner:
            fut_a = inner.submit(
                research_payment_market,
                proposal_id=proposal_id,
                inputs=inputs,
            )
            fut_b = inner.submit(
                research_payment_market_claude,
                proposal_id=proposal_id,
                inputs=inputs,
            )
            try:
                pass_a = fut_a.result()
            except Exception:
                log.exception(
                    "payment_market_researcher_a (gemini): proposal %d "
                    "failed; consolidating with B-only results.",
                    proposal_id,
                )
                pass_a = empty
            try:
                pass_b = fut_b.result()
            except Exception:
                log.exception(
                    "payment_market_researcher_b (claude): proposal %d "
                    "failed; consolidating with A-only results.",
                    proposal_id,
                )
                pass_b = empty

        # If both providers came back empty, surface that explicitly —
        # the consolidator would otherwise quietly return an empty
        # result and the user would see "no data" without context.
        if (
            not pass_a.comparable_awards
            and not pass_a.competitor_processors
            and not pass_b.comparable_awards
            and not pass_b.competitor_processors
        ):
            raise RuntimeError(
                "payment_market_researcher: both providers returned "
                "empty results. Check agent_runs for details."
            )

        result = consolidate_payment_market_research(
            proposal_id=proposal_id,
            pass_a=pass_a,
            pass_b=pass_b,
        )

        # Compute and stamp from the SAME immutable cost-basis snapshot.
        # If an operator changes the shared file before commit, retry the
        # cheap deterministic math once; never label old math with a new hash.
        persisted = False
        for _attempt in range(2):
            with payment_cost_basis_lock():
                pricing_snapshot = deepcopy(load_payment_systems_pricing())
            used_provenance = payment_cost_basis_provenance(pricing_snapshot)
            result.profit_math = _compute_profit_math(
                result,
                pricing_data=pricing_snapshot,
            )
            if _persist_result(
                proposal_id,
                result,
                cost_basis_provenance=used_provenance,
            ):
                persisted = True
                break
        if not persisted:
            raise RuntimeError(
                "Payment cost basis changed repeatedly while profit math "
                "was being committed; rerun Payment Market Research."
            )

        _set_stage(
            proposal_id,
            f"Payment Market Researcher: scan complete — "
            f"{len(result.comparable_awards)} comparable award(s), "
            f"{len(result.competitor_processors)} likely "
            f"competitor(s), volume estimate "
            f"${(result.volume_estimate.annual_processed_volume_midpoint_usd or 0):,.0f} "
            f"midpoint.",
        )

    except Exception as exc:
        log.exception(
            "payment_market_researcher failed for proposal %d",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            f"Payment Market Researcher: failed — {exc}",
            status="failed",
        )


# ---- Inputs snapshot -----------------------------------------------------


def _snapshot_inputs(proposal_id: int) -> PaymentMarketResearchInputs | None:
    """Build the inputs from the proposal row + RFP package text +
    payment_systems.json fallback ranges."""
    pricing = load_payment_systems_pricing()
    fallback_ranges = (pricing.get("volume_adjusted_offering") or {}).get(
        "typical_county_tier_ranges_for_fallback"
    ) or {}

    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return None
        rfp_title = p.title or ""
        rfp_agency = p.agency or ""
        scope_summary = _build_scope_summary(p)

    return PaymentMarketResearchInputs(
        rfp_title=rfp_title,
        rfp_agency=rfp_agency,
        scope_summary=scope_summary,
        quadratic_summary=_build_quadratic_summary(),
        fallback_rate_ranges=fallback_ranges,
    )


def _build_scope_summary(proposal: Proposal) -> str:
    """First few thousand chars of the primary RFP doc's extracted
    text, concatenated with any compliance items mentioning fees/
    pricing/hardware. Keeps the prompt focused without paying for
    the full RFP body."""
    parts: list[str] = []
    if proposal.rfp_package and proposal.rfp_package.documents:
        for doc in proposal.rfp_package.documents:
            text = (doc.extracted_text_md or "").strip()
            if text:
                parts.append(text[:3000])
                break  # primary doc only for now
    fee_keywords = (
        "fee",
        "pricing",
        "rate",
        "interchange",
        "monthly",
        "hardware",
        "terminal",
        "POS",
        "PCI",
    )
    if proposal.compliance_items:
        relevant = [
            ci.requirement_text
            for ci in proposal.compliance_items
            if any(k.lower() in (ci.requirement_text or "").lower() for k in fee_keywords)
        ][:25]
        if relevant:
            parts.append("\n\n=== Fee / pricing / hardware compliance items ===")
            for ci in relevant:
                parts.append(f"  - {ci}")
    return "\n".join(parts)[:8000]


def _build_quadratic_summary() -> str:
    """Short Quadratic Financial / NAC pitch for fit awareness in the
    grounded research prompt."""
    return (
        "Quadratic Financial (formerly National Acceptance Company / "
        "NAC EFT, founded 1985) — 40-year payment-processing operator "
        "embedded in Quadratic Digital LLC, a federal-grade government "
        "IT integrator (FedRAMP, DoD SRG, GSA OLM). PA-based, small "
        "business. Currently PCI DSS Level 3 with documented roadmap "
        "to Level 2 within 12 months. NACHA member; end-to-end "
        "encryption; U.S.-only data residency. Hardware via subcontract "
        "partner (Clover / Verifone / PAX / Ingenico) — no in-house "
        "terminals."
    )


# ---- Profit math --------------------------------------------------------


def compute_profit_math(
    result,
    *,
    pricing_data: dict | None = None,
) -> ProfitMath:
    """Public alias of _compute_profit_math so callers outside the
    orchestrator (e.g. service helpers re-running profit math after
    a cost-basis edit) can reuse the math without spawning a new
    market-research run."""
    return _compute_profit_math(result, pricing_data=pricing_data)


def _compute_profit_math(
    result,
    *,
    pricing_data: dict | None = None,
) -> ProfitMath:
    """Compute revenue (rate × volume) − internal costs from the
    agent's pricing recommendation + volume estimate + our cost basis.
    All branches degrade gracefully — missing inputs produce None
    fields rather than zeros, so the writer can disclose what's
    missing instead of asserting fake numbers."""
    if pricing_data is None:
        pricing_data = load_payment_systems_pricing()
    cost_basis = (pricing_data.get("our_cost_basis") or {}).copy()
    # Layer defaults under any user-provided values.
    for k, default in _DEFAULT_COST_BASIS.items():
        cost_basis.setdefault(k, default)

    ps = result.pricing_structure
    ve = result.volume_estimate
    assumptions: list[str] = []

    markup_bps = ps.proposed_credit_card_markup_bps
    per_txn_fee = ps.proposed_per_txn_fee_usd or 0.0
    monthly_fee = ps.proposed_monthly_fee_usd or 0.0
    txn_count = ve.estimated_transaction_count_annual
    vol_low = ve.annual_processed_volume_low_usd
    vol_mid = ve.annual_processed_volume_midpoint_usd
    vol_high = ve.annual_processed_volume_high_usd

    if markup_bps is None:
        assumptions.append(
            "Proposed credit-card markup bps was null in the agent result; revenue projection skipped."
        )
        return ProfitMath(
            cost_basis_assumptions=assumptions,
            computation_notes="Insufficient agent data to project revenue.",
        )

    def _revenue(volume: float | None) -> float | None:
        if volume is None:
            return None
        markup = volume * markup_bps / 10000.0
        # Per-txn fee × estimated count (conservatively scale by volume
        # ratio when txn_count was estimated at the midpoint volume).
        if txn_count and vol_mid:
            count_for_volume = txn_count * (volume / vol_mid)
        elif txn_count:
            count_for_volume = txn_count
        else:
            count_for_volume = 0
        return markup + (count_for_volume * per_txn_fee) + (12 * monthly_fee)

    rev_low = _revenue(vol_low)
    rev_mid = _revenue(vol_mid)
    rev_high = _revenue(vol_high)

    sponsor_bps = float(cost_basis.get("sponsor_acquirer_fee_bps") or 0)
    gateway_per_txn = float(cost_basis.get("gateway_per_txn_usd") or 0)
    annualized_pci = float(cost_basis.get("annualized_pci_compliance_usd") or 0)
    annualized_support = float(cost_basis.get("annualized_support_allocation_usd") or 0)

    # Caveat fires when ops finance HASN'T explicitly confirmed the
    # values. The user (or the Edit Cost Basis dialog) flips
    # `_confirmed_by_ops_finance` to true once the four fields
    # reflect actual NAC numbers — at that point the writer stops
    # disclaiming the cost basis as approximate.
    confirmed = bool((pricing_data.get("our_cost_basis") or {}).get("_confirmed_by_ops_finance"))
    if not confirmed:
        assumptions.append(
            "Internal cost basis uses industry-typical defaults "
            "(sponsor 8 bps, gateway $0.03/txn, PCI $15K/yr, support "
            "$20K/yr per county). Refine via the Cost tab's Edit "
            "Cost Basis dialog, or by editing data/pricing/"
            "payment_systems.json `our_cost_basis` directly, then "
            "flip `_confirmed_by_ops_finance` to true to suppress "
            "this caveat."
        )

    def _cost(volume: float | None) -> float | None:
        if volume is None:
            return None
        sponsor_cost = volume * sponsor_bps / 10000.0
        if txn_count and vol_mid:
            count_for_volume = txn_count * (volume / vol_mid)
        elif txn_count:
            count_for_volume = txn_count
        else:
            count_for_volume = 0
        gateway_cost = count_for_volume * gateway_per_txn
        return sponsor_cost + gateway_cost + annualized_pci + annualized_support

    cost_at_mid = _cost(vol_mid)
    profit_low = (rev_low - _cost(vol_low)) if (rev_low is not None and _cost(vol_low) is not None) else None
    profit_mid = (rev_mid - cost_at_mid) if (rev_mid is not None and cost_at_mid is not None) else None
    profit_high = (
        (rev_high - _cost(vol_high)) if (rev_high is not None and _cost(vol_high) is not None) else None
    )

    margin_pct: float | None = None
    if rev_mid and rev_mid > 0 and profit_mid is not None:
        margin_pct = profit_mid / rev_mid

    return ProfitMath(
        annual_processor_revenue_low_usd=rev_low,
        annual_processor_revenue_midpoint_usd=rev_mid,
        annual_processor_revenue_high_usd=rev_high,
        annual_internal_costs_usd=cost_at_mid,
        annual_net_profit_low_usd=profit_low,
        annual_net_profit_midpoint_usd=profit_mid,
        annual_net_profit_high_usd=profit_high,
        profit_margin_pct_at_midpoint=margin_pct,
        cost_basis_assumptions=assumptions,
        computation_notes=(
            f"Revenue = (volume × {markup_bps} bps) + "
            f"(txn_count × ${per_txn_fee:.2f}) + (12 × ${monthly_fee:.0f}). "
            f"Internal cost = (volume × {int(sponsor_bps)} bps) + "
            f"(txn_count × ${gateway_per_txn:.2f}) + ${int(annualized_pci):,}/yr PCI + "
            f"${int(annualized_support):,}/yr support."
        ),
    )


# ---- Persistence --------------------------------------------------------


def _persist_result(
    proposal_id: int,
    result,
    *,
    cost_basis_provenance: dict[str, str] | None = None,
) -> bool:
    """Serialize the result and write to proposals.payment_market_
    scan_json. Replaces any prior scan in full — re-running the
    researcher overwrites the prior result rather than merging."""
    with proposal_write_lock(proposal_id):
        with payment_cost_basis_lock():
            current_provenance = current_payment_cost_basis_provenance()
            used_provenance = (
                cost_basis_provenance or current_provenance
            )
            if used_provenance != current_provenance:
                return False
            payload = json.dumps(
                stamp_payment_market_scan_provenance(
                    result.to_json_dict(),
                    provenance=used_provenance,
                ),
                default=str,
                indent=2,
            )
            with session_scope() as db:
                acquire_proposal_write_fence(db, proposal_id)
                p = ensure_proposal_mutable(
                    db,
                    proposal_id,
                    operation="persist payment market research",
                )
                if p is None:
                    return False
                p.payment_market_scan_json = payload
    return True


__all__ = [
    "spawn_payment_market_research",
    "run_payment_market_research",
]
