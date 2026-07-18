"""Tests for Section M persistence round-trip through the Proposal ORM row."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime


def _seed_proposal_with_package(db):
    """Create a minimal RfpPackage + Proposal fixture and return the Proposal."""
    from app.core.enums import ProposalRole, ProposalStatus
    from app.models import Proposal, RfpPackage

    pkg = RfpPackage(
        uploaded_by="pytest",
        uploaded_at=datetime.now(UTC),
        storage_dir="memory://rfp-package",
    )
    db.add(pkg)
    db.flush()

    proposal = Proposal(
        rfp_package_id=pkg.id,
        title="Synthetic Proposal",
        role=ProposalRole.PRIME,
        status=ProposalStatus.INTAKING,
    )
    db.add(proposal)
    db.flush()
    return proposal


def test_evaluation_criteria_round_trip_through_proposal_row(inmemory_db):
    """Persist a criteria dict as JSON, re-read it, assert equality."""
    from app.db.session import session_scope

    criteria = {
        "evaluation_method": "trade_off",
        "factors": [
            {
                "factor_id": "F1",
                "factor_name": "Technical Approach",
                "weight_pct": 40,
                "weight_descriptive": None,
                "scoring_scale": "Exceptional/Acceptable/Marginal/Unacceptable",
                "evidence_required": "Detailed narrative",
                "subfactors": [],
            },
            {
                "factor_id": "F2",
                "factor_name": "Past Performance",
                "weight_pct": 30,
                "weight_descriptive": None,
                "scoring_scale": None,
                "evidence_required": None,
                "subfactors": [],
            },
            {
                "factor_id": "F3",
                "factor_name": "Price",
                "weight_pct": 30,
                "weight_descriptive": None,
                "scoring_scale": None,
                "evidence_required": None,
                "subfactors": [],
            },
        ],
        "section_l_to_m_map": {"REQ-001": ["F1"]},
        "trade_off_language": "Best value to the government.",
        "lowest_price_clause": None,
        "extraction_notes": "Weights sum to 100%.",
    }

    # Write
    proposal_id: int
    with session_scope() as db:
        proposal = _seed_proposal_with_package(db)
        proposal.evaluation_criteria_json = json.dumps(criteria)
        proposal_id = proposal.id

    # Read back in a fresh session
    with session_scope() as db:
        from app.models import Proposal

        p = db.get(Proposal, proposal_id)
        raw = p.evaluation_criteria_json

    assert raw is not None
    loaded = json.loads(raw)
    assert loaded == criteria
    assert loaded["evaluation_method"] == "trade_off"
    assert len(loaded["factors"]) == 3
    assert loaded["section_l_to_m_map"]["REQ-001"] == ["F1"]


def test_load_evaluation_criteria_returns_none_when_not_yet_extracted(inmemory_db):
    """load_evaluation_criteria returns None when the column is NULL.

    Reloads the service module AFTER the inmemory_db monkeypatch is in place
    so that session_scope inside the service points to the in-memory engine —
    same pattern used by test_citation_check.py for citation_check.
    """
    import app.services.evaluation_criteria as ec_module

    # Reload forces re-import of session_scope from the now-patched db module
    ec_module = importlib.reload(ec_module)
    load_fn = ec_module.load_evaluation_criteria

    from app.db.session import session_scope

    with session_scope() as db:
        proposal = _seed_proposal_with_package(db)
        proposal_id = proposal.id
        # evaluation_criteria_json is NULL by default

    result = load_fn(proposal_id)
    assert result is None
