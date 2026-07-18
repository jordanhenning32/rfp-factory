"""Payment-Systems Market Consolidator — merges the two providers'
payment-market-scan outputs (Gemini-grounded + Claude+web_search) into
a single PaymentMarketScanResult with provenance attribution per
comparable award and competitor processor.

Pure-Python merge, no LLM call. Identity matching:
  - Comparable awards: dedupe by canonicalized (processor_name,
    customer_name) tuple. Different identity than the labor-flow's
    award_title canonicalization — for processor awards the natural
    key is "Forte serving Tarrant County" not the contract title
    (titles vary widely; "Merchant Processing Services" vs
    "Credit Card and ACH Processing Agreement" describe the same
    underlying contract when the processor + customer match).
  - Competitor processors: canonicalized firm name (reuses the
    teaming consolidator's helper). Worldpay vs FIS Worldpay → same
    firm. Reused logic = consistent name canonicalization across
    every consolidator in the system.

Per row the consolidator records:
  confirmed_by  — list[str] subset of {"gemini", "claude"}.
                  Length 2 = both providers surfaced this row;
                  length 1 = single provider.
  needs_review  — True for single-provider rows. Awards: True
                  always for single-provider (rate disclosures are
                  too varied across procurement docs to call any
                  single-source award authoritative). Competitors:
                  True for single-provider firms.

Pricing structure: Pass A's recommendation wins, but null fields
backfill from Pass B when Pass B has them. Volume estimate: averaged
across providers when both produced numbers, otherwise whichever
provider has it. Insufficient_data_warning: OR'd (either provider
flagging → consolidated flags). Citations: union by URI.

Profit math is NOT computed here — left blank ProfitMath for the
orchestrator to fill from the consolidated pricing + volume + our
cost basis.
"""

from __future__ import annotations

import logging
from statistics import mean

from app.agents.payment_market_researcher import (
    ComparableProcessorAward,
    CompetitorProcessor,
    PaymentMarketScanResult,
    PaymentPricingStructure,
    ProfitMath,
    VolumeEstimate,
)
from app.agents.teaming_consolidator import _canonicalize_name

log = logging.getLogger(__name__)


def _award_key(award: ComparableProcessorAward) -> str:
    """(processor, customer) tuple canonicalized to a single string
    key. Empty string means the award is missing both fields and
    can't be deduped — those flow through as separate rows."""
    proc = _canonicalize_name(award.processor_name) if award.processor_name else ""
    cust = _canonicalize_name(award.customer_name) if award.customer_name else ""
    if not proc and not cust:
        return ""
    return f"{proc}|{cust}"


def _avg(x: float | None, y: float | None) -> float | None:
    if x is not None and y is not None:
        return float(mean([x, y]))
    return x if x is not None else y


def _avg_int(x: int | None, y: int | None) -> int | None:
    if x is not None and y is not None:
        return int(round(mean([x, y])))
    return x if x is not None else y


def _bumped_award(
    award: ComparableProcessorAward,
    *,
    confirmed_by: list[str],
    consensus: bool,
) -> ComparableProcessorAward:
    """Attach provenance. Single-provider awards get needs_review=True
    — processor rate disclosures vary too much across procurement
    docs to call a single source authoritative."""
    return ComparableProcessorAward(
        processor_name=award.processor_name,
        customer_name=award.customer_name,
        award_year=award.award_year,
        pricing_model=award.pricing_model,
        disclosed_credit_card_rate_text=award.disclosed_credit_card_rate_text,
        annual_volume_estimate_usd=award.annual_volume_estimate_usd,
        contract_term_years=award.contract_term_years,
        source_url=award.source_url,
        notes=award.notes,
        confirmed_by=list(confirmed_by),
        needs_review=(not consensus),
    )


def _bumped_competitor(
    comp: CompetitorProcessor,
    *,
    confirmed_by: list[str],
    consensus: bool,
) -> CompetitorProcessor:
    return CompetitorProcessor(
        name=comp.name,
        market_position=comp.market_position,
        typical_pricing_summary=comp.typical_pricing_summary,
        likelihood_to_bid=comp.likelihood_to_bid,
        source_urls=list(comp.source_urls or []),
        notes=comp.notes,
        confirmed_by=list(confirmed_by),
        needs_review=(not consensus),
    )


def _merge_pricing_structure(
    a: PaymentPricingStructure,
    b: PaymentPricingStructure,
) -> PaymentPricingStructure:
    """Pass A's recommendation wins on the categorical fields
    (pricing_model, rationale, positioning). Numeric medians are
    averaged when both providers produced them; missing values fill
    from whichever provider has them."""
    other_fees: list[dict] = []
    if a.other_fees_recommended:
        other_fees.extend(a.other_fees_recommended)
    seen_names = {f.get("name", "") for f in other_fees}
    for fee in b.other_fees_recommended or []:
        if fee.get("name", "") not in seen_names:
            other_fees.append(fee)
            seen_names.add(fee.get("name", ""))

    return PaymentPricingStructure(
        pricing_model=a.pricing_model or b.pricing_model,
        pricing_model_rationale=(a.pricing_model_rationale or b.pricing_model_rationale),
        median_market_credit_card_markup_bps=_avg_int(
            a.median_market_credit_card_markup_bps,
            b.median_market_credit_card_markup_bps,
        ),
        proposed_credit_card_markup_bps=_avg_int(
            a.proposed_credit_card_markup_bps,
            b.proposed_credit_card_markup_bps,
        ),
        median_market_per_txn_fee_usd=_avg(
            a.median_market_per_txn_fee_usd,
            b.median_market_per_txn_fee_usd,
        ),
        proposed_per_txn_fee_usd=_avg(
            a.proposed_per_txn_fee_usd,
            b.proposed_per_txn_fee_usd,
        ),
        median_market_ach_fee_usd=_avg(
            a.median_market_ach_fee_usd,
            b.median_market_ach_fee_usd,
        ),
        proposed_ach_fee_usd=_avg(
            a.proposed_ach_fee_usd,
            b.proposed_ach_fee_usd,
        ),
        median_market_monthly_fee_usd=_avg(
            a.median_market_monthly_fee_usd,
            b.median_market_monthly_fee_usd,
        ),
        proposed_monthly_fee_usd=_avg(
            a.proposed_monthly_fee_usd,
            b.proposed_monthly_fee_usd,
        ),
        other_fees_recommended=other_fees,
        rate_positioning=a.rate_positioning or b.rate_positioning or "match",
    )


def _merge_volume(
    a: VolumeEstimate,
    b: VolumeEstimate,
) -> VolumeEstimate:
    """Average each band tier across providers; concatenate bases."""
    bases: list[str] = []
    if a.estimation_basis:
        bases.append(f"[Gemini] {a.estimation_basis}")
    if b.estimation_basis:
        bases.append(f"[Claude+web] {b.estimation_basis}")

    # Confidence: take the higher of the two.
    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    a_rank = confidence_rank.get((a.confidence or "low").lower(), 0)
    b_rank = confidence_rank.get((b.confidence or "low").lower(), 0)
    chosen_confidence = (a.confidence if a_rank >= b_rank else b.confidence) or "low"

    return VolumeEstimate(
        annual_processed_volume_low_usd=_avg(
            a.annual_processed_volume_low_usd,
            b.annual_processed_volume_low_usd,
        ),
        annual_processed_volume_midpoint_usd=_avg(
            a.annual_processed_volume_midpoint_usd,
            b.annual_processed_volume_midpoint_usd,
        ),
        annual_processed_volume_high_usd=_avg(
            a.annual_processed_volume_high_usd,
            b.annual_processed_volume_high_usd,
        ),
        estimated_transaction_count_annual=_avg_int(
            a.estimated_transaction_count_annual,
            b.estimated_transaction_count_annual,
        ),
        average_transaction_size_usd=_avg(
            a.average_transaction_size_usd,
            b.average_transaction_size_usd,
        ),
        estimation_basis=" | ".join(bases) if bases else "",
        confidence=chosen_confidence,
    )


def _merge_citations(
    a: list[dict],
    b: list[dict],
) -> list[dict]:
    """Union by URI. First-seen wins for the title/label."""
    seen: dict[str, dict] = {}
    for c in (a or []) + (b or []):
        uri = (c.get("uri") or "").strip()
        if not uri or uri in seen:
            continue
        seen[uri] = {"title": c.get("title", ""), "uri": uri}
    return list(seen.values())


def consolidate_payment_market_research(
    *,
    proposal_id: int,
    pass_a: PaymentMarketScanResult,
    pass_b: PaymentMarketScanResult,
) -> PaymentMarketScanResult:
    """Merge two providers' PaymentMarketScanResult outputs into one.
    Returns a single result with provenance attribution. ProfitMath
    is left blank — orchestrator fills it after consolidation using
    consolidated rates × consolidated volume − cost basis."""
    # ---- Awards: dedupe by (processor, customer) ----
    a_by_key: dict[str, ComparableProcessorAward] = {}
    a_singletons: list[ComparableProcessorAward] = []
    for aw in pass_a.comparable_awards or []:
        key = _award_key(aw)
        if key:
            a_by_key.setdefault(key, aw)
        else:
            a_singletons.append(aw)

    b_by_key: dict[str, ComparableProcessorAward] = {}
    b_singletons: list[ComparableProcessorAward] = []
    for aw in pass_b.comparable_awards or []:
        key = _award_key(aw)
        if key:
            b_by_key.setdefault(key, aw)
        else:
            b_singletons.append(aw)

    consensus_keys = [k for k in a_by_key if k in b_by_key]
    only_a_keys = [k for k in a_by_key if k not in b_by_key]
    only_b_keys = [k for k in b_by_key if k not in a_by_key]

    merged_awards: list[ComparableProcessorAward] = []
    for k in consensus_keys:
        # Prefer Pass A's row; backfill numeric fields from Pass B
        # where Pass A is null.
        a_aw = a_by_key[k]
        b_aw = b_by_key[k]
        merged = ComparableProcessorAward(
            processor_name=a_aw.processor_name or b_aw.processor_name,
            customer_name=a_aw.customer_name or b_aw.customer_name,
            award_year=a_aw.award_year if a_aw.award_year is not None else b_aw.award_year,
            pricing_model=a_aw.pricing_model or b_aw.pricing_model,
            disclosed_credit_card_rate_text=(
                a_aw.disclosed_credit_card_rate_text or b_aw.disclosed_credit_card_rate_text
            ),
            annual_volume_estimate_usd=_avg(
                a_aw.annual_volume_estimate_usd,
                b_aw.annual_volume_estimate_usd,
            ),
            contract_term_years=(
                a_aw.contract_term_years if a_aw.contract_term_years is not None else b_aw.contract_term_years
            ),
            source_url=a_aw.source_url or b_aw.source_url,
            notes=a_aw.notes or b_aw.notes,
        )
        merged_awards.append(
            _bumped_award(
                merged,
                confirmed_by=["gemini", "claude"],
                consensus=True,
            )
        )
    for k in only_a_keys:
        merged_awards.append(
            _bumped_award(
                a_by_key[k],
                confirmed_by=["gemini"],
                consensus=False,
            )
        )
    for k in only_b_keys:
        merged_awards.append(
            _bumped_award(
                b_by_key[k],
                confirmed_by=["claude"],
                consensus=False,
            )
        )
    # Singletons (no key) flow through with single-provider attribution
    for aw in a_singletons:
        merged_awards.append(
            _bumped_award(
                aw,
                confirmed_by=["gemini"],
                consensus=False,
            )
        )
    for aw in b_singletons:
        merged_awards.append(
            _bumped_award(
                aw,
                confirmed_by=["claude"],
                consensus=False,
            )
        )

    # ---- Competitors: dedupe by canonicalized firm name ----
    a_comp_by_canon: dict[str, CompetitorProcessor] = {}
    for c in pass_a.competitor_processors or []:
        canon = _canonicalize_name(c.name)
        if canon and canon not in a_comp_by_canon:
            a_comp_by_canon[canon] = c

    b_comp_by_canon: dict[str, CompetitorProcessor] = {}
    for c in pass_b.competitor_processors or []:
        canon = _canonicalize_name(c.name)
        if canon and canon not in b_comp_by_canon:
            b_comp_by_canon[canon] = c

    comp_consensus = [c for c in a_comp_by_canon if c in b_comp_by_canon]
    comp_only_a = [c for c in a_comp_by_canon if c not in b_comp_by_canon]
    comp_only_b = [c for c in b_comp_by_canon if c not in a_comp_by_canon]

    merged_competitors: list[CompetitorProcessor] = []
    for canon in comp_consensus:
        a_c = a_comp_by_canon[canon]
        b_c = b_comp_by_canon[canon]
        merged_c = CompetitorProcessor(
            name=a_c.name,  # Pass A's surface form
            market_position=a_c.market_position or b_c.market_position,
            typical_pricing_summary=(a_c.typical_pricing_summary or b_c.typical_pricing_summary),
            likelihood_to_bid=a_c.likelihood_to_bid or b_c.likelihood_to_bid,
            source_urls=list({*(a_c.source_urls or []), *(b_c.source_urls or [])}),
            notes=a_c.notes or b_c.notes,
        )
        merged_competitors.append(
            _bumped_competitor(
                merged_c,
                confirmed_by=["gemini", "claude"],
                consensus=True,
            )
        )
    for canon in comp_only_a:
        merged_competitors.append(
            _bumped_competitor(
                a_comp_by_canon[canon],
                confirmed_by=["gemini"],
                consensus=False,
            )
        )
    for canon in comp_only_b:
        merged_competitors.append(
            _bumped_competitor(
                b_comp_by_canon[canon],
                confirmed_by=["claude"],
                consensus=False,
            )
        )

    # ---- Pricing structure + volume + insufficient warning + citations ----
    pricing = _merge_pricing_structure(
        pass_a.pricing_structure,
        pass_b.pricing_structure,
    )
    volume = _merge_volume(
        pass_a.volume_estimate,
        pass_b.volume_estimate,
    )
    insufficient_data = bool(pass_a.insufficient_data_warning or pass_b.insufficient_data_warning)
    citations = _merge_citations(pass_a.citations, pass_b.citations)

    log.info(
        "payment_market_consolidator: proposal %d — "
        "awards: consensus=%d only_a=%d only_b=%d singletons=%d merged=%d · "
        "competitors: consensus=%d only_a=%d only_b=%d merged=%d · "
        "insufficient=%s",
        proposal_id,
        len(consensus_keys),
        len(only_a_keys),
        len(only_b_keys),
        len(a_singletons) + len(b_singletons),
        len(merged_awards),
        len(comp_consensus),
        len(comp_only_a),
        len(comp_only_b),
        len(merged_competitors),
        insufficient_data,
    )

    return PaymentMarketScanResult(
        pricing_structure=pricing,
        comparable_awards=merged_awards,
        competitor_processors=merged_competitors,
        volume_estimate=volume,
        profit_math=ProfitMath(),  # filled by orchestrator
        insufficient_data_warning=insufficient_data,
        citations=citations,
    )


__all__ = [
    "consolidate_payment_market_research",
]
