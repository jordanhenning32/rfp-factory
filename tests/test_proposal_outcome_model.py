"""Tests for app/models/proposal_outcome.py::ProposalOutcome.

Verifies the ORM contract:
  - Round-trip via Proposal.outcome back-reference (1:1 relationship)
  - cascade="all, delete-orphan": deleting the Proposal removes its
    ProposalOutcome row at the SQLite layer (PRAGMA foreign_keys=ON
    enforces the FK CASCADE; the relationship cascade enforces the
    orphan side at the ORM layer).
  - unique=True on proposal_id: a second insert for the same proposal
    raises IntegrityError.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError


def test_proposal_outcome_round_trip_cascade_and_unique(inmemory_db):
    from app.core.enums import (
        ProposalOutcomeStatus,
        ProposalRole,
        ProposalStatus,
    )
    from app.db.session import session_scope
    from app.models import (
        Proposal,
        ProposalOutcome,
        RfpPackage,
    )

    # ── seed: RfpPackage + Proposal + ProposalOutcome ─────────────────
    with session_scope() as db:
        pkg = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://pkg",
        )
        db.add(pkg)
        db.flush()
        pkg_id = pkg.id

        proposal = Proposal(
            rfp_package_id=pkg_id,
            title="Outcome Round-trip Proposal",
            role=ProposalRole.PRIME,
            status=ProposalStatus.SUBMITTED,
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id

        outcome = ProposalOutcome(
            proposal_id=proposal_id,
            outcome=ProposalOutcomeStatus.WON.value,
            our_proposed_price_usd=100_000.00,
            awarded_price_usd=95_000.00,
            awarded_to="Quadratic Digital LLC",
            debrief_received=True,
            debrief_notes="Closed via debrief 2026-05-19.",
            factor_scores_json=[
                {
                    "factor_id": "F1",
                    "factor_name": "Technical Approach",
                    "our_score": 38,
                    "winning_score": 40,
                    "max_score": 40,
                    "notes": "Strong cloud-native fit.",
                },
            ],
        )
        db.add(outcome)

    # ── round-trip via Proposal.outcome (back-reference) ──────────────
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        assert p is not None
        assert p.outcome is not None
        oc = p.outcome
        # `outcome` column may come back as enum or string depending on
        # SA's enum hydration; assert via the string value either way.
        oc_value = oc.outcome.value if hasattr(oc.outcome, "value") else str(oc.outcome)
        assert oc_value == ProposalOutcomeStatus.WON.value
        assert float(oc.our_proposed_price_usd) == 100_000.00
        assert float(oc.awarded_price_usd) == 95_000.00
        assert oc.awarded_to == "Quadratic Digital LLC"
        assert oc.debrief_received is True
        assert isinstance(oc.factor_scores_json, list)
        assert oc.factor_scores_json[0]["factor_id"] == "F1"

    # ── unique constraint: second insert raises IntegrityError ────────
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            dup = ProposalOutcome(
                proposal_id=proposal_id,
                outcome=ProposalOutcomeStatus.LOST.value,
            )
            db.add(dup)
            db.flush()

    # ── cascade-on-delete: deleting Proposal removes its outcome ──────
    with session_scope() as db:
        # Re-fetch — the failed insert rolled back the session above.
        p = db.get(Proposal, proposal_id)
        assert p is not None
        db.delete(p)

    with session_scope() as db:
        remaining = db.query(ProposalOutcome).filter(ProposalOutcome.proposal_id == proposal_id).count()
        assert remaining == 0
