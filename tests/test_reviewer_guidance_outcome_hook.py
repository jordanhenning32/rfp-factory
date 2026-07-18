"""Tests for app/services/lessons.py::format_reviewer_guidance — the
outcome-correlation block (pipeline 3 win/loss ledger feedback hook).

LOAD-BEARING CONTRACT (this is the test that locks it down):

  1. When proposal_outcomes data exists AND a category has >= 5
     observations across WON+LOST proposals AND >= 70% are LOST, the
     returned string contains the OUTCOME CORRELATION block with the
     category label and the LOST/WON counts.

  2. When ZERO ProposalOutcome rows exist, the returned string does NOT
     contain "OUTCOME CORRELATION" — the existing dismiss-rate block is
     unbroken AND byte-identical to its prior shape.

Both tests use the existing `inmemory_db` fixture and exercise real
code paths (no mocking the unit under test, no LLM stubs — the hook is
pure SQL + python).
"""

from __future__ import annotations

from datetime import UTC, datetime


def _seed_proposal_with_outcome(
    db,
    *,
    title: str,
    outcome_value: str | None,
):
    """Helper: create a Proposal + ProposalOutcome (when outcome_value is
    not None). Returns the proposal id."""
    from app.core.enums import ProposalRole, ProposalStatus
    from app.models import Proposal, ProposalOutcome, RfpPackage

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
    )
    db.add(proposal)
    db.flush()

    if outcome_value is not None:
        db.add(
            ProposalOutcome(
                proposal_id=proposal.id,
                outcome=outcome_value,
                decided_at=datetime.now(UTC),
            )
        )
        db.flush()

    return proposal.id


def _seed_section_with_finding(
    db,
    *,
    proposal_id: int,
    section_id: str,
    category_value: str,
):
    """Helper: create a ProposalSection + one ReviewerFinding on it."""
    from app.core.enums import FindingSeverity, ReviewerAgent
    from app.models import ProposalSection, ReviewerFinding

    sec = ProposalSection(
        proposal_id=proposal_id,
        section_id=section_id,
        section_title=f"Section {section_id}",
    )
    db.add(sec)
    db.flush()

    db.add(
        ReviewerFinding(
            proposal_section_id=sec.id,
            reviewer_agent=ReviewerAgent.A_COMPLIANCE_RISK.value,
            pass_number=1,
            severity=FindingSeverity.MAJOR.value,
            category=category_value,
            finding_text="seed finding",
            suggested_fix="seed suggested fix",
        )
    )


def _repatch_lessons_session_scope(monkeypatch):
    """Force `app.services.lessons.session_scope` to re-bind to whatever
    the current `app.db.session.session_scope` is.

    The inmemory_db fixture monkey-patches the symbol on `app.db.session`,
    but lessons.py imports the function by name (`from app.db.session
    import session_scope`), which binds it at module-import time. If
    lessons.py was already imported in a previous test, the rebinding
    on `app.db.session` does not propagate. This helper forces a fresh
    bind so the hook reads the current test's in-memory DB.
    """
    import app.db.session as _db_session
    import app.services.lessons as _lessons

    monkeypatch.setattr(_lessons, "session_scope", _db_session.session_scope)


def test_outcome_correlation_block_present_when_data_correlates(inmemory_db, monkeypatch):
    """Seed 8 LOST + 2 WON proposals, each with one
    `overcommitment`-category finding. Assert the OUTCOME CORRELATION
    block appears in format_reviewer_guidance output."""
    _repatch_lessons_session_scope(monkeypatch)

    from app.core.enums import FindingCategory, ProposalOutcomeStatus
    from app.db.session import session_scope
    from app.services.lessons import format_reviewer_guidance

    cat_value = FindingCategory.OVERCOMMITMENT.value

    with session_scope() as db:
        # 8 LOST proposals, each with one overcommitment finding.
        for i in range(8):
            pid = _seed_proposal_with_outcome(
                db,
                title=f"lost-{i}",
                outcome_value=ProposalOutcomeStatus.LOST.value,
            )
            _seed_section_with_finding(
                db,
                proposal_id=pid,
                section_id=f"S-lost-{i}",
                category_value=cat_value,
            )
        # 2 WON proposals, each with one overcommitment finding.
        for i in range(2):
            pid = _seed_proposal_with_outcome(
                db,
                title=f"won-{i}",
                outcome_value=ProposalOutcomeStatus.WON.value,
            )
            _seed_section_with_finding(
                db,
                proposal_id=pid,
                section_id=f"S-won-{i}",
                category_value=cat_value,
            )

    out = format_reviewer_guidance(
        reviewer="A",
        categories=[cat_value],
    )

    # The block exists, is labeled, and reports the correlation counts.
    assert "OUTCOME CORRELATION" in out
    assert "overcommitment" in out
    assert "8 LOST" in out
    assert "2 WON" in out


def test_zero_outcomes_returns_existing_dismiss_rate_only(inmemory_db, monkeypatch):
    """Seed ONLY ReviewerFinding rows with enough user actions (accept
    + dismiss) to trigger the existing dismiss-rate block, with ZERO
    ProposalOutcome rows. Assert OUTCOME CORRELATION is absent AND the
    existing dismiss-rate block ("USER FEEDBACK CALIBRATION") is intact.
    """
    _repatch_lessons_session_scope(monkeypatch)

    from app.core.enums import (
        FindingCategory,
        FindingSeverity,
        ProposalRole,
        ProposalStatus,
        ReviewerAgent,
    )
    from app.db.session import session_scope
    from app.models import (
        Proposal,
        ProposalSection,
        ReviewerFinding,
        RfpPackage,
    )
    from app.services.lessons import format_reviewer_guidance

    cat_value = FindingCategory.OVERCOMMITMENT.value

    with session_scope() as db:
        pkg = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://pkg-dismiss-only",
        )
        db.add(pkg)
        db.flush()

        proposal = Proposal(
            rfp_package_id=pkg.id,
            title="Dismiss-Rate Only",
            role=ProposalRole.PRIME,
            status=ProposalStatus.SUBMITTED,
        )
        db.add(proposal)
        db.flush()

        sec = ProposalSection(
            proposal_id=proposal.id,
            section_id="S-only",
            section_title="Only Section",
        )
        db.add(sec)
        db.flush()

        # Seed 5 dismissed + 5 accepted findings on overcommitment so the
        # dismiss-rate block clears its threshold. The auto-loop
        # `resolved_in_pass_number` exclusion in get_category_action_rates
        # means accepts here must NOT carry a resolved_in_pass_number.
        now = datetime.now(UTC)
        for i in range(5):
            db.add(
                ReviewerFinding(
                    proposal_section_id=sec.id,
                    reviewer_agent=ReviewerAgent.A_COMPLIANCE_RISK.value,
                    pass_number=1,
                    severity=FindingSeverity.MAJOR.value,
                    category=cat_value,
                    finding_text=f"dismissed {i}",
                    suggested_fix="x",
                    dismissed_at=now,
                    dismissed_reason="false positive",
                )
            )
        for i in range(5):
            db.add(
                ReviewerFinding(
                    proposal_section_id=sec.id,
                    reviewer_agent=ReviewerAgent.A_COMPLIANCE_RISK.value,
                    pass_number=1,
                    severity=FindingSeverity.MAJOR.value,
                    category=cat_value,
                    finding_text=f"accepted {i}",
                    suggested_fix="x",
                    accepted_at=now,
                    # resolved_in_pass_number left NULL — counts as user accept
                )
            )

    out = format_reviewer_guidance(
        reviewer="A",
        categories=[cat_value],
    )

    # Substring smoke checks — the dismiss-rate block is present; the
    # outcome-correlation block is absent.
    assert "USER FEEDBACK CALIBRATION" in out
    assert "OUTCOME CORRELATION" not in out

    # Byte-identical contract: when zero ProposalOutcome rows exist,
    # the extension's output MUST be exactly what the dismiss-rate-only
    # path produced before the hook was added. Compare to the direct
    # dismiss-rate helper output. Any future regression that mutates
    # the dismiss-rate wording, ordering, or whitespace will fail here.
    from app.services.lessons import _build_category_calibration_block

    expected_dismiss_block = _build_category_calibration_block(
        categories=[cat_value],
    )
    assert out == "\n\n" + expected_dismiss_block
