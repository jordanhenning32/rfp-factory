"""Cost Market Consolidator — merges the two providers' market-scan
outputs (Gemini-grounded + Claude+web_search) into a single
`MarketScanResult` with provenance attribution per award and
competitor.

Pure-Python merge, no LLM call. Identity matching:
  - Awards: canonicalized award_title (drops case, punctuation, stop
    words like "task order", "contract", year/version suffixes).
    Different agencies often run similarly-titled re-competes; the
    canonical key is loose enough to merge them when both providers
    surface the same underlying contract.
  - Competitors: canonicalized firm name (reuses the teaming
    consolidator's helper — same `(first-initial, last-word)`-style
    suffix-stripping). Booz Allen Hamilton vs Booz Allen Hamilton
    Inc. → same firm.

Per row the consolidator records:
  confirmed_by  — list[str] subset of {"gemini", "claude"}.
                  Length 2 = both surfaced this row; 1 = single
                  provider.
  needs_review  — True for single-provider rows the user should
                  verify before relying on. Awards: any single-
                  provider row with relevance < 0.7. Competitors:
                  any single-provider firm (rate inference is
                  inherently noisy and a single source isn't enough).

Market band is averaged across providers when both produced one;
methodology concatenates Pass A's text + a "Pass B agreed: ..." or
"Pass B differed: ..." note. Insufficient_data_warning is OR'd
(either provider flagging insufficient → consolidated flags
insufficient).
"""

from __future__ import annotations

import logging
import re
from statistics import mean

from app.agents.market_researcher import (
    ComparableAward,
    Competitor,
    MarketScanResult,
)
from app.agents.teaming_consolidator import _canonicalize_name

log = logging.getLogger(__name__)


# Common federal-contract boilerplate words to strip from award
# titles before computing the dedupe key. Aggressive enough to match
# "Healthcare.gov Modernization Task Order 0001" with "Healthcare.gov
# Modernization (Contract HHSM-500-2020)" — the content tokens
# ("healthcare", "gov", "modernization") survive both.
_AWARD_BOILERPLATE = {
    "contract",
    "award",
    "task",
    "order",
    "agreement",
    "bpa",
    "idiq",
    "gwac",
    "sow",
    "for",
    "the",
    "and",
    "with",
    "from",
    "blanket",
    "purchase",
    "delivery",
    "version",
    "vol",
    "volume",
    "fy",
    "phase",
    "modification",
    "under",
    "no",
    "nbr",
    "number",
}


def _canonicalize_award_title(title: str) -> str:
    """Loose canonicalization for matching the same underlying award
    across two providers' descriptions. Lowercases, drops punctuation,
    drops common federal-contract boilerplate words, drops pure-digit
    tokens (years, identifiers), then sorts the remaining content
    words so word-order differences don't break the match.

    Limitation: opaque alphanumeric contract identifiers (e.g.
    'HHSM-500-2020-000XX' → 'hhsm 000xx') survive as content tokens
    when only one provider includes them. Awards differing only on
    identifier won't merge and will appear separately with their
    respective provider chips — a known weak point users can manually
    reconcile. Competitor consolidation has no such issue (firm names
    are stable).
    """
    s = (title or "").strip().lower()
    # Replace any non-alphanumeric with whitespace; preserves digits
    # inside tokens like "1ststep" but they'd get filtered downstream
    # if they're pure-numeric.
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = [w for w in s.split() if len(w) >= 3 and w not in _AWARD_BOILERPLATE and not w.isdigit()]
    return " ".join(sorted(words))


def _bumped_award(
    award: ComparableAward,
    *,
    confirmed_by: list[str],
    consensus: bool,
) -> ComparableAward:
    """Attach provenance to an award. Awards don't have a HIGH/MED/LOW
    confidence field — we use relevance_score as the proxy. needs_review
    fires for single-provider rows below 0.7 relevance."""
    rel = award.relevance_score if award.relevance_score is not None else 0.5
    needs_review = (not consensus) and rel < 0.7
    return ComparableAward(
        award_title=award.award_title,
        award_value_usd=award.award_value_usd,
        period_of_performance_months=award.period_of_performance_months,
        awardee_name=award.awardee_name,
        customer_agency=award.customer_agency,
        source_url=award.source_url,
        relevance_score=award.relevance_score,
        notes=award.notes,
        confirmed_by=list(confirmed_by),
        needs_review=needs_review,
    )


def _bumped_competitor(
    competitor: Competitor,
    *,
    confirmed_by: list[str],
    consensus: bool,
) -> Competitor:
    """Attach provenance to a competitor. Single-provider competitors
    always get needs_review=True — rate inference depends heavily on
    which awards a single provider found, and a different provider
    might infer a very different range. Cross-verification matters."""
    return Competitor(
        name=competitor.name,
        likelihood_to_bid=competitor.likelihood_to_bid,
        estimated_rate_low_usd=competitor.estimated_rate_low_usd,
        estimated_rate_high_usd=competitor.estimated_rate_high_usd,
        rate_estimation_basis=competitor.rate_estimation_basis,
        source_urls=list(competitor.source_urls or []),
        notes=competitor.notes,
        confirmed_by=list(confirmed_by),
        needs_review=(not consensus),
    )


def _merge_band(
    a: MarketScanResult,
    b: MarketScanResult,
) -> tuple[float | None, float | None, float | None]:
    """Average each band tier across providers when both produced
    a value; fall back to whichever single provider has it."""

    def _avg(x: float | None, y: float | None) -> float | None:
        if x is not None and y is not None:
            return float(mean([x, y]))
        return x if x is not None else y

    return (
        _avg(a.market_band_low_usd, b.market_band_low_usd),
        _avg(a.market_band_mid_usd, b.market_band_mid_usd),
        _avg(a.market_band_high_usd, b.market_band_high_usd),
    )


def _merge_methodology(a: MarketScanResult, b: MarketScanResult) -> str:
    """Compose a methodology string that captures both providers'
    reasoning. Pass A leads (Gemini has the longer track record on
    this task); Pass B is appended as agreement / variance note."""
    parts: list[str] = []
    a_text = (a.methodology or "").strip()
    b_text = (b.methodology or "").strip()
    if a_text:
        parts.append(f"[Gemini grounded] {a_text}")
    if b_text:
        parts.append(f"[Claude+web] {b_text}")
    if not parts:
        return "(no methodology produced by either provider)"
    return "\n\n".join(parts)


def _merge_insufficient(
    a: MarketScanResult,
    b: MarketScanResult,
) -> str | None:
    """OR the two providers' insufficient_data warnings. If either
    flagged, the consolidated result flags too — better to caveat than
    silently let one provider's optimism mask sparse data."""
    a_warn = (a.insufficient_data_warning or "").strip()
    b_warn = (b.insufficient_data_warning or "").strip()
    if not a_warn and not b_warn:
        return None
    if a_warn and b_warn:
        return f"Both providers flagged sparse data. Pass A: {a_warn} | Pass B: {b_warn}"
    if a_warn:
        return f"Pass A (Gemini) flagged sparse data: {a_warn}"
    return f"Pass B (Claude+web) flagged sparse data: {b_warn}"


def consolidate_market_research(
    *,
    proposal_id: int,
    pass_a: MarketScanResult,
    pass_b: MarketScanResult,
) -> MarketScanResult:
    """Merge two providers' MarketScanResult outputs into one. Returns
    a single MarketScanResult ready for `upsert_market_scan` —
    persistence and the UI consume it the same way as a single-
    provider result.
    """
    # ---- Awards: dedupe by canonicalized title ----
    a_awards_by_canon: dict[str, ComparableAward] = {}
    for aw in pass_a.comparable_awards or []:
        canon = _canonicalize_award_title(aw.award_title)
        if canon and canon not in a_awards_by_canon:
            a_awards_by_canon[canon] = aw

    b_awards_by_canon: dict[str, ComparableAward] = {}
    for aw in pass_b.comparable_awards or []:
        canon = _canonicalize_award_title(aw.award_title)
        if canon and canon not in b_awards_by_canon:
            b_awards_by_canon[canon] = aw

    award_consensus = [c for c in a_awards_by_canon if c in b_awards_by_canon]
    award_only_a = [c for c in a_awards_by_canon if c not in b_awards_by_canon]
    award_only_b = [c for c in b_awards_by_canon if c not in a_awards_by_canon]

    merged_awards: list[ComparableAward] = []
    for canon in award_consensus:
        # Prefer Pass A's row (Gemini's grounded data tends to have
        # better source URLs on federal procurement sites).
        merged_awards.append(
            _bumped_award(
                a_awards_by_canon[canon],
                confirmed_by=["gemini", "claude"],
                consensus=True,
            )
        )
    for canon in award_only_a:
        merged_awards.append(
            _bumped_award(
                a_awards_by_canon[canon],
                confirmed_by=["gemini"],
                consensus=False,
            )
        )
    for canon in award_only_b:
        merged_awards.append(
            _bumped_award(
                b_awards_by_canon[canon],
                confirmed_by=["claude"],
                consensus=False,
            )
        )

    # ---- Competitors: dedupe by canonicalized firm name ----
    a_comp_by_canon: dict[str, Competitor] = {}
    for c in pass_a.competitors or []:
        canon = _canonicalize_name(c.name)
        if canon and canon not in a_comp_by_canon:
            a_comp_by_canon[canon] = c

    b_comp_by_canon: dict[str, Competitor] = {}
    for c in pass_b.competitors or []:
        canon = _canonicalize_name(c.name)
        if canon and canon not in b_comp_by_canon:
            b_comp_by_canon[canon] = c

    comp_consensus = [c for c in a_comp_by_canon if c in b_comp_by_canon]
    comp_only_a = [c for c in a_comp_by_canon if c not in b_comp_by_canon]
    comp_only_b = [c for c in b_comp_by_canon if c not in a_comp_by_canon]

    merged_competitors: list[Competitor] = []
    for canon in comp_consensus:
        a_c = a_comp_by_canon[canon]
        b_c = b_comp_by_canon[canon]

        # Average the rate ranges when both providers had numbers;
        # otherwise prefer whichever provider had non-null values.
        def _avg(x: float | None, y: float | None) -> float | None:
            if x is not None and y is not None:
                return float(mean([x, y]))
            return x if x is not None else y

        merged = Competitor(
            name=a_c.name,  # use Pass A's surface form
            likelihood_to_bid=a_c.likelihood_to_bid,
            estimated_rate_low_usd=_avg(
                a_c.estimated_rate_low_usd,
                b_c.estimated_rate_low_usd,
            ),
            estimated_rate_high_usd=_avg(
                a_c.estimated_rate_high_usd,
                b_c.estimated_rate_high_usd,
            ),
            rate_estimation_basis=(a_c.rate_estimation_basis or b_c.rate_estimation_basis or ""),
            source_urls=list({*(a_c.source_urls or []), *(b_c.source_urls or [])}),
            notes=a_c.notes or b_c.notes or "",
        )
        merged_competitors.append(
            _bumped_competitor(
                merged,
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

    # ---- Band + methodology + insufficient warning ----
    band_low, band_mid, band_high = _merge_band(pass_a, pass_b)
    methodology = _merge_methodology(pass_a, pass_b)
    insufficient = _merge_insufficient(pass_a, pass_b)

    log.info(
        "market_consolidator: proposal %d — "
        "awards: consensus=%d only_a=%d only_b=%d merged=%d · "
        "competitors: consensus=%d only_a=%d only_b=%d merged=%d · "
        "band=$%s/$%s/$%s",
        proposal_id,
        len(award_consensus),
        len(award_only_a),
        len(award_only_b),
        len(merged_awards),
        len(comp_consensus),
        len(comp_only_a),
        len(comp_only_b),
        len(merged_competitors),
        band_low,
        band_mid,
        band_high,
    )

    return MarketScanResult(
        market_band_low_usd=band_low,
        market_band_mid_usd=band_mid,
        market_band_high_usd=band_high,
        methodology=methodology,
        comparable_awards=merged_awards,
        competitors=merged_competitors,
        insufficient_data_warning=insufficient,
    )


__all__ = [
    "consolidate_market_research",
]
