from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session


def _bind_service_sessions(monkeypatch):
    """Point modules that bind session_scope at import time at the test DB."""
    import app.db.session as db_session
    import app.services.cost_reviewer as cost_reviewer
    import app.services.evaluation_criteria as evaluation_criteria
    import app.services.findings as findings
    import app.services.framing as framing
    import app.services.market_scan as market_scan
    import app.services.needs_human as needs_human
    import app.services.payment_cost_review as payment_cost_review
    import app.services.polish as polish
    import app.services.pricing as pricing
    import app.services.sections as sections
    import app.services.service_line as service_line
    import app.services.stages as stages
    import app.services.submission_commitments as commitments
    import app.services.team as team
    import app.services.timeline as timeline

    modules = (
        cost_reviewer,
        evaluation_criteria,
        findings,
        framing,
        market_scan,
        needs_human,
        payment_cost_review,
        polish,
        pricing,
        sections,
        service_line,
        stages,
        commitments,
        team,
        timeline,
    )
    for module in modules:
        monkeypatch.setattr(module, "session_scope", db_session.session_scope)
    return {
        module.__name__.rsplit(".", 1)[-1]: module
        for module in modules
    }


def _seed_proposal_graph(db_session, *, archived: bool) -> dict[str, int]:
    from app.core.enums import ProposalStatus
    from app.models import (
        ComplianceMatrixItem,
        CostReviewFinding,
        GapAnalysis,
        PricingPackage,
        Proposal,
        ProposalSection,
        ProposalTeamMember,
        ReviewerFinding,
        RfpPackage,
        SubmissionCommitment,
    )

    with db_session.session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://archive-immutability",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Immutable archive",
            status=ProposalStatus.DRAFT_READY,
            service_line="payment_systems",
            proposed_scenario="MEDIUM",
            selected_pricing_model="flat_rate",
            timeline_json='{"anchor_date": null, "phases": []}',
            payment_cost_review_findings_json=(
                '{"findings": [{"finding_id": "PCR-1", '
                '"user_action": "pending", "user_note": null}]}'
            ),
        )
        db.add(proposal)
        db.flush()

        section = ProposalSection(
            proposal_id=proposal.id,
            section_id="SEC-001",
            section_title="Technical approach",
            section_order=1,
            draft_text_markdown="Original [NEEDS_HUMAN: owner] text.",
            current_revision_number=3,
            needs_human_placeholders_json=[{
                "marker_text": "owner",
                "description": "Name the owner",
                "category": "personnel",
            }],
        )
        db.add(section)
        db.flush()

        item = ComplianceMatrixItem(
            proposal_id=proposal.id,
            requirement_id="REQ-001",
            requirement_text="Attach the signed certification.",
            source_doc="rfp.pdf",
            requirement_type="mandatory_form",
            category="certification",
        )
        db.add(item)
        db.flush()
        gap = GapAnalysis(
            proposal_id=proposal.id,
            requirement_id_fk=item.id,
            gap_id="GAP-001",
            gap_severity="major",
            gap_description="Evidence is missing.",
            mitigation_options_json=[{"approach": "self-perform"}],
        )
        db.add(gap)

        member = ProposalTeamMember(
            proposal_id=proposal.id,
            role_name="Program Manager",
            assigned_person="Alex Morgan",
            time_allocation_pct=50,
        )
        commitment = SubmissionCommitment(
            proposal_id=proposal.id,
            description="Attach transition plan",
        )
        db.add_all([member, commitment])

        reviewer_finding = ReviewerFinding(
            proposal_section_id=section.id,
            reviewer_agent="A",
            pass_number=1,
            severity="MAJOR",
            category="weak_persuasion",
            finding_text="Add proof.",
        )
        pricing_package = PricingPackage(
            proposal_id=proposal.id,
            scenario="MEDIUM",
        )
        db.add_all([reviewer_finding, pricing_package])
        db.flush()
        cost_finding = CostReviewFinding(
            pricing_package_id=pricing_package.id,
            finding_text="Check the fee.",
            severity="MAJOR",
            category="MARGIN",
        )
        db.add(cost_finding)
        db.flush()

        ids = {
            "proposal": proposal.id,
            "package": package.id,
            "section": section.id,
            "item": item.id,
            "gap": gap.id,
            "member": member.id,
            "commitment": commitment.id,
            "reviewer_finding": reviewer_finding.id,
            "cost_finding": cost_finding.id,
        }
        if archived:
            proposal.status = ProposalStatus.ARCHIVED
        return ids


def test_archived_proposal_rejects_delete_without_touching_record(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    from app.models import Proposal, RfpPackage
    from app.services import proposals

    ids = _seed_proposal_graph(db_session, archived=True)

    with Session(inmemory_db) as db:
        result = proposals.delete_proposal(db, ids["proposal"])
        assert result == {
            "deleted": False,
            "reason": "archived proposals are read-only and cannot be deleted",
        }
        assert db.get(Proposal, ids["proposal"]) is not None
        assert db.get(RfpPackage, ids["package"]) is not None


def test_archived_proposal_rejects_tab_and_service_mutations_but_allows_export(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.services.amendments as amendments
    import app.services.proposal_outcomes as outcomes
    import app.services.win_strategy as win_strategy
    from app.core.enums import ProposalOutcomeStatus
    from app.models import (
        AgentRun,
        ComplianceMatrixItem,
        CostReviewFinding,
        GapAnalysis,
        Proposal,
        ProposalOutcome,
        ProposalSection,
        ProposalTeamMember,
        ReviewerFinding,
        SubmissionCommitment,
    )
    from app.services.export import compile_proposal_to_docx
    from app.services.proposal_access import ArchivedProposalError
    from app.services.proposals import UploadedFile

    services = _bind_service_sessions(monkeypatch)
    ids = _seed_proposal_graph(db_session, archived=False)

    # Outcome/debrief capture is valid before archive. Once archived, this
    # row becomes part of the immutable proposal record too.
    outcomes.upsert_outcome(
        proposal_id=ids["proposal"],
        outcome=ProposalOutcomeStatus.PENDING,
        debrief_notes="Captured before archive",
    )
    with db_session.session_scope() as db:
        db.get(Proposal, ids["proposal"]).status = "archived"

    def attach_amendment() -> None:
        with Session(inmemory_db) as db:
            amendments.attach_amendment_to_proposal(
                proposal_id=ids["proposal"],
                files=[UploadedFile("amendment.pdf", b"amendment")],
                document_role="amendment",
                sequence_number=1,
                db=db,
            )

    mutations = [
        ("manual draft edit", lambda: services["sections"].save_manual_edit(
            ids["section"], "Changed text",
        )),
        ("outline replacement", lambda: services["sections"].replace_outline(
            ids["proposal"], [],
        )),
        ("placeholder resolution", lambda: services["needs_human"].resolve_placeholder(
            proposal_section_pk=ids["section"],
            marker_text="owner",
            kind="edit",
            value="Taylor",
        )),
        ("gap decision", lambda: services["framing"].update_gap_resolution(
            ids["gap"], resolved=True,
        )),
        ("team edit", lambda: services["team"].update_team_member(
            ids["member"], {"role_name": "Changed"},
        )),
        ("commitment edit", lambda: services["submission_commitments"].update_commitment(
            ids["commitment"], description="Changed",
        )),
        ("checklist toggle", lambda: services["submission_commitments"].set_rfp_required_item_obtained(
            ids["item"], True,
        )),
        ("timeline edit", lambda: services["timeline"].set_anchor_date(
            ids["proposal"], "2035-01-01",
        )),
        ("review finding triage", lambda: services["findings"].accept_finding(
            ids["reviewer_finding"],
        )),
        ("cost finding triage", lambda: services["cost_reviewer"].update_cost_review_finding_action(
            finding_ids=[ids["cost_finding"]], user_action="accepted",
        )),
        ("payment finding triage", lambda: services["payment_cost_review"].update_payment_finding_action(
            ids["proposal"], "PCR-1", action="accepted",
        )),
        ("pricing scenario", lambda: services["pricing"].set_proposed_scenario(
            ids["proposal"], "LOW",
        )),
        ("pricing replacement", lambda: services["pricing"].upsert_pricing_packages(
            proposal_id=ids["proposal"], packages=[],
            market_scan_id=None, agent_run_id=None,
        )),
        ("service line", lambda: services["service_line"].set_service_line(
            ids["proposal"], "it_services",
        )),
        ("payment pricing model", lambda: services["service_line"].set_selected_pricing_model(
            ids["proposal"], "tiered",
        )),
        ("market research replacement", lambda: services["market_scan"].upsert_market_scan(
            proposal_id=ids["proposal"], result=None,
        )),
        ("evaluation criteria extraction", lambda: services["evaluation_criteria"].extract_and_persist_evaluation_criteria(
            ids["proposal"],
        )),
        ("amendment attachment", attach_amendment),
        ("win strategy", lambda: win_strategy._persist(
            ids["proposal"], "win_themes", {"themes": []},
        )),
        ("outcome/debrief edit", lambda: outcomes.upsert_outcome(
            proposal_id=ids["proposal"],
            outcome=ProposalOutcomeStatus.WON,
            debrief_notes="Late mutation",
        )),
        ("polish audit write", lambda: services["polish"].record_polish_edit(
            proposal_id=ids["proposal"],
            proposal_section_id=ids["section"],
            section_id_label="SEC-001",
            issue_type="VOICE",
            severity="MINOR",
            edit_summary="Changed",
            rationale=None,
            problematic_text=None,
            suggested_fix=None,
            applied_at=datetime.now(UTC),
            applied_in_run_at=datetime.now(UTC),
            cost_usd=0,
        )),
    ]
    for _label, mutation in mutations:
        with pytest.raises(ArchivedProposalError, match="archived and read-only"):
            mutation()

    # Best-effort stage logging intentionally swallows persistence errors, but
    # the archive guard must still prevent a new audit row.
    services["stages"].record_stage(ids["proposal"], "stale background write")

    with db_session.session_scope() as db:
        proposal = db.get(Proposal, ids["proposal"])
        section = db.get(ProposalSection, ids["section"])
        assert proposal.status == "archived"
        assert proposal.service_line == "payment_systems"
        assert proposal.proposed_scenario == "MEDIUM"
        assert proposal.selected_pricing_model == "flat_rate"
        assert proposal.timeline_json == '{"anchor_date": null, "phases": []}'
        assert section.draft_text_markdown == "Original [NEEDS_HUMAN: owner] text."
        assert section.current_revision_number == 3
        assert not db.get(GapAnalysis, ids["gap"]).resolved
        assert db.get(ProposalTeamMember, ids["member"]).role_name == "Program Manager"
        assert db.get(SubmissionCommitment, ids["commitment"]).description == "Attach transition plan"
        assert not db.get(ComplianceMatrixItem, ids["item"]).submission_obtained
        assert db.get(ReviewerFinding, ids["reviewer_finding"]).accepted_at is None
        assert db.get(CostReviewFinding, ids["cost_finding"]).user_action == "pending"
        outcome = db.query(ProposalOutcome).filter_by(
            proposal_id=ids["proposal"],
        ).one()
        assert outcome.outcome == "pending"
        assert outcome.debrief_notes == "Captured before archive"
        assert db.query(AgentRun).filter_by(proposal_id=ids["proposal"]).count() == 0

    # Read-only compilation/export remains available for the audit record.
    payload = services["sections"].compile_proposal_markdown(ids["proposal"])
    assert "Original" in payload["markdown"]
    docx_bytes, filename, summary = compile_proposal_to_docx(
        ids["proposal"],
        include_submission_checklist=False,
        proposal_title="Archived export",
    )
    assert docx_bytes.startswith(b"PK")
    assert filename == "archived-export.docx"
    assert summary["total_sections"] == 1


def test_archived_proposal_jobs_fail_before_status_audit_or_llm_work(
    inmemory_db, monkeypatch,
) -> None:
    """A stale tab must not be able to restart a job on an archive."""
    import app.db.session as db_session
    import app.jobs.amendment as amendment_job
    import app.jobs.cost_analyst as cost_analyst
    import app.jobs.cost_reviewer as cost_reviewer
    import app.jobs.cost_writer as cost_writer
    import app.jobs.final_polish as final_polish
    import app.jobs.intake as intake
    import app.jobs.market_researcher as market_researcher
    import app.jobs.outline as outline
    import app.jobs.payment_cost_reviewer as payment_cost_reviewer
    import app.jobs.payment_market_researcher as payment_market_researcher
    import app.jobs.reviewer as reviewer
    import app.jobs.strategy_implementer as strategy_implementer
    import app.jobs.team_composer as team_composer
    import app.jobs.writer as writer
    from app.models import AgentRun, Proposal
    from app.services.proposal_access import ArchivedProposalError

    monkeypatch.setattr(
        amendment_job, "session_scope", db_session.session_scope,
    )
    ids = _seed_proposal_graph(db_session, archived=True)
    proposal_id = ids["proposal"]

    entries = [
        ("intake", lambda: intake.run_intake_pipeline(proposal_id)),
        ("shortfall", lambda: intake.run_shortfall_only(proposal_id)),
        ("teaming research", lambda: intake.run_teaming_research_only(proposal_id)),
        ("section M", lambda: intake.run_section_m_only(proposal_id)),
        ("outline", lambda: outline.run_outline_generation(proposal_id)),
        ("team composer", lambda: team_composer.propose_team_composition(proposal_id)),
        ("market research", lambda: market_researcher.run_market_research(proposal_id)),
        ("payment market research", lambda: payment_market_researcher.run_payment_market_research(proposal_id)),
        ("cost analyst", lambda: cost_analyst.run_cost_analyst(proposal_id)),
        ("cost writer", lambda: cost_writer.run_cost_writer(proposal_id)),
        ("cost reviewer", lambda: cost_reviewer.run_cost_reviewer(proposal_id)),
        ("payment cost reviewer", lambda: payment_cost_reviewer.run_payment_cost_reviewer(proposal_id)),
        ("writer team", lambda: writer.run_writer_team(proposal_id)),
        ("section writer", lambda: writer.run_writer_for_section(
            proposal_id, ids["section"],
        )),
        ("reviewer", lambda: reviewer.run_reviewer_loop(proposal_id)),
        ("section reviewer", lambda: reviewer.run_reviewer_for_section(
            proposal_id, ids["section"],
        )),
        ("auto review", lambda: reviewer.run_auto_review_revise_loop(proposal_id)),
        ("final polish", lambda: final_polish.run_final_polish(proposal_id)),
        ("strategy synthesis", lambda: strategy_implementer.synthesize_strategy_directives(proposal_id)),
        ("strategy application", lambda: strategy_implementer.apply_strategy_directives(
            proposal_id, [],
        )),
        ("amendment ingestion", lambda: amendment_job.run_amendment_ingestion(
            proposal_id=proposal_id, document_id=999,
        )),
    ]
    for _label, entry in entries:
        with pytest.raises(ArchivedProposalError, match="archived and read-only"):
            entry()

    with db_session.session_scope() as db:
        assert db.get(Proposal, proposal_id).status == "archived"
        assert db.query(AgentRun).filter_by(proposal_id=proposal_id).count() == 0
