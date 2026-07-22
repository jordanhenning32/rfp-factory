"""Persistence helpers for MarketScan + relational detail rows.

Re-running the Cost Market Researcher REPLACES the existing scan for
a proposal — the unique constraint on market_scans.proposal_id enforces
one-scan-per-proposal. Cascade deletes on the detail tables clear
comparable_awards + competitors when the parent scan is replaced.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agents.market_researcher import MarketScanResult
from app.db.session import session_scope
from app.models import (
    MarketScan,
    MarketScanComparableAward,
    MarketScanCompetitor,
)
from app.services.proposal_access import ensure_proposal_mutable

log = logging.getLogger(__name__)


def upsert_market_scan(
    *,
    proposal_id: int,
    result: MarketScanResult,
    agent_run_id: int | None = None,
) -> int:
    """Replace any existing scan for the proposal with the new result.
    Returns the new market_scans.id.

    The cascade on detail tables clears the old comparable_awards and
    competitors automatically. We delete + insert (rather than diff)
    because re-runs should treat the new result as authoritative and
    we don't need to preserve row IDs.
    """
    with session_scope() as db:
        ensure_proposal_mutable(
            db, proposal_id, operation="replace market research",
        )
        existing = db.execute(
            select(MarketScan).where(MarketScan.proposal_id == proposal_id)
        ).scalar_one_or_none()
        if existing is not None:
            db.delete(existing)
            db.flush()

        scan = MarketScan(
            proposal_id=proposal_id,
            market_band_low_usd=result.market_band_low_usd,
            market_band_mid_usd=result.market_band_mid_usd,
            market_band_high_usd=result.market_band_high_usd,
            methodology=_compose_methodology(
                result.methodology,
                result.insufficient_data_warning,
            ),
            agent_run_id=agent_run_id,
        )
        db.add(scan)
        db.flush()  # need scan.id for FK on detail rows

        for a in result.comparable_awards:
            db.add(
                MarketScanComparableAward(
                    market_scan_id=scan.id,
                    award_title=a.award_title,
                    award_value_usd=a.award_value_usd,
                    period_of_performance_months=a.period_of_performance_months,
                    awardee_name=a.awardee_name,
                    customer_agency=a.customer_agency,
                    source_url=a.source_url,
                    relevance_score=a.relevance_score,
                    notes=a.notes,
                    confirmed_by=list(a.confirmed_by or []),
                    needs_review=bool(a.needs_review),
                )
            )
        for c in result.competitors:
            db.add(
                MarketScanCompetitor(
                    market_scan_id=scan.id,
                    competitor_name=c.name,
                    likelihood_to_bid=c.likelihood_to_bid,
                    estimated_rate_low_usd=c.estimated_rate_low_usd,
                    estimated_rate_high_usd=c.estimated_rate_high_usd,
                    rate_estimation_basis=c.rate_estimation_basis,
                    source_urls=c.source_urls,
                    notes=c.notes,
                    confirmed_by=list(c.confirmed_by or []),
                    needs_review=bool(c.needs_review),
                )
            )
        db.flush()
        new_id = scan.id

    log.info(
        "market_scan: upserted scan id=%d for proposal %d (%d awards, %d competitors)",
        new_id,
        proposal_id,
        len(result.comparable_awards),
        len(result.competitors),
    )
    return new_id


def get_market_scan_snapshot(proposal_id: int) -> dict[str, Any] | None:
    """Read-only snapshot of the current scan + detail rows. Returns
    a plain-dict structure so callers can access fields after the
    session closes (no DetachedInstanceError).
    """
    with session_scope() as db:
        scan = db.execute(
            select(MarketScan)
            .where(MarketScan.proposal_id == proposal_id)
            .options(
                selectinload(MarketScan.comparable_awards),
                selectinload(MarketScan.competitors),
            )
        ).scalar_one_or_none()
        if scan is None:
            return None
        return {
            "id": scan.id,
            "proposal_id": scan.proposal_id,
            "market_band_low_usd": (
                float(scan.market_band_low_usd) if scan.market_band_low_usd is not None else None
            ),
            "market_band_mid_usd": (
                float(scan.market_band_mid_usd) if scan.market_band_mid_usd is not None else None
            ),
            "market_band_high_usd": (
                float(scan.market_band_high_usd) if scan.market_band_high_usd is not None else None
            ),
            "methodology": scan.methodology,
            "agent_run_id": scan.agent_run_id,
            "created_at": scan.created_at,
            "updated_at": scan.updated_at,
            "comparable_awards": [
                {
                    "id": a.id,
                    "award_title": a.award_title,
                    "award_value_usd": (float(a.award_value_usd) if a.award_value_usd is not None else None),
                    "period_of_performance_months": a.period_of_performance_months,
                    "awardee_name": a.awardee_name,
                    "customer_agency": a.customer_agency,
                    "source_url": a.source_url,
                    "relevance_score": a.relevance_score,
                    "notes": a.notes,
                    "confirmed_by": list(a.confirmed_by or []),
                    "needs_review": bool(a.needs_review),
                }
                for a in scan.comparable_awards
            ],
            "competitors": [
                {
                    "id": c.id,
                    "competitor_name": c.competitor_name,
                    "likelihood_to_bid": c.likelihood_to_bid,
                    "estimated_rate_low_usd": (
                        float(c.estimated_rate_low_usd) if c.estimated_rate_low_usd is not None else None
                    ),
                    "estimated_rate_high_usd": (
                        float(c.estimated_rate_high_usd) if c.estimated_rate_high_usd is not None else None
                    ),
                    "rate_estimation_basis": c.rate_estimation_basis,
                    "source_urls": list(c.source_urls or []),
                    "notes": c.notes,
                    "confirmed_by": list(c.confirmed_by or []),
                    "needs_review": bool(c.needs_review),
                }
                for c in scan.competitors
            ],
        }


def _compose_methodology(
    methodology: str,
    insufficient_warning: str | None,
) -> str | None:
    """Combine the agent's methodology with any sparse-data warning so
    both surface in the UI without needing a separate column.
    """
    parts = []
    if methodology and methodology.strip():
        parts.append(methodology.strip())
    if insufficient_warning and insufficient_warning.strip():
        parts.append(f"WARNING: {insufficient_warning.strip()}")
    if not parts:
        return None
    return "\n\n".join(parts)


__all__ = [
    "upsert_market_scan",
    "get_market_scan_snapshot",
]
