"""Proposal-outcome ledger service.

CRUD + summary rollup + learning-feed pull. Outcome rows are the
post-submission half of the proposal lifecycle; the ledger is consumed
by `app.services.lessons.format_reviewer_guidance` to bias reviewer
agents based on patterns from won vs lost proposals.

Four public exports:
    get_outcome(proposal_id)             — read one row (or None)
    upsert_outcome(*, proposal_id, ...)  — create-or-update; None-kwargs
                                             preserve existing values
    list_outcomes_for_learning(...)      — historical rows for the hook
    get_win_rate_summary(...)            — rollup for the dashboard

All four call through `session_scope()`. `get_outcome` and
`list_outcomes_for_learning` call `db.expunge_all()` before returning so
callers can read attributes after the session closes (mirrors the
snapshot pattern used in `app/services/amendments.py`).
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime

from sqlalchemy import select

from app.core.enums import ProposalOutcomeStatus

# Import the module (not the symbol) so the tests' inmemory_db fixture
# can monkeypatch `app.db.session.session_scope` and every call below
# picks the patched callable up at call time. The existing services
# bind the symbol at module-import time; that works for them because
# the existing test suite never exercises a service whose internal
# session_scope() call needs redirection across test boundaries.
from app.db import session as _db_session
from app.models import Proposal, ProposalOutcome

log = logging.getLogger(__name__)


__all__ = [
    "get_outcome",
    "get_win_rate_summary",
    "list_outcomes_for_learning",
    "upsert_outcome",
]


def _coerce_outcome_value(outcome: ProposalOutcomeStatus | str) -> str:
    """Accept either the enum or its string value; return the string value.

    Defensive — every caller in this module passes either form. We always
    store the string in the column (the column is String(20), not a
    DB-side enum).
    """
    if hasattr(outcome, "value"):
        return str(outcome.value)
    return str(outcome)


def get_outcome(proposal_id: int) -> ProposalOutcome | None:
    """Return the outcome row for a proposal, or None if unset.

    Returns a detached ORM instance (db.expunge_all() before close) so
    callers can read fields after the session_scope() exits — mirrors
    the snapshot pattern used by app/services/amendments.py.
    """
    with _db_session.session_scope() as db:
        row = db.execute(
            select(ProposalOutcome).where(ProposalOutcome.proposal_id == proposal_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        db.expunge_all()
        return row


def upsert_outcome(
    *,
    proposal_id: int,
    outcome: ProposalOutcomeStatus,
    submitted_at: datetime | None = None,
    decided_at: datetime | None = None,
    our_proposed_price_usd: float | None = None,
    awarded_price_usd: float | None = None,
    awarded_to: str | None = None,
    debrief_received: bool | None = None,
    our_total_score: float | None = None,
    winning_total_score: float | None = None,
    debrief_notes: str | None = None,
    factor_scores: list[dict] | None = None,
) -> ProposalOutcome:
    """Create or update the single outcome row for a proposal.

    None-kwargs preserve existing values (per the prompt). `outcome` is
    REQUIRED — every UI invocation carries an explicit choice (PENDING
    being a real outcome value, not a sentinel for "no change").
    `factor_scores` writes to `factor_scores_json` (the ORM Mapped type
    is JSON; SQLAlchemy serializes the list).
    """
    outcome_value = _coerce_outcome_value(outcome)

    with _db_session.session_scope() as db:
        row = db.execute(
            select(ProposalOutcome).where(ProposalOutcome.proposal_id == proposal_id)
        ).scalar_one_or_none()

        if row is None:
            row = ProposalOutcome(
                proposal_id=proposal_id,
                outcome=outcome_value,
                submitted_at=submitted_at,
                decided_at=decided_at,
                our_proposed_price_usd=our_proposed_price_usd,
                awarded_price_usd=awarded_price_usd,
                awarded_to=awarded_to,
                debrief_received=bool(debrief_received) if debrief_received is not None else False,
                our_total_score=our_total_score,
                winning_total_score=winning_total_score,
                debrief_notes=debrief_notes,
                factor_scores_json=factor_scores,
            )
            db.add(row)
            db.flush()
        else:
            # Preserve-existing-when-None semantics: only overwrite when
            # the caller actually passed a value. `outcome` is non-None by
            # signature so it always overwrites.
            row.outcome = outcome_value
            if submitted_at is not None:
                row.submitted_at = submitted_at
            if decided_at is not None:
                row.decided_at = decided_at
            if our_proposed_price_usd is not None:
                row.our_proposed_price_usd = our_proposed_price_usd
            if awarded_price_usd is not None:
                row.awarded_price_usd = awarded_price_usd
            if awarded_to is not None:
                row.awarded_to = awarded_to
            if debrief_received is not None:
                row.debrief_received = bool(debrief_received)
            if our_total_score is not None:
                row.our_total_score = our_total_score
            if winning_total_score is not None:
                row.winning_total_score = winning_total_score
            if debrief_notes is not None:
                row.debrief_notes = debrief_notes
            if factor_scores is not None:
                row.factor_scores_json = factor_scores
            db.flush()

        db.expunge_all()
        return row


def list_outcomes_for_learning(
    *,
    service_line: str | None = None,
    since: datetime | None = None,
) -> list[ProposalOutcome]:
    """Pull historical outcomes for ad-hoc analysis / future tooling.

    NOTE on intended consumer: the original spec called this the
    "feeding function" for the reviewer-guidance hook in
    app/services/lessons.py. In practice `_format_outcome_calibration`
    issues a direct SQL aggregate (GROUP BY category × outcome) rather
    than fetching rows and grouping in Python — that path is cheaper
    on large ledgers and avoids round-tripping every outcome row into
    the orchestrator's memory.

    This helper is retained as a clean row-level read for: dashboard
    analyses, future per-RFP debriefing UIs, and exporters that need
    the full ProposalOutcome objects rather than aggregate counts.
    Tests exercise the contract; no production caller today.

    JOIN on Proposal for `service_line` filter; filter `decided_at >=
    since` when provided. Excludes PENDING (the column comparison uses
    the string value, matching how the row is stored). Order:
    decided_at DESC, NULLs last.

    Returns detached snapshots (`expunge_all` before return) so callers
    can read fields after the session_scope() exits.
    """
    with _db_session.session_scope() as db:
        q = (
            select(ProposalOutcome)
            .join(Proposal, Proposal.id == ProposalOutcome.proposal_id)
            .where(ProposalOutcome.outcome != ProposalOutcomeStatus.PENDING.value)
        )
        if service_line is not None:
            q = q.where(Proposal.service_line == service_line)
        if since is not None:
            q = q.where(ProposalOutcome.decided_at.is_not(None))
            q = q.where(ProposalOutcome.decided_at >= since)
        q = q.order_by(ProposalOutcome.decided_at.desc().nullslast())
        rows = list(db.execute(q).scalars().all())
        db.expunge_all()
        return rows


def get_win_rate_summary(
    *,
    service_line: str | None = None,
    since: datetime | None = None,
) -> dict:
    """Roll up outcome counts + win-rate + median prices for the Dashboard.

    Excludes PENDING from `total`. Returns:
        {
            "total": int,
            "won": int,
            "lost": int,
            "no_award": int,
            "withdrawn": int,
            "win_rate_pct": float | None,
            "median_awarded_price_usd": float | None,
            "median_our_proposed_price_usd": float | None,
        }

    Win-rate denominator: won + lost (excludes no_award + withdrawn so
    cancellations + voluntary pullouts don't dilute the rate).
    `win_rate_pct` is None when `won + lost == 0`. Medians use
    statistics.median over the non-None price values within the filter
    window.
    """
    with _db_session.session_scope() as db:
        q = (
            select(ProposalOutcome)
            .join(Proposal, Proposal.id == ProposalOutcome.proposal_id)
            .where(ProposalOutcome.outcome != ProposalOutcomeStatus.PENDING.value)
        )
        if service_line is not None:
            q = q.where(Proposal.service_line == service_line)
        if since is not None:
            q = q.where(ProposalOutcome.decided_at.is_not(None))
            q = q.where(ProposalOutcome.decided_at >= since)
        rows = list(db.execute(q).scalars().all())

        counts = {"won": 0, "lost": 0, "no_award": 0, "withdrawn": 0}
        awarded_prices: list[float] = []
        proposed_prices: list[float] = []
        for r in rows:
            key = _coerce_outcome_value(r.outcome)
            if key in counts:
                counts[key] += 1
            if r.awarded_price_usd is not None:
                awarded_prices.append(float(r.awarded_price_usd))
            if r.our_proposed_price_usd is not None:
                proposed_prices.append(float(r.our_proposed_price_usd))

    total = counts["won"] + counts["lost"] + counts["no_award"] + counts["withdrawn"]
    win_lose_total = counts["won"] + counts["lost"]
    if win_lose_total > 0:
        win_rate_pct = round(100.0 * counts["won"] / win_lose_total, 1)
    else:
        win_rate_pct = None

    return {
        "total": total,
        "won": counts["won"],
        "lost": counts["lost"],
        "no_award": counts["no_award"],
        "withdrawn": counts["withdrawn"],
        "win_rate_pct": win_rate_pct,
        "median_awarded_price_usd": (float(statistics.median(awarded_prices)) if awarded_prices else None),
        "median_our_proposed_price_usd": (
            float(statistics.median(proposed_prices)) if proposed_prices else None
        ),
    }
