from __future__ import annotations

from datetime import UTC, datetime


def test_findings_badge_counts_pending_and_accepted_unresolved(
    inmemory_db, monkeypatch,
) -> None:
    """Accepted findings remain actions until a later review resolves them."""
    import app.db.session as db_session
    import app.ui.pages as pages
    from app.core.enums import ProposalStatus
    from app.models import Proposal, ProposalSection, ReviewerFinding, RfpPackage

    # pages.py binds SessionLocal at import time, so point that local name at
    # the isolated session factory installed by the inmemory_db fixture.
    monkeypatch.setattr(pages, "SessionLocal", db_session.SessionLocal)

    now = datetime.now(UTC)
    with db_session.session_scope() as db:
        package = RfpPackage(
            uploaded_at=now,
            storage_dir="memory://ui-findings-badge",
        )
        db.add(package)
        db.flush()

        proposal = Proposal(
            rfp_package_id=package.id,
            title="Findings badge regression",
            status=ProposalStatus.DRAFT_READY,
        )
        db.add(proposal)
        db.flush()

        section = ProposalSection(
            proposal_id=proposal.id,
            section_id="SEC-001",
            section_title="Technical Approach",
            section_order=1,
        )
        db.add(section)
        db.flush()

        common = {
            "proposal_section_id": section.id,
            "reviewer_agent": "A",
            "pass_number": 1,
            "severity": "MAJOR",
            "category": "compliance_gap",
            "suggested_fix": "Fix it.",
        }
        db.add_all(
            [
                ReviewerFinding(
                    **common,
                    finding_text="Pending finding",
                ),
                ReviewerFinding(
                    **common,
                    finding_text="Accepted but not applied",
                    accepted_at=now,
                ),
                ReviewerFinding(
                    **common,
                    finding_text="Dismissed finding",
                    dismissed_at=now,
                    dismissed_reason="Not applicable",
                ),
                ReviewerFinding(
                    **common,
                    finding_text="Accepted and resolved finding",
                    accepted_at=now,
                    resolved_in_pass_number=2,
                ),
            ]
        )
        proposal_id = proposal.id

    badges = pages._compute_tab_badges(proposal_id)

    assert badges["findings"] == 2
