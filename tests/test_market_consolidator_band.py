from __future__ import annotations

from app.agents.market_consolidator import consolidate_market_research
from app.agents.market_researcher import ComparableAward, MarketScanResult


def _award(
    title: str,
    value: float | None,
    pop_months: int | None,
    relevance: float,
) -> ComparableAward:
    return ComparableAward(
        award_title=title,
        award_value_usd=value,
        period_of_performance_months=pop_months,
        awardee_name="Vendor",
        customer_agency="Agency",
        source_url=f"https://example.test/{title.lower().replace(' ', '-')}",
        relevance_score=relevance,
        notes="test comparable",
    )


def test_consolidator_recalculates_band_from_valued_awards() -> None:
    pass_a = MarketScanResult(
        market_band_low_usd=585_000,
        market_band_mid_usd=800_000,
        market_band_high_usd=1_170_000,
        methodology="provider tried to anchor on rough range",
        comparable_awards=[
            _award("Two Year Similar Award", 1_200_000, 24, 0.9),
            _award("One Year Similar Award", 900_000, 12, 0.8),
            _award("Huge Low-Relevance Platform", 25_000_000, 60, 0.2),
        ],
    )
    pass_b = MarketScanResult(
        market_band_low_usd=None,
        market_band_mid_usd=None,
        market_band_high_usd=None,
        methodology="",
    )

    result = consolidate_market_research(
        proposal_id=123,
        pass_a=pass_a,
        pass_b=pass_b,
        target_pop_months=12,
    )

    assert result.market_band_low_usd == 600_000
    assert result.market_band_mid_usd == 750_000
    assert result.market_band_high_usd == 900_000
    assert "recalculated the persisted market band" in result.methodology
    assert result.insufficient_data_warning is None


def test_consolidator_does_not_persist_band_with_too_few_valued_awards() -> None:
    pass_a = MarketScanResult(
        market_band_low_usd=585_000,
        market_band_mid_usd=800_000,
        market_band_high_usd=1_170_000,
        methodology="fallback-derived provider band",
        comparable_awards=[
            _award("Value Missing Comparable", None, 12, 0.9),
            _award("Low Relevance Value", 2_000_000, 12, 0.2),
        ],
    )
    pass_b = MarketScanResult(
        market_band_low_usd=600_000,
        market_band_mid_usd=900_000,
        market_band_high_usd=1_200_000,
        methodology="another fallback-derived provider band",
    )

    result = consolidate_market_research(
        proposal_id=456,
        pass_a=pass_a,
        pass_b=pass_b,
        target_pop_months=12,
    )

    assert result.market_band_low_usd is None
    assert result.market_band_mid_usd is None
    assert result.market_band_high_usd is None
    assert "did not persist a market band" in result.methodology
    assert result.insufficient_data_warning is not None
    assert "Only 0 valued comparable award" in result.insufficient_data_warning
