"""Tests for app/services/proposal_outcomes.py.

Exercises real code paths against the in-memory DB — no mocks of the
unit under test. Three test functions:

  1. test_upsert_outcome_insert_then_update_preserves_existing
     — insert WON @ 100k; second call with our_proposed_price_usd=None
       AND awarded_price_usd=95k must preserve the original 100k AND
       set awarded_price_usd=95k AND preserve original debrief_notes.

  2. test_get_outcome_returns_none_and_then_row
     — None before any upsert; correct shape after upsert.

  3. test_get_win_rate_summary_against_synthetic_data
     — 3 WON + 2 LOST it_services, 1 NO_AWARD payment_systems, 1 PENDING:
       * unfiltered: total=6 (PENDING excluded), win_rate_pct=60.0
       * service_line=it_services: total=5, win_rate_pct=60.0
       * since cutoff: respects decided_at filter
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _seed_proposal(db, *, title: str, service_line: str | None = None):
    """Helper: create RfpPackage + Proposal, return proposal_id."""
    from app.core.enums import ProposalRole, ProposalStatus
    from app.models import Proposal, RfpPackage

    pkg = RfpPackage(
        uploaded_by="pytest",
        uploaded_at=datetime.now(UTC),
        storage_dir=f"memory://pkg-{title}",
    )
    db.add(pkg)
    db.flush()

    proposal = Proposal(
        rfp_package_id=pkg.id,
        title=title,
        role=ProposalRole.PRIME,
        status=ProposalStatus.SUBMITTED,
        service_line=service_line,
    )
    db.add(proposal)
    db.flush()
    return proposal.id


def test_upsert_outcome_insert_then_update_preserves_existing(inmemory_db):
    from app.core.enums import ProposalOutcomeStatus
    from app.db.session import session_scope
    from app.services.proposal_outcomes import get_outcome, upsert_outcome

    with session_scope() as db:
        pid = _seed_proposal(db, title="Upsert Test")

    # Initial insert: WON, 100k proposed, debrief notes set.
    row1 = upsert_outcome(
        proposal_id=pid,
        outcome=ProposalOutcomeStatus.WON,
        our_proposed_price_usd=100_000.00,
        debrief_notes="initial debrief notes",
    )
    assert row1.id is not None
    assert float(row1.our_proposed_price_usd) == 100_000.00
    assert row1.debrief_notes == "initial debrief notes"

    # Update: same outcome, set awarded_price_usd=95k, leave proposed
    # price + debrief_notes as None (None must preserve existing).
    row2 = upsert_outcome(
        proposal_id=pid,
        outcome=ProposalOutcomeStatus.WON,
        awarded_price_usd=95_000.00,
        debrief_notes=None,
    )
    # Same row id (upsert, not insert).
    assert row2.id == row1.id
    assert float(row2.awarded_price_usd) == 95_000.00
    # PRESERVED: original our_proposed_price_usd from row1.
    assert float(row2.our_proposed_price_usd) == 100_000.00
    # PRESERVED: original debrief_notes.
    assert row2.debrief_notes == "initial debrief notes"

    # Re-read via get_outcome to confirm persistence.
    row3 = get_outcome(pid)
    assert row3 is not None
    assert float(row3.our_proposed_price_usd) == 100_000.00
    assert float(row3.awarded_price_usd) == 95_000.00
    assert row3.debrief_notes == "initial debrief notes"


def test_get_outcome_returns_none_and_then_row(inmemory_db):
    from app.core.enums import ProposalOutcomeStatus
    from app.db.session import session_scope
    from app.services.proposal_outcomes import get_outcome, upsert_outcome

    with session_scope() as db:
        pid = _seed_proposal(db, title="Get Outcome Test")

    # Before any upsert.
    assert get_outcome(pid) is None

    # After upsert.
    upsert_outcome(
        proposal_id=pid,
        outcome=ProposalOutcomeStatus.LOST,
        debrief_notes="post-mortem stub",
    )
    row = get_outcome(pid)
    assert row is not None
    value = row.outcome.value if hasattr(row.outcome, "value") else str(row.outcome)
    assert value == ProposalOutcomeStatus.LOST.value
    assert row.debrief_notes == "post-mortem stub"


def test_get_win_rate_summary_against_synthetic_data(inmemory_db):
    from app.core.enums import ProposalOutcomeStatus
    from app.db.session import session_scope
    from app.services.proposal_outcomes import (
        get_win_rate_summary,
        list_outcomes_for_learning,
        upsert_outcome,
    )

    now = datetime.now(UTC)
    older = now - timedelta(days=400)
    recent = now - timedelta(days=30)

    # 3 WON it_services + 2 LOST it_services + 1 NO_AWARD payment_systems + 1 PENDING.
    won_specs = [
        ("won1", "it_services", 100_000.00, 95_000.00, recent),
        ("won2", "it_services", 150_000.00, 140_000.00, recent),
        ("won3", "it_services", 200_000.00, 190_000.00, older),
    ]
    lost_specs = [
        ("lost1", "it_services", 110_000.00, None, recent),
        ("lost2", "it_services", 130_000.00, None, recent),
    ]
    other_specs = [
        ("no_award1", "payment_systems", 75_000.00, None, recent, ProposalOutcomeStatus.NO_AWARD),
        ("pending1", "it_services", 50_000.00, None, None, ProposalOutcomeStatus.PENDING),
    ]

    with session_scope() as db:
        pids: dict[str, int] = {}
        for title, sl, *_ in won_specs + lost_specs + other_specs:
            pids[title] = _seed_proposal(db, title=title, service_line=sl)

    for title, _sl, proposed, awarded, decided in won_specs:
        upsert_outcome(
            proposal_id=pids[title],
            outcome=ProposalOutcomeStatus.WON,
            our_proposed_price_usd=proposed,
            awarded_price_usd=awarded,
            decided_at=decided,
        )
    for title, _sl, proposed, awarded, decided in lost_specs:
        upsert_outcome(
            proposal_id=pids[title],
            outcome=ProposalOutcomeStatus.LOST,
            our_proposed_price_usd=proposed,
            awarded_price_usd=awarded,
            decided_at=decided,
        )
    for title, _sl, proposed, awarded, decided, status in other_specs:
        upsert_outcome(
            proposal_id=pids[title],
            outcome=status,
            our_proposed_price_usd=proposed,
            awarded_price_usd=awarded,
            decided_at=decided,
        )

    # ── unfiltered rollup ─────────────────────────────────────────────
    summary = get_win_rate_summary()
    assert summary["total"] == 6  # PENDING excluded
    assert summary["won"] == 3
    assert summary["lost"] == 2
    assert summary["no_award"] == 1
    assert summary["withdrawn"] == 0
    # Denominator = won + lost = 5; win_rate = 3 / 5 = 60.0
    assert summary["win_rate_pct"] == 60.0
    # Median over the 3 awarded prices (95k, 140k, 190k) -> 140k
    assert summary["median_awarded_price_usd"] == 140_000.00

    # ── service_line filter ───────────────────────────────────────────
    s_it = get_win_rate_summary(service_line="it_services")
    assert s_it["total"] == 5  # 3 WON + 2 LOST (PENDING excluded, no_award is payment)
    assert s_it["won"] == 3
    assert s_it["lost"] == 2
    assert s_it["no_award"] == 0
    assert s_it["win_rate_pct"] == 60.0

    s_payment = get_win_rate_summary(service_line="payment_systems")
    assert s_payment["total"] == 1
    assert s_payment["no_award"] == 1
    # win + lose == 0 -> win_rate_pct is None
    assert s_payment["win_rate_pct"] is None

    # ── since cutoff (recent only) ────────────────────────────────────
    cutoff = now - timedelta(days=200)
    s_recent = get_win_rate_summary(since=cutoff)
    # Excludes won3 (400 days ago) and pending1 (no decided_at).
    # Kept: won1, won2, lost1, lost2, no_award1 = 5 rows.
    assert s_recent["total"] == 5
    assert s_recent["won"] == 2
    assert s_recent["lost"] == 2
    assert s_recent["no_award"] == 1

    # ── list_outcomes_for_learning sanity ─────────────────────────────
    all_outcomes = list_outcomes_for_learning()
    # Excludes PENDING. 3 WON + 2 LOST + 1 NO_AWARD = 6 rows.
    assert len(all_outcomes) == 6
    it_outcomes = list_outcomes_for_learning(service_line="it_services")
    # 3 WON + 2 LOST it_services = 5 rows.
    assert len(it_outcomes) == 5
