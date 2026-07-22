from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest


def _bind_services(monkeypatch):
    import app.db.session as db_session
    import app.services.payment_cost_review as payment_cost_review
    import app.services.review_freshness as review_freshness
    import app.services.submission_commitments as commitments
    import app.services.workflow as workflow

    monkeypatch.setattr(commitments, "session_scope", db_session.session_scope)
    monkeypatch.setattr(
        payment_cost_review, "session_scope", db_session.session_scope,
    )
    monkeypatch.setattr(
        review_freshness, "session_scope", db_session.session_scope,
    )
    monkeypatch.setattr(workflow, "session_scope", db_session.session_scope)
    return db_session, commitments, workflow


def _seed_package_and_proposal(db_session, *, status, service_line=None) -> int:
    from app.models import Proposal, RfpPackage

    with db_session.session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://workflow-package",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Workflow gate test",
            status=status,
            service_line=service_line,
        )
        db.add(proposal)
        db.flush()
        return proposal.id


def _add_review_coverage(db, *, proposal_id: int, section, now) -> None:
    """Seed truthful composite evidence for one current section revision."""
    from app.core.enums import AgentRunStatus
    from app.models import AgentRun
    from app.services.review_coverage import (
        REVIEW_COVERAGE_AGENT,
        review_coverage_prompt_version,
    )

    db.add(AgentRun(
        proposal_id=proposal_id,
        agent_name=REVIEW_COVERAGE_AGENT,
        prompt_version=review_coverage_prompt_version(
            section.id, section.current_revision_number or 0,
        ),
        started_at=now,
        completed_at=now,
        status=AgentRunStatus.COMPLETED,
    ))


def test_scope_signoff_requires_matrix_and_resolved_gaps(
    inmemory_db, monkeypatch,
) -> None:
    db_session, _commitments, workflow = _bind_services(monkeypatch)
    from app.core.enums import ProposalStatus
    from app.models import ComplianceMatrixItem, GapAnalysis, Proposal

    proposal_id = _seed_package_and_proposal(
        db_session, status=ProposalStatus.AWAITING_SCOPE_SIGNOFF,
    )

    empty = workflow.sign_off_scope(proposal_id)
    assert not empty["ok"]
    assert "empty" in empty["blockers"][0].lower()

    with db_session.session_scope() as db:
        item = ComplianceMatrixItem(
            proposal_id=proposal_id,
            requirement_id="REQ-001",
            requirement_text="The contractor shall provide support.",
            source_doc="rfp.pdf",
            requirement_type="shall",
            category="technical",
        )
        db.add(item)
        db.flush()
        gap = GapAnalysis(
            proposal_id=proposal_id,
            requirement_id_fk=item.id,
            gap_id="GAP-001",
            gap_severity="major",
            gap_description="Staffing evidence needed",
            resolved=False,
        )
        db.add(gap)
        db.flush()
        gap_id = gap.id

    unresolved = workflow.sign_off_scope(proposal_id)
    assert not unresolved["ok"]
    assert "1 remaining" in unresolved["blockers"][0]

    with db_session.session_scope() as db:
        db.get(GapAnalysis, gap_id).resolved = True

    assert workflow.sign_off_scope(proposal_id)["ok"]
    with db_session.session_scope() as db:
        assert db.get(Proposal, proposal_id).status == ProposalStatus.DRAFTING


@pytest.mark.parametrize(
    "review_status",
    ["pending", "extracting", "reviewing", "partial", "failed", "unexpected"],
)
def test_scope_signoff_blocks_incomplete_document_requirements_review(
    inmemory_db, monkeypatch, review_status,
) -> None:
    db_session, _commitments, workflow = _bind_services(monkeypatch)
    from app.core.enums import ProposalStatus
    from app.models import ComplianceMatrixItem, Proposal, RfpPackageDocument

    proposal_id = _seed_package_and_proposal(
        db_session, status=ProposalStatus.AWAITING_SCOPE_SIGNOFF,
    )
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        document = RfpPackageDocument(
            rfp_package_id=proposal.rfp_package_id,
            filename="requirements.pdf",
            storage_path="memory://requirements.pdf",
            structure_json={
                "requirements_review": {
                    "schema_version": 1,
                    "status": review_status,
                },
            },
        )
        db.add_all([
            document,
            ComplianceMatrixItem(
                proposal_id=proposal_id,
                requirement_id="REQ-001",
                requirement_text="The contractor shall provide support.",
                source_doc="requirements.pdf",
                requirement_type="shall",
                category="technical",
            ),
        ])

    result = workflow.sign_off_scope(proposal_id)

    assert not result["ok"]
    assert result["reason"] == "scope_incomplete"
    displayed_status = "unknown" if review_status == "unexpected" else review_status
    assert any(
        "requirements.pdf" in blocker and displayed_status in blocker
        for blocker in result["blockers"]
    )
    with db_session.session_scope() as db:
        assert db.get(Proposal, proposal_id).status == ProposalStatus.AWAITING_SCOPE_SIGNOFF


@pytest.mark.parametrize(
    "review_status",
    [None, "complete", "review_required", "degraded", "not_applicable"],
)
def test_scope_signoff_allows_legacy_or_terminal_document_review_state(
    inmemory_db, monkeypatch, review_status,
) -> None:
    db_session, _commitments, workflow = _bind_services(monkeypatch)
    from app.core.enums import ProposalStatus
    from app.models import ComplianceMatrixItem, Proposal, RfpPackageDocument

    proposal_id = _seed_package_and_proposal(
        db_session, status=ProposalStatus.AWAITING_SCOPE_SIGNOFF,
    )
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        structure_json = None
        if review_status is not None:
            structure_json = {
                "requirements_review": {
                    "schema_version": 1,
                    "status": review_status,
                },
            }
        db.add_all([
            RfpPackageDocument(
                rfp_package_id=proposal.rfp_package_id,
                filename="requirements.pdf",
                storage_path="memory://requirements.pdf",
                structure_json=structure_json,
            ),
            ComplianceMatrixItem(
                proposal_id=proposal_id,
                requirement_id="REQ-001",
                requirement_text="The contractor shall provide support.",
                source_doc="requirements.pdf",
                requirement_type="shall",
                category="technical",
            ),
        ])

    assert workflow.sign_off_scope(proposal_id)["ok"]


@pytest.mark.parametrize("malformed_review", [{}, None, "invalid", []])
def test_scope_signoff_blocks_present_but_malformed_review_state(
    inmemory_db, monkeypatch, malformed_review,
) -> None:
    db_session, _commitments, workflow = _bind_services(monkeypatch)
    from app.core.enums import ProposalStatus
    from app.models import ComplianceMatrixItem, Proposal, RfpPackageDocument

    proposal_id = _seed_package_and_proposal(
        db_session, status=ProposalStatus.AWAITING_SCOPE_SIGNOFF,
    )
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        db.add_all([
            RfpPackageDocument(
                rfp_package_id=proposal.rfp_package_id,
                filename="requirements.pdf",
                storage_path="memory://requirements.pdf",
                structure_json={"requirements_review": malformed_review},
            ),
            ComplianceMatrixItem(
                proposal_id=proposal_id,
                requirement_id="REQ-001",
                requirement_text="The contractor shall provide support.",
                source_doc="requirements.pdf",
                requirement_type="shall",
                category="technical",
            ),
        ])

    result = workflow.sign_off_scope(proposal_id)

    assert not result["ok"]
    assert result["reason"] == "scope_incomplete"
    assert any(
        "requirements.pdf" in blocker and "unknown" in blocker
        for blocker in result["blockers"]
    )


@pytest.mark.parametrize(
    "bad_details",
    [
        {"classification": "invalid"},
        {"extraction": {"coverage": "invalid"}},
        {"classification": {"reviewed_count": "not-a-number"}},
    ],
)
def test_scope_signoff_blocks_malformed_nested_review_details(
    inmemory_db, monkeypatch, bad_details,
) -> None:
    db_session, _commitments, workflow = _bind_services(monkeypatch)
    from app.core.enums import ProposalStatus
    from app.models import ComplianceMatrixItem, Proposal, RfpPackageDocument

    proposal_id = _seed_package_and_proposal(
        db_session, status=ProposalStatus.AWAITING_SCOPE_SIGNOFF,
    )
    review = {"schema_version": 1, "status": "complete", **bad_details}
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        db.add_all([
            RfpPackageDocument(
                rfp_package_id=proposal.rfp_package_id,
                filename="requirements.pdf",
                storage_path="memory://requirements.pdf",
                structure_json={"requirements_review": review},
            ),
            ComplianceMatrixItem(
                proposal_id=proposal_id,
                requirement_id="REQ-001",
                requirement_text="The contractor shall provide support.",
                source_doc="requirements.pdf",
                requirement_type="shall",
                category="technical",
            ),
        ])

    result = workflow.sign_off_scope(proposal_id)

    assert result["ok"] is False
    assert any(
        "requirements.pdf" in blocker and "unknown" in blocker
        for blocker in result["blockers"]
    )


def _make_payment_proposal_ready(db_session, proposal_id: int) -> None:
    from app.core.enums import AgentRunStatus
    from app.models import (
        AgentRun,
        Proposal,
        ProposalSection,
        ProposalTeamMember,
    )
    from app.services.payment_cost_review import (
        persist_payment_cost_review_data,
    )
    from app.services.review_freshness import (
        stamp_payment_market_scan_provenance,
    )

    now = datetime.now(UTC)
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        proposal.team_approved_at = now
        proposal.payment_market_scan_json = json.dumps(
            stamp_payment_market_scan_provenance({
                "pricing_structure": {
                    "pricing_model": "interchange_plus",
                },
            })
        )
        db.add(ProposalTeamMember(
            proposal_id=proposal_id,
            role_name="Program Manager",
            assigned_person="Alex Morgan",
            time_allocation_pct=100,
        ))
        technical_section = ProposalSection(
            proposal_id=proposal_id,
            section_id="SEC-001",
            section_title="Technical Approach",
            section_order=1,
            draft_text_markdown="Complete technical response.",
        )
        db.add_all([
            technical_section,
            ProposalSection(
                proposal_id=proposal_id,
                section_id="SEC-002",
                section_title="Cost Volume",
                section_order=2,
                requires_cost_analysis=True,
                draft_text_markdown="Complete payment fee narrative.",
            ),
        ])
        db.flush()
        _add_review_coverage(
            db,
            proposal_id=proposal_id,
            section=technical_section,
            now=now,
        )
        db.add(AgentRun(
            proposal_id=proposal_id,
            agent_name="reviewer_a",
            model_used="fixture",
            status=AgentRunStatus.COMPLETED,
            started_at=now,
            completed_at=now,
        ))
    assert persist_payment_cost_review_data(
        proposal_id,
        {"findings": [], "bid_ready": True},
    )


def test_submission_gate_supports_payment_flow_and_blocks_commitments(
    inmemory_db, monkeypatch,
) -> None:
    db_session, commitments, workflow = _bind_services(monkeypatch)
    from app.core.enums import ProposalStatus
    from app.models import Proposal, SubmissionCommitment

    proposal_id = _seed_package_and_proposal(
        db_session,
        status=ProposalStatus.DRAFT_READY,
        service_line="payment_systems",
    )
    _make_payment_proposal_ready(db_session, proposal_id)

    readiness = commitments.evaluate_submission_readiness(proposal_id)
    assert readiness["ready"], readiness["blockers"]

    with db_session.session_scope() as db:
        db.add(SubmissionCommitment(
            proposal_id=proposal_id,
            description="Attach the implementation diagram",
            obtained=False,
        ))

    blocked = workflow.approve_for_submission(proposal_id)
    assert not blocked["ok"]
    assert any("implementation diagram" in b for b in blocked["blockers"])
    with db_session.session_scope() as db:
        assert db.get(Proposal, proposal_id).status == ProposalStatus.DRAFT_READY
        commitment = db.query(SubmissionCommitment).one()
        commitment.obtained = True

    assert workflow.approve_for_submission(proposal_id)["ok"]
    assert workflow.mark_submitted(proposal_id)["ok"]
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal.status == ProposalStatus.SUBMITTED
        assert proposal.submitted_at is not None


def test_approval_requires_finished_workflow_status(
    inmemory_db, monkeypatch,
) -> None:
    db_session, _commitments, workflow = _bind_services(monkeypatch)
    from app.core.enums import ProposalStatus
    from app.models import Proposal

    proposal_id = _seed_package_and_proposal(
        db_session,
        status=ProposalStatus.AWAITING_TEAM_APPROVAL,
        service_line="payment_systems",
    )
    _make_payment_proposal_ready(db_session, proposal_id)

    result = workflow.approve_for_submission(proposal_id)
    assert not result["ok"]
    assert result["reason"] == "invalid_status"
    with db_session.session_scope() as db:
        assert db.get(Proposal, proposal_id).status == ProposalStatus.AWAITING_TEAM_APPROVAL


def test_clean_it_cost_review_is_recognized_from_completed_agent_run(
    inmemory_db, monkeypatch,
) -> None:
    db_session, commitments, _workflow = _bind_services(monkeypatch)
    from app.core.enums import AgentRunStatus, ProposalStatus
    from app.models import (
        AgentRun,
        PricingPackage,
        Proposal,
        ProposalSection,
        ProposalTeamMember,
    )

    proposal_id = _seed_package_and_proposal(
        db_session,
        status=ProposalStatus.DRAFT_READY,
        service_line="it_services",
    )
    now = datetime.now(UTC)
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        proposal.team_approved_at = now
        db.add(ProposalTeamMember(
            proposal_id=proposal_id,
            role_name="Program Manager",
            assigned_person="Alex Morgan",
            time_allocation_pct=100,
        ))
        technical_section = ProposalSection(
            proposal_id=proposal_id,
            section_id="SEC-001",
            section_title="Technical Approach",
            section_order=1,
            draft_text_markdown="Complete response.",
        )
        db.add(technical_section)
        db.add_all([
            PricingPackage(proposal_id=proposal_id, scenario=scenario)
            for scenario in ("LOW", "MEDIUM", "HIGH")
        ])
        db.add(AgentRun(
            proposal_id=proposal_id,
            agent_name="reviewer_a",
            model_used="fixture",
            status=AgentRunStatus.COMPLETED,
            started_at=now,
            completed_at=now,
        ))
        db.flush()
        _add_review_coverage(
            db,
            proposal_id=proposal_id,
            section=technical_section,
            now=now,
        )

    before = commitments.evaluate_submission_readiness(proposal_id)
    assert not before["ready"]
    assert any("Cost Reviewer hasn't completed" in b for b in before["blockers"])

    with db_session.session_scope() as db:
        db.add(AgentRun(
            proposal_id=proposal_id,
            agent_name="cost_reviewer:fixture",
            model_used="fixture",
            status=AgentRunStatus.COMPLETED,
            started_at=now,
            completed_at=now,
        ))

    after = commitments.evaluate_submission_readiness(proposal_id)
    assert after["ready"], after["blockers"]


def test_archive_preserves_submitted_record_and_rejects_earlier_status(
    inmemory_db, monkeypatch,
) -> None:
    db_session, _commitments, workflow = _bind_services(monkeypatch)
    from app.core.enums import ProposalStatus
    from app.models import Proposal

    submitted_id = _seed_package_and_proposal(
        db_session, status=ProposalStatus.SUBMITTED,
    )
    not_submitted_id = _seed_package_and_proposal(
        db_session, status=ProposalStatus.DRAFT_READY,
    )

    blocked = workflow.archive_proposal(not_submitted_id)
    assert not blocked["ok"]
    assert blocked["reason"] == "invalid_status"

    archived = workflow.archive_proposal(submitted_id)
    assert archived["ok"]
    with db_session.session_scope() as db:
        assert db.get(Proposal, submitted_id).status == ProposalStatus.ARCHIVED
        assert db.get(Proposal, not_submitted_id).status == ProposalStatus.DRAFT_READY
