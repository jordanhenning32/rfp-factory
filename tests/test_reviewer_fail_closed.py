"""Fail-closed contracts for proposal review and submission readiness."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

from sqlalchemy.orm import Session


def _bind_db(monkeypatch):
    import app.db.session as db_session
    import app.jobs.reviewer as reviewer
    import app.services.submission_commitments as commitments

    monkeypatch.setattr(reviewer, "SessionLocal", db_session.SessionLocal)
    monkeypatch.setattr(reviewer, "session_scope", db_session.session_scope)
    monkeypatch.setattr(commitments, "session_scope", db_session.session_scope)
    return db_session, reviewer, commitments


def _seed_proposal_and_sections(engine, *, revisions=(1,)):
    from app.core.enums import ProposalStatus
    from app.models import Proposal, ProposalSection, RfpPackage

    with Session(engine) as db:
        package = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://review-fail-closed",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Review fail-closed contract",
            status=ProposalStatus.DRAFT_READY,
        )
        db.add(proposal)
        db.flush()
        sections = []
        for index, revision in enumerate(revisions, 1):
            section = ProposalSection(
                proposal_id=proposal.id,
                section_id=f"SEC-{index:03d}",
                section_title=f"Section {index}",
                section_order=index,
                draft_text_markdown=f"Current draft {index}",
                current_revision_number=revision,
            )
            db.add(section)
            db.flush()
            sections.append(section.id)
        proposal_id = proposal.id
        db.commit()
    return proposal_id, sections


def _section_snapshot(section_pk: int, *, revision: int = 1) -> dict:
    return {
        "pk": section_pk,
        "section_id": "SEC-001",
        "section_title": "Technical Approach",
        "section_brief": "Answer the requirement.",
        "page_limit": None,
        "word_limit": None,
        "requires_cost_analysis": False,
        "draft_md": "Current response.",
        "revision": revision,
        "citations": [],
        "needs_human": [],
        "applied_gaps": [],
        "compliance_items_addressed": [],
    }


def _patch_review_dependencies(monkeypatch, reviewer) -> None:
    monkeypatch.setattr(reviewer, "_run_preflight", lambda *_a, **_k: [])
    monkeypatch.setattr(reviewer, "get_pass_number_for_section", lambda _pk: 0)
    monkeypatch.setattr(reviewer, "clear_unresolved_for_section", lambda _pk: 0)
    monkeypatch.setattr(reviewer, "_set_stage", Mock())


def test_provider_failure_marks_coverage_failed_and_never_becomes_clean(
    inmemory_db,
    monkeypatch,
) -> None:
    from app.core.enums import AgentRunStatus
    from app.models import AgentRun
    from app.services.review_coverage import REVIEW_COVERAGE_AGENT

    db_session, reviewer, _commitments = _bind_db(monkeypatch)
    proposal_id, (section_pk,) = _seed_proposal_and_sections(inmemory_db)
    _patch_review_dependencies(monkeypatch, reviewer)

    def _review_a_failure(**_kwargs):
        raise RuntimeError("primary provider unavailable")

    persisted_agents: list[str] = []

    def _persist(*, reviewer_agent, findings, **_kwargs):
        persisted_agents.append(reviewer_agent)
        return len(findings)

    monkeypatch.setattr(reviewer, "review_a", _review_a_failure)
    monkeypatch.setattr(reviewer, "review_b", lambda **_kwargs: [])
    monkeypatch.setattr(reviewer, "persist_findings", _persist)

    result = reviewer._review_one_section(
        _section_snapshot(section_pk), "prefix-a", "prefix-b", proposal_id,
    )

    assert not result.succeeded
    assert any("Reviewer A" in failure for failure in result.failures)
    assert persisted_agents == ["B"]  # healthy peer still finishes
    with db_session.SessionLocal() as db:
        marker = db.query(AgentRun).filter(
            AgentRun.proposal_id == proposal_id,
            AgentRun.agent_name == REVIEW_COVERAGE_AGENT,
        ).one()
        assert marker.status == AgentRunStatus.FAILED
        assert "primary provider unavailable" in (marker.error_text or "")

    pending = Mock(return_value=[])
    monkeypatch.setattr(
        reviewer,
        "_refresh_section_snapshot",
        lambda *_args: _section_snapshot(section_pk),
    )
    monkeypatch.setattr(
        reviewer,
        "_review_one_section",
        lambda *_args: reviewer.SectionReviewResult(
            failures=("Reviewer A: provider failure",),
        ),
    )
    monkeypatch.setattr(reviewer, "get_unresolved_findings_for_section", pending)

    outcome = reviewer._process_one_section(
        section=_section_snapshot(section_pk),
        prefix_a="prefix-a",
        prefix_b="prefix-b",
        proposal_id=proposal_id,
        cancel_event=reviewer.threading.Event(),
        max_passes=1,
        section_idx=1,
        n_total=1,
    )
    assert outcome == "review_failed"
    pending.assert_not_called()


def test_finding_persistence_failure_marks_composite_review_failed(
    inmemory_db,
    monkeypatch,
) -> None:
    from app.core.enums import AgentRunStatus
    from app.models import AgentRun
    from app.services.review_coverage import REVIEW_COVERAGE_AGENT

    db_session, reviewer, _commitments = _bind_db(monkeypatch)
    proposal_id, (section_pk,) = _seed_proposal_and_sections(inmemory_db)
    _patch_review_dependencies(monkeypatch, reviewer)
    monkeypatch.setattr(reviewer, "review_a", lambda **_kwargs: [])
    monkeypatch.setattr(reviewer, "review_b", lambda **_kwargs: [])

    def _persist(*, reviewer_agent, **_kwargs):
        if reviewer_agent == "A":
            raise RuntimeError("review finding database write failed")
        return 0

    monkeypatch.setattr(reviewer, "persist_findings", _persist)
    result = reviewer._review_one_section(
        _section_snapshot(section_pk), "prefix-a", "prefix-b", proposal_id,
    )

    assert not result.succeeded
    assert any("Reviewer A" in failure for failure in result.failures)
    with db_session.SessionLocal() as db:
        marker = db.query(AgentRun).filter(
            AgentRun.agent_name == REVIEW_COVERAGE_AGENT,
        ).one()
        assert marker.status == AgentRunStatus.FAILED
        assert "database write failed" in (marker.error_text or "")


def test_readiness_requires_complete_review_of_every_current_revision(
    inmemory_db,
    monkeypatch,
) -> None:
    from app.core.enums import AgentRunStatus
    from app.models import AgentRun, ProposalSection
    from app.services.review_coverage import (
        REVIEW_COVERAGE_AGENT,
        review_coverage_prompt_version,
    )

    db_session, _reviewer, commitments = _bind_db(monkeypatch)
    proposal_id, section_ids = _seed_proposal_and_sections(
        inmemory_db, revisions=(2, 4),
    )
    now = datetime.now(UTC)

    def _add_run(agent_name, status, *, prompt_version=None):
        with db_session.session_scope() as db:
            db.add(AgentRun(
                proposal_id=proposal_id,
                agent_name=agent_name,
                model_used="fixture" if not agent_name.startswith("_") else None,
                prompt_version=prompt_version,
                started_at=now,
                completed_at=now,
                status=status,
            ))

    # Historical provider successes cannot prove which sections/revisions were
    # reviewed and must not satisfy the gate.
    _add_run("reviewer_a", AgentRunStatus.COMPLETED)
    _add_run("reviewer_b", AgentRunStatus.COMPLETED)
    checks = {
        item["key"]: item
        for item in commitments.compute_system_verified_items(proposal_id)
    }
    assert not checks["review_run"]["verified"]
    assert "0/2" in checks["review_run"]["detail"]

    first_key = review_coverage_prompt_version(section_ids[0], 2)
    second_key = review_coverage_prompt_version(section_ids[1], 4)
    _add_run(REVIEW_COVERAGE_AGENT, AgentRunStatus.COMPLETED, prompt_version=first_key)
    _add_run(REVIEW_COVERAGE_AGENT, AgentRunStatus.FAILED, prompt_version=second_key)
    checks = {
        item["key"]: item
        for item in commitments.compute_system_verified_items(proposal_id)
    }
    assert not checks["review_run"]["verified"]
    assert "latest attempt(s) failed" in checks["review_run"]["detail"]

    # The latest attempt for the second section succeeds: both current
    # revisions are now covered.
    _add_run(REVIEW_COVERAGE_AGENT, AgentRunStatus.COMPLETED, prompt_version=second_key)
    checks = {
        item["key"]: item
        for item in commitments.compute_system_verified_items(proposal_id)
    }
    assert checks["review_run"]["verified"]

    # A manual edit bumps the revision. The old clean marker is immediately
    # stale and approval fails closed until the new revision is reviewed.
    with db_session.session_scope() as db:
        db.get(ProposalSection, section_ids[0]).current_revision_number = 3
    checks = {
        item["key"]: item
        for item in commitments.compute_system_verified_items(proposal_id)
    }
    assert not checks["review_run"]["verified"]
    assert "1/2" in checks["review_run"]["detail"]
