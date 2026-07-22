"""Retry, cancellation, and crash-recovery contracts.

All database work is isolated by ``inmemory_db``; no canonical proposal data
or provider call is touched.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _seed_proposal(
    engine,
    *,
    status,
    title: str = "Recovery contract",
) -> int:
    from app.models import Proposal, RfpPackage

    with Session(engine) as db:
        package = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://recovery-contract",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title=title,
            status=status,
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id
        db.commit()
        return proposal_id


def _status_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _patch_reviewer_db(reviewer, db_session, monkeypatch) -> None:
    # reviewer.py binds these names at import time; point them at the fixture's
    # isolated engine regardless of test collection/import order.
    monkeypatch.setattr(reviewer, "SessionLocal", db_session.SessionLocal)
    monkeypatch.setattr(reviewer, "session_scope", db_session.session_scope)


def _review_snapshot(section_count: int = 4) -> dict:
    return {
        "sections": [
            {
                "pk": index,
                "section_id": f"SEC-{index:03d}",
                "section_title": f"Section {index}",
                "draft_md": f"Draft {index}",
                "requires_cost_analysis": False,
            }
            for index in range(1, section_count + 1)
        ]
    }


def test_cancellation_registry_serializes_registration_and_cleans_up():
    from app.services.cancellation import (
        JOB_AUTO_REVIEW,
        is_cancelled,
        is_running,
        register,
        request_cancel,
        unregister,
    )

    proposal_id = 910_001
    unregister(JOB_AUTO_REVIEW, proposal_id)
    start = threading.Barrier(12)

    def _race_register():
        start.wait(timeout=2)
        return register(JOB_AUTO_REVIEW, proposal_id)

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            events = list(pool.map(lambda _n: _race_register(), range(12)))

        winners = [event for event in events if event is not None]
        assert len(winners) == 1
        event = winners[0]
        assert is_running(JOB_AUTO_REVIEW, proposal_id)
        assert request_cancel(JOB_AUTO_REVIEW, proposal_id) is True
        assert event.is_set()
        assert is_cancelled(JOB_AUTO_REVIEW, proposal_id)
        assert not is_running(JOB_AUTO_REVIEW, proposal_id)
        assert unregister(JOB_AUTO_REVIEW, proposal_id, event) is True
        assert not is_cancelled(JOB_AUTO_REVIEW, proposal_id)
    finally:
        unregister(JOB_AUTO_REVIEW, proposal_id)


def test_stale_unregister_cannot_remove_a_newer_job():
    from app.services.cancellation import (
        JOB_AUTO_REVIEW,
        is_running,
        register,
        unregister,
    )

    proposal_id = 910_002
    unregister(JOB_AUTO_REVIEW, proposal_id)
    first = register(JOB_AUTO_REVIEW, proposal_id)
    assert first is not None
    assert unregister(JOB_AUTO_REVIEW, proposal_id, first) is True

    second = register(JOB_AUTO_REVIEW, proposal_id)
    assert second is not None
    try:
        assert unregister(JOB_AUTO_REVIEW, proposal_id, first) is False
        assert is_running(JOB_AUTO_REVIEW, proposal_id)
    finally:
        unregister(JOB_AUTO_REVIEW, proposal_id, second)


def test_overlapping_section_workers_remain_visible_until_both_finish():
    from app.services.cancellation import (
        add_active_section,
        clear_active_sections,
        get_active_sections,
        remove_active_section,
    )

    proposal_id = 910_003
    section_pk = 44
    clear_active_sections(proposal_id)
    add_active_section(proposal_id, section_pk)
    add_active_section(proposal_id, section_pk)

    assert get_active_sections(proposal_id) == {section_pk}
    remove_active_section(proposal_id, section_pk)
    assert get_active_sections(proposal_id) == {section_pk}
    remove_active_section(proposal_id, section_pk)
    assert get_active_sections(proposal_id) == set()


def test_auto_review_parallel_orchestration_restores_status(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.core.enums import ProposalStatus
    from app.jobs import reviewer
    from app.models import Proposal
    from app.services.cancellation import JOB_AUTO_REVIEW, is_running

    _patch_reviewer_db(reviewer, db_session, monkeypatch)
    proposal_id = _seed_proposal(
        inmemory_db,
        status=ProposalStatus.DRAFT_READY,
    )
    stages = Mock()
    monkeypatch.setattr(reviewer, "_set_stage", stages)
    monkeypatch.setattr(reviewer, "get_settings", lambda: SimpleNamespace(auto_loop_workers=3))
    monkeypatch.setattr(reviewer, "_snapshot_review_inputs", lambda _pid: _review_snapshot(6))
    monkeypatch.setattr(reviewer, "_build_prefixes", lambda *_args: ("A", "B"))
    monkeypatch.setattr(reviewer, "_run_consistency_pass", Mock())

    worker_gate = threading.Barrier(3)
    observed: list[int] = []
    lock = threading.Lock()

    def _worker(*, section, proposal_id, **_kwargs):
        from app.services.cancellation import (
            add_active_section,
            get_active_sections,
            remove_active_section,
        )

        add_active_section(proposal_id, section["pk"])
        try:
            worker_gate.wait(timeout=2)
            with lock:
                observed.append(len(get_active_sections(proposal_id)))
            return "clean"
        finally:
            remove_active_section(proposal_id, section["pk"])

    monkeypatch.setattr(reviewer, "_process_one_section", _worker)
    reviewer.run_auto_review_revise_loop(proposal_id, max_passes=2)

    with Session(inmemory_db) as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal is not None
        assert _status_value(proposal.status) == "draft_ready"
    assert max(observed) == 3
    assert not is_running(JOB_AUTO_REVIEW, proposal_id)
    final = stages.call_args_list[-1]
    assert final.kwargs["status"] == "completed"


def test_auto_review_cancel_drains_workers_restores_status_and_marks_cancelled(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.core.enums import ProposalStatus
    from app.jobs import reviewer
    from app.models import Proposal
    from app.services.cancellation import (
        JOB_AUTO_REVIEW,
        get_active_sections,
        is_running,
        request_cancel,
    )

    _patch_reviewer_db(reviewer, db_session, monkeypatch)
    proposal_id = _seed_proposal(
        inmemory_db,
        status=ProposalStatus.DRAFT_READY,
    )
    stages = Mock()
    monkeypatch.setattr(reviewer, "_set_stage", stages)
    monkeypatch.setattr(reviewer, "get_settings", lambda: SimpleNamespace(auto_loop_workers=2))
    monkeypatch.setattr(reviewer, "_snapshot_review_inputs", lambda _pid: _review_snapshot(5))
    monkeypatch.setattr(reviewer, "_build_prefixes", lambda *_args: ("A", "B"))
    consistency = Mock()
    monkeypatch.setattr(reviewer, "_run_consistency_pass", consistency)

    workers_started = threading.Event()
    cancel_seen: list[int] = []

    def _worker(*, section, proposal_id, cancel_event, **_kwargs):
        from app.services.cancellation import add_active_section, remove_active_section

        add_active_section(proposal_id, section["pk"])
        workers_started.set()
        try:
            assert cancel_event.wait(timeout=2), "worker never received cancellation"
            cancel_seen.append(section["pk"])
            return "cancelled"
        finally:
            remove_active_section(proposal_id, section["pk"])

    monkeypatch.setattr(reviewer, "_process_one_section", _worker)

    def _cancel_when_started():
        assert workers_started.wait(timeout=2)
        assert request_cancel(JOB_AUTO_REVIEW, proposal_id) is True

    cancel_thread = threading.Thread(target=_cancel_when_started)
    cancel_thread.start()
    reviewer.run_auto_review_revise_loop(proposal_id, max_passes=2)
    cancel_thread.join(timeout=2)
    assert not cancel_thread.is_alive()

    with Session(inmemory_db) as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal is not None
        assert _status_value(proposal.status) == "draft_ready"
    assert cancel_seen
    assert get_active_sections(proposal_id) == set()
    assert not is_running(JOB_AUTO_REVIEW, proposal_id)
    consistency.assert_not_called()
    final = stages.call_args_list[-1]
    assert "CANCELLED" in final.args[1]
    assert final.kwargs["status"] == "cancelled"


def test_auto_review_startup_exception_restores_status_and_registry(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.core.enums import ProposalStatus
    from app.jobs import reviewer
    from app.models import Proposal
    from app.services.cancellation import JOB_AUTO_REVIEW, is_running

    _patch_reviewer_db(reviewer, db_session, monkeypatch)
    proposal_id = _seed_proposal(
        inmemory_db,
        status=ProposalStatus.DRAFT_READY,
    )
    stages = Mock()
    monkeypatch.setattr(reviewer, "_set_stage", stages)
    monkeypatch.setattr(reviewer, "get_settings", lambda: SimpleNamespace(auto_loop_workers=2))

    def _boom(_proposal_id):
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(reviewer, "_snapshot_review_inputs", _boom)
    reviewer.run_auto_review_revise_loop(proposal_id)

    with Session(inmemory_db) as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal is not None
        assert _status_value(proposal.status) == "draft_ready"
    assert not is_running(JOB_AUTO_REVIEW, proposal_id)
    final = stages.call_args_list[-1]
    assert "failed" in final.args[1].lower()
    assert final.kwargs["status"] == "failed"


def test_auto_review_honors_cancel_after_last_worker_before_consistency(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.core.enums import ProposalStatus
    from app.jobs import reviewer

    _patch_reviewer_db(reviewer, db_session, monkeypatch)
    proposal_id = _seed_proposal(inmemory_db, status=ProposalStatus.DRAFT_READY)
    stages = Mock()
    consistency = Mock()
    monkeypatch.setattr(reviewer, "_set_stage", stages)
    monkeypatch.setattr(reviewer, "get_settings", lambda: SimpleNamespace(auto_loop_workers=1))
    monkeypatch.setattr(reviewer, "_snapshot_review_inputs", lambda _pid: _review_snapshot(1))
    monkeypatch.setattr(reviewer, "_build_prefixes", lambda *_args: ("A", "B"))
    monkeypatch.setattr(reviewer, "_run_consistency_pass", consistency)

    def _worker(*, cancel_event, **_kwargs):
        # Simulate cancellation landing just after useful worker work ended.
        cancel_event.set()
        return "clean"

    monkeypatch.setattr(reviewer, "_process_one_section", _worker)
    reviewer.run_auto_review_revise_loop(proposal_id)

    consistency.assert_not_called()
    final = stages.call_args_list[-1]
    assert final.kwargs["status"] == "cancelled"


def test_auto_review_cleanup_preserves_unrelated_active_section_marker(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.core.enums import ProposalStatus
    from app.jobs import reviewer
    from app.services.cancellation import (
        add_active_section,
        get_active_sections,
        remove_active_section,
    )

    _patch_reviewer_db(reviewer, db_session, monkeypatch)
    proposal_id = _seed_proposal(inmemory_db, status=ProposalStatus.DRAFT_READY)
    unrelated_section_pk = 88_001
    add_active_section(proposal_id, unrelated_section_pk)
    monkeypatch.setattr(reviewer, "_set_stage", Mock())
    monkeypatch.setattr(reviewer, "get_settings", lambda: SimpleNamespace(auto_loop_workers=1))
    monkeypatch.setattr(reviewer, "_snapshot_review_inputs", lambda _pid: _review_snapshot(1))
    monkeypatch.setattr(reviewer, "_build_prefixes", lambda *_args: ("A", "B"))
    monkeypatch.setattr(reviewer, "_process_one_section", lambda **_kwargs: "clean")
    monkeypatch.setattr(reviewer, "_run_consistency_pass", Mock())

    try:
        reviewer.run_auto_review_revise_loop(proposal_id)
        assert get_active_sections(proposal_id) == {unrelated_section_pk}
    finally:
        remove_active_section(proposal_id, unrelated_section_pk)


def test_stale_busy_recovery_maps_states_and_honors_live_activity(
    inmemory_db,
):
    from app.core.enums import AgentRunStatus, ProposalStatus
    from app.models import AgentRun, Proposal
    from app.services.proposals import recover_stale_busy_proposals

    old = datetime.now(UTC) - timedelta(minutes=10)
    recent = datetime.now(UTC) - timedelta(seconds=20)

    writer_id = _seed_proposal(
        inmemory_db, status=ProposalStatus.DRAFT_IN_PROGRESS, title="Old writer"
    )
    no_stage_review_id = _seed_proposal(
        inmemory_db, status=ProposalStatus.REVIEWING, title="No stage reviewer"
    )
    recent_pricing_id = _seed_proposal(
        inmemory_db, status=ProposalStatus.PRICING, title="Live pricing"
    )
    recent_failed_review_id = _seed_proposal(
        inmemory_db, status=ProposalStatus.REVIEWING, title="Failed reviewer"
    )
    stuck_intake_id = _seed_proposal(
        inmemory_db, status=ProposalStatus.INTAKING, title="Stuck intake"
    )
    stable_id = _seed_proposal(
        inmemory_db, status=ProposalStatus.DRAFT_READY, title="Already stable"
    )

    with Session(inmemory_db) as db:
        db.add_all(
            [
                AgentRun(
                    proposal_id=writer_id,
                    agent_name="_stage",
                    status=AgentRunStatus.COMPLETED,
                    created_at=old,
                    started_at=old,
                    completed_at=old,
                    error_text="Writer still running…",
                ),
                AgentRun(
                    proposal_id=recent_pricing_id,
                    agent_name="_stage",
                    status=AgentRunStatus.COMPLETED,
                    created_at=recent,
                    started_at=recent,
                    completed_at=recent,
                    error_text="Pricing still running…",
                ),
                AgentRun(
                    proposal_id=recent_failed_review_id,
                    agent_name="_stage",
                    status=AgentRunStatus.FAILED,
                    created_at=recent,
                    started_at=recent,
                    completed_at=recent,
                    error_text="Reviewer failed.",
                ),
                AgentRun(
                    proposal_id=stuck_intake_id,
                    agent_name="_stage",
                    status=AgentRunStatus.FAILED,
                    created_at=recent,
                    started_at=recent,
                    completed_at=recent,
                    error_text="Pipeline failed.",
                ),
            ]
        )
        db.commit()

    result = recover_stale_busy_proposals()

    assert set(result["reverted"]) == {
        (writer_id, "draft_in_progress", "awaiting_draft"),
        (no_stage_review_id, "reviewing", "draft_ready"),
        (recent_failed_review_id, "reviewing", "draft_ready"),
    }
    assert result["intaking_stuck"] == [stuck_intake_id]

    with Session(inmemory_db) as db:
        states = {
            pid: _status_value(db.get(Proposal, pid).status)
            for pid in (
                writer_id,
                no_stage_review_id,
                recent_pricing_id,
                recent_failed_review_id,
                stuck_intake_id,
                stable_id,
            )
        }

    assert states == {
        writer_id: "awaiting_draft",
        no_stage_review_id: "draft_ready",
        recent_pricing_id: "pricing",
        recent_failed_review_id: "draft_ready",
        stuck_intake_id: "intaking",
        stable_id: "draft_ready",
    }


def test_intake_retry_reset_removes_stale_derivatives_but_preserves_source_and_user_data(
    inmemory_db,
    tmp_path,
):
    from app.core.enums import (
        AgentRunStatus,
        ComplianceStatus,
        FindingCategory,
        FindingSeverity,
        GapSeverity,
        ProposalOutcomeStatus,
        ProposalStatus,
        RequirementCategory,
        RequirementType,
        ReviewerAgent,
    )
    from app.models import (
        AgentRun,
        ComplianceMatrixItem,
        CostReviewFinding,
        GapAnalysis,
        MarketScan,
        PolishEdit,
        PricingPackage,
        PricingPackageLine,
        Proposal,
        ProposalOutcome,
        ProposalSection,
        ProposalTeamMember,
        ReviewerFinding,
        RfpPackage,
        RfpPackageDocument,
        SubmissionCommitment,
    )
    from app.services.proposals import reset_for_intake_retry

    now = datetime.now(UTC)
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"unchanged source bytes")

    with Session(inmemory_db) as db:
        package = RfpPackage(
            uploaded_by="human@example.test",
            uploaded_at=now,
            storage_dir=str(tmp_path),
            notes="Keep package notes",
        )
        db.add(package)
        db.flush()
        document = RfpPackageDocument(
            rfp_package_id=package.id,
            filename="source.pdf",
            storage_path=str(source_path),
            extracted_text_md="Previously extracted source text",
            page_count=3,
        )
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Keep this title",
            agency="Keep this agency",
            naics="541512",
            status=ProposalStatus.INTAKING,
            notes="Keep human notes",
            cots_orientation=True,
            team_approved_at=now,
            teaming_framing="open",
            build_framing="self_perform_first",
            service_line="it_services",
            payment_market_scan_json='{"stale": true}',
            payment_cost_review_findings_json='[{"stale": true}]',
            timeline_json='{"phases": [{"phase_name": "Keep"}]}',
            evaluation_criteria_json='{"stale": true}',
            evaluator_scorecard_json='{"stale": true}',
            win_themes_json='{"stale": true}',
            past_performance_matches_json='{"stale": true}',
            price_to_win_json='{"stale": true}',
            red_team_findings_json='{"stale": true}',
            graphics_tables_json='{"stale": true}',
            cost_review_strategy_markdown="stale strategy",
            cost_review_strategy_generated_at=now,
            cost_review_strategy_findings_count=4,
        )
        db.add_all([document, proposal])
        db.flush()

        item = ComplianceMatrixItem(
            proposal_id=proposal.id,
            requirement_id="REQ-001",
            requirement_text="The offeror shall provide a plan.",
            source_doc="source.pdf",
            requirement_type=RequirementType.SHALL,
            category=RequirementCategory.TECHNICAL,
            compliance_status=ComplianceStatus.GAP_FLAGGED,
        )
        section = ProposalSection(
            proposal_id=proposal.id,
            section_id="SEC-001",
            section_title="Stale draft",
            section_order=1,
            draft_text_markdown="This must not survive retry.",
        )
        db.add_all([item, section])
        db.flush()
        item.linked_response_section_id = section.id
        gap = GapAnalysis(
            proposal_id=proposal.id,
            requirement_id_fk=item.id,
            gap_id="GAP-001",
            gap_severity=GapSeverity.MAJOR,
            gap_description="Stale gap",
            mitigation_options_json=[],
        )
        finding = ReviewerFinding(
            proposal_section_id=section.id,
            reviewer_agent=ReviewerAgent.A_COMPLIANCE_RISK,
            pass_number=1,
            severity=FindingSeverity.MAJOR,
            category=FindingCategory.COMPLIANCE_GAP,
            finding_text="Stale finding",
        )
        polish = PolishEdit(
            proposal_id=proposal.id,
            proposal_section_id=section.id,
            section_id_label="SEC-001",
            issue_type="style",
            severity="MINOR",
            edit_summary="Stale edit",
            applied_at=now,
            applied_in_run_at=now,
            cost_usd=0.01,
        )
        commitment = SubmissionCommitment(
            proposal_id=proposal.id,
            description="Keep this user checklist item",
            source="manual",
            source_section_id=section.id,
            obtained=True,
            notes="Keep checklist notes",
        )
        team_member = ProposalTeamMember(
            proposal_id=proposal.id,
            role_name="Keep PM",
            person_kind="named",
            time_allocation_pct=50,
            phases_active_json=[],
            display_order=1,
        )
        outcome = ProposalOutcome(
            proposal_id=proposal.id,
            outcome=ProposalOutcomeStatus.PENDING,
            debrief_received=False,
            debrief_notes="Keep outcome notes",
        )
        failed_stage = AgentRun(
            proposal_id=proposal.id,
            agent_name="_stage",
            prompt_version="terminal-stage-v1",
            status=AgentRunStatus.FAILED,
            started_at=now,
            completed_at=now,
            error_text="Pipeline failed.",
        )
        market_scan = MarketScan(
            proposal_id=proposal.id,
            market_band_low_usd=100,
            market_band_mid_usd=200,
            market_band_high_usd=300,
            methodology="Stale scan",
        )
        db.add_all(
            [
                gap,
                finding,
                polish,
                commitment,
                team_member,
                outcome,
                failed_stage,
                market_scan,
            ]
        )
        db.flush()
        pricing = PricingPackage(
            proposal_id=proposal.id,
            scenario="MEDIUM",
            market_scan_id=market_scan.id,
            odcs_json=[],
            indirect_costs_json={},
            pnl_projection_json={},
        )
        db.add(pricing)
        db.flush()
        db.add_all(
            [
                PricingPackageLine(
                    pricing_package_id=pricing.id,
                    labor_category="Program Manager",
                    wage_band="120k",
                    coverage_level="high",
                    hours=100,
                    loaded_hourly_rate_usd=80,
                    loaded_cost_usd=8000,
                    ga_allocation_usd=800,
                    proposed_billing_rate_usd=120,
                    billed_total_usd=12000,
                    profit_per_hour_usd=40,
                ),
                CostReviewFinding(
                    pricing_package_id=pricing.id,
                    finding_text="Stale cost finding",
                    severity=FindingSeverity.MAJOR,
                    alternative_scenarios_json=[],
                ),
            ]
        )
        proposal_id = proposal.id
        package_id = package.id
        document_id = document.id
        commitment_id = commitment.id
        team_member_id = team_member.id
        outcome_id = outcome.id
        stage_id = failed_stage.id
        db.commit()

    result = reset_for_intake_retry(proposal_id)

    assert result == {
        "ok": True,
        "compliance_items": 1,
        "gap_analyses": 1,
        "sections": 1,
        "reviewer_findings": 1,
        "polish_edits": 1,
        "pricing_packages": 1,
        "market_scans": 1,
    }
    assert source_path.read_bytes() == b"unchanged source bytes"

    with Session(inmemory_db) as db:
        assert db.scalar(
            select(func.count()).select_from(ComplianceMatrixItem).where(
                ComplianceMatrixItem.proposal_id == proposal_id
            )
        ) == 0
        assert db.scalar(
            select(func.count()).select_from(GapAnalysis).where(
                GapAnalysis.proposal_id == proposal_id
            )
        ) == 0
        assert db.scalar(
            select(func.count()).select_from(ProposalSection).where(
                ProposalSection.proposal_id == proposal_id
            )
        ) == 0
        assert db.scalar(select(func.count()).select_from(ReviewerFinding)) == 0
        assert db.scalar(
            select(func.count()).select_from(PolishEdit).where(
                PolishEdit.proposal_id == proposal_id
            )
        ) == 0
        assert db.scalar(
            select(func.count()).select_from(PricingPackage).where(
                PricingPackage.proposal_id == proposal_id
            )
        ) == 0
        assert db.scalar(select(func.count()).select_from(PricingPackageLine)) == 0
        assert db.scalar(select(func.count()).select_from(CostReviewFinding)) == 0
        assert db.scalar(
            select(func.count()).select_from(MarketScan).where(
                MarketScan.proposal_id == proposal_id
            )
        ) == 0

        proposal = db.get(Proposal, proposal_id)
        assert proposal is not None
        assert _status_value(proposal.status) == "intaking"
        assert proposal.title == "Keep this title"
        assert proposal.agency == "Keep this agency"
        assert proposal.notes == "Keep human notes"
        assert proposal.teaming_framing == "open"
        assert proposal.build_framing == "self_perform_first"
        assert proposal.timeline_json == '{"phases": [{"phase_name": "Keep"}]}'
        assert proposal.cots_orientation is False
        assert proposal.team_approved_at is None
        assert proposal.evaluation_criteria_json is None
        assert proposal.evaluator_scorecard_json is None
        assert proposal.win_themes_json is None
        assert proposal.past_performance_matches_json is None
        assert proposal.price_to_win_json is None
        assert proposal.red_team_findings_json is None
        assert proposal.graphics_tables_json is None
        assert proposal.cost_review_strategy_markdown is None
        assert proposal.cost_review_strategy_generated_at is None
        assert proposal.cost_review_strategy_findings_count is None
        assert proposal.payment_market_scan_json is None
        assert proposal.payment_cost_review_findings_json is None

        assert db.get(RfpPackage, package_id) is not None
        preserved_doc = db.get(RfpPackageDocument, document_id)
        assert preserved_doc is not None
        assert preserved_doc.extracted_text_md == "Previously extracted source text"
        preserved_commitment = db.get(SubmissionCommitment, commitment_id)
        assert preserved_commitment is not None
        assert preserved_commitment.source_section_id is None
        assert preserved_commitment.obtained is True
        assert db.get(ProposalTeamMember, team_member_id) is not None
        assert db.get(ProposalOutcome, outcome_id) is not None
        assert db.get(AgentRun, stage_id) is not None


def test_intake_retry_refuses_live_or_post_intake_proposals_without_mutation(
    inmemory_db,
):
    from app.core.enums import AgentRunStatus, ProposalStatus, RequirementCategory, RequirementType
    from app.models import (
        AgentRun,
        ComplianceMatrixItem,
        Proposal,
        RfpPackageDocument,
    )
    from app.services.proposals import reset_for_intake_retry

    live_id = _seed_proposal(inmemory_db, status=ProposalStatus.INTAKING, title="Live intake")
    advanced_id = _seed_proposal(
        inmemory_db,
        status=ProposalStatus.AWAITING_SCOPE_SIGNOFF,
        title="Advanced proposal",
    )
    now = datetime.now(UTC)
    with Session(inmemory_db) as db:
        advanced = db.get(Proposal, advanced_id)
        assert advanced is not None
        db.add_all(
            [
                AgentRun(
                    proposal_id=live_id,
                    agent_name="_stage",
                    status=AgentRunStatus.COMPLETED,
                    started_at=now,
                    completed_at=now,
                    error_text="Extracting compliance matrix…",
                ),
                ComplianceMatrixItem(
                    proposal_id=live_id,
                    requirement_id="REQ-LIVE",
                    requirement_text="Live partial item",
                    source_doc="source.pdf",
                    requirement_type=RequirementType.SHALL,
                    category=RequirementCategory.TECHNICAL,
                ),
                ComplianceMatrixItem(
                    proposal_id=advanced_id,
                    requirement_id="REQ-ADV",
                    requirement_text="Advanced item",
                    source_doc="source.pdf",
                    requirement_type=RequirementType.SHALL,
                    category=RequirementCategory.TECHNICAL,
                ),
                RfpPackageDocument(
                    rfp_package_id=advanced.rfp_package_id,
                    filename="healthy-source.pdf",
                    storage_path="memory://healthy-source.pdf",
                    extracted_text_md="The offeror shall provide a plan.",
                    structure_json={
                        "requirements_review": {
                            "status": "complete",
                            "requires_manual_review": False,
                        }
                    },
                ),
            ]
        )
        db.commit()

    live_result = reset_for_intake_retry(live_id)
    advanced_result = reset_for_intake_retry(advanced_id)

    assert live_result["ok"] is False
    assert live_result["reason"] == "pipeline_active"
    assert advanced_result == {
        "ok": False,
        "reason": "invalid_status",
        "status": "awaiting_scope_signoff",
    }
    with Session(inmemory_db) as db:
        assert db.query(ComplianceMatrixItem).filter(
            ComplianceMatrixItem.proposal_id.in_([live_id, advanced_id])
        ).count() == 2
        healthy_document = db.query(RfpPackageDocument).filter_by(
            rfp_package_id=db.get(Proposal, advanced_id).rfp_package_id
        ).one()
        assert healthy_document.structure_json["requirements_review"][
            "status"
        ] == "complete"


@pytest.mark.parametrize(
    "blocking_status",
    ["failed", "partial", "unknown", "active", "extracting", "reviewing"],
)
def test_intake_retry_recovers_scope_gate_blocked_requirements_review(
    inmemory_db,
    blocking_status,
):
    from app.core.enums import (
        AgentRunStatus,
        ProposalStatus,
        RequirementCategory,
        RequirementType,
    )
    from app.models import (
        AgentRun,
        ComplianceMatrixItem,
        Proposal,
        RfpPackageDocument,
    )
    from app.services.proposals import reset_for_intake_retry

    proposal_id = _seed_proposal(
        inmemory_db,
        status=ProposalStatus.AWAITING_SCOPE_SIGNOFF,
        title=f"Blocked {blocking_status} review",
    )
    now = datetime.now(UTC)
    with Session(inmemory_db) as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal is not None
        blocked_document = RfpPackageDocument(
            rfp_package_id=proposal.rfp_package_id,
            filename="blocked-source.pdf",
            storage_path="memory://blocked-source.pdf",
            extracted_text_md="The offeror must submit a staffing plan.",
            structure_json={
                "outline": {"preserve": True},
                "requirements_review": {
                    "status": blocking_status,
                    "requires_manual_review": True,
                    "classification": {"stale": True},
                },
            },
        )
        healthy_document = RfpPackageDocument(
            rfp_package_id=proposal.rfp_package_id,
            filename="healthy-source.pdf",
            storage_path="memory://healthy-source.pdf",
            extracted_text_md="The offeror shall provide a transition plan.",
            structure_json={
                "requirements_review": {
                    "status": "complete",
                    "requires_manual_review": False,
                }
            },
        )
        db.add_all(
            [
                blocked_document,
                healthy_document,
                ComplianceMatrixItem(
                    proposal_id=proposal_id,
                    requirement_id="REQ-STALE",
                    requirement_text="Stale requirement",
                    source_doc="blocked-source.pdf",
                    requirement_type=RequirementType.MUST,
                    category=RequirementCategory.TECHNICAL,
                ),
                # A recently completed stage is normal at scope sign-off and
                # must not make this durable-review recovery look like a live
                # intake reset.
                AgentRun(
                    proposal_id=proposal_id,
                    agent_name="_stage",
                    status=AgentRunStatus.COMPLETED,
                    started_at=now,
                    completed_at=now,
                    error_text="Intake reached scope sign-off.",
                ),
            ]
        )
        db.commit()
        document_ids = [blocked_document.id, healthy_document.id]

    result = reset_for_intake_retry(proposal_id)

    assert result["ok"] is True
    assert result["compliance_items"] == 1
    with Session(inmemory_db) as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal is not None
        assert _status_value(proposal.status) == "intaking"
        assert db.query(ComplianceMatrixItem).filter_by(
            proposal_id=proposal_id
        ).count() == 0
        documents = (
            db.query(RfpPackageDocument)
            .filter(RfpPackageDocument.id.in_(document_ids))
            .order_by(RfpPackageDocument.id)
            .all()
        )
        assert len(documents) == 2
        for document in documents:
            review = document.structure_json["requirements_review"]
            assert review["status"] == "pending"
            assert review["source_document_id"] == document.id
            assert review["requires_manual_review"] is False
            assert "classification" not in review
        assert documents[0].structure_json["outline"] == {"preserve": True}


def test_intake_retry_refuses_recent_recoverable_failed_substep(inmemory_db):
    """A FAILED stage can be a warning while the intake thread continues."""
    from app.core.enums import (
        AgentRunStatus,
        ProposalStatus,
        RequirementCategory,
        RequirementType,
    )
    from app.models import AgentRun, ComplianceMatrixItem
    from app.services.proposals import reset_for_intake_retry

    proposal_id = _seed_proposal(
        inmemory_db,
        status=ProposalStatus.INTAKING,
        title="Continuing after recoverable failure",
    )
    now = datetime.now(UTC)
    with Session(inmemory_db) as db:
        db.add_all(
            [
                AgentRun(
                    proposal_id=proposal_id,
                    agent_name="_stage",
                    status=AgentRunStatus.FAILED,
                    started_at=now,
                    completed_at=now,
                    error_text=(
                        "Evaluation criteria extraction failed — continuing."
                    ),
                ),
                ComplianceMatrixItem(
                    proposal_id=proposal_id,
                    requirement_id="REQ-CONTINUING",
                    requirement_text="Partial live extraction must survive.",
                    source_doc="source.pdf",
                    requirement_type=RequirementType.SHALL,
                    category=RequirementCategory.TECHNICAL,
                ),
            ]
        )
        db.commit()

    result = reset_for_intake_retry(proposal_id)

    assert result["ok"] is False
    assert result["reason"] == "pipeline_active"
    assert result["stage_status"] == "failed"
    with Session(inmemory_db) as db:
        assert db.query(ComplianceMatrixItem).filter_by(
            proposal_id=proposal_id
        ).count() == 1


def test_intake_retry_reset_rolls_back_as_one_transaction(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.core.enums import AgentRunStatus, ProposalStatus, RequirementCategory, RequirementType
    from app.models import AgentRun, ComplianceMatrixItem
    from app.services.proposals import reset_for_intake_retry

    proposal_id = _seed_proposal(inmemory_db, status=ProposalStatus.INTAKING)
    now = datetime.now(UTC)
    with Session(inmemory_db) as db:
        db.add_all(
            [
                AgentRun(
                    proposal_id=proposal_id,
                    agent_name="_stage",
                    status=AgentRunStatus.FAILED,
                    started_at=now,
                    completed_at=now,
                    error_text="Pipeline failed.",
                ),
                ComplianceMatrixItem(
                    proposal_id=proposal_id,
                    requirement_id="REQ-ROLLBACK",
                    requirement_text="Must survive rollback",
                    source_doc="source.pdf",
                    requirement_type=RequirementType.MUST,
                    category=RequirementCategory.TECHNICAL,
                ),
            ]
        )
        db.commit()

    def _reject_commit(_session):
        raise RuntimeError("synthetic commit failure")

    sqlalchemy_event.listen(db_session.SessionLocal, "before_commit", _reject_commit)
    try:
        try:
            reset_for_intake_retry(proposal_id)
        except RuntimeError as exc:
            assert "synthetic commit failure" in str(exc)
        else:
            raise AssertionError("reset unexpectedly committed")
    finally:
        sqlalchemy_event.remove(db_session.SessionLocal, "before_commit", _reject_commit)

    with Session(inmemory_db) as db:
        assert db.query(ComplianceMatrixItem).filter(
            ComplianceMatrixItem.proposal_id == proposal_id
        ).count() == 1


def test_cancel_auto_review_live_loop_signals_without_forcing_status(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.core.enums import ProposalStatus
    from app.models import AgentRun, Proposal
    from app.services.cancellation import JOB_AUTO_REVIEW, register, unregister
    from app.ui import pages

    proposal_id = _seed_proposal(inmemory_db, status=ProposalStatus.REVIEWING)
    monkeypatch.setattr(pages, "session_scope", db_session.session_scope)
    notify = Mock()
    monkeypatch.setattr(pages.ui, "notify", notify)
    unregister(JOB_AUTO_REVIEW, proposal_id)
    event = register(JOB_AUTO_REVIEW, proposal_id)
    assert event is not None
    try:
        pages._cancel_auto_review(proposal_id)
        assert event.is_set()
        with Session(inmemory_db) as db:
            proposal = db.get(Proposal, proposal_id)
            assert proposal is not None
            assert _status_value(proposal.status) == "reviewing"
            assert db.query(AgentRun).filter(
                AgentRun.proposal_id == proposal_id,
                AgentRun.agent_name == "_stage",
            ).count() == 0
        assert "Cancel signal sent" in notify.call_args.args[0]
    finally:
        unregister(JOB_AUTO_REVIEW, proposal_id, event)


def test_cancel_auto_review_recovers_stale_reviewing_status(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.core.enums import ProposalStatus
    from app.models import AgentRun, Proposal
    from app.services.cancellation import JOB_AUTO_REVIEW, unregister
    from app.ui import pages

    proposal_id = _seed_proposal(inmemory_db, status=ProposalStatus.REVIEWING)
    monkeypatch.setattr(pages, "session_scope", db_session.session_scope)
    notify = Mock()
    monkeypatch.setattr(pages.ui, "notify", notify)
    unregister(JOB_AUTO_REVIEW, proposal_id)

    pages._cancel_auto_review(proposal_id)

    with Session(inmemory_db) as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal is not None
        assert _status_value(proposal.status) == "draft_ready"
        stage = db.scalar(
            select(AgentRun).where(
                AgentRun.proposal_id == proposal_id,
                AgentRun.agent_name == "_stage",
            )
        )
        assert stage is not None
        assert "Status reset by user" in (stage.error_text or "")
    assert "reset to Draft Ready" in notify.call_args.args[0]


def test_cancelled_progress_visual_is_distinct_from_failure_and_completion():
    from app.core.enums import AgentRunStatus
    from app.ui.pages import _progress_run_visual

    cancelled = SimpleNamespace(
        agent_name="_stage",
        status=AgentRunStatus.CANCELLED,
        error_text="Auto review-revise CANCELLED by user.",
    )
    assert _progress_run_visual(cancelled, 0) == (
        "cancel",
        "text-amber-700",
    )
    assert _progress_run_visual(cancelled, 0) not in {
        ("error", "text-red-700"),
        ("check_circle", "text-green-700"),
    }
