"""Regression tests for writer failure safety and truthful status reporting."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, call

import pytest
from sqlalchemy.orm import Session


def _seed_proposal(
    engine,
    *,
    status,
    with_section: bool = False,
) -> tuple[int, int | None]:
    from app.core.enums import ProposalRole
    from app.models import Proposal, ProposalSection, RfpPackage

    with Session(engine) as db:
        package = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://writer-failure-contracts",
        )
        db.add(package)
        db.flush()

        proposal = Proposal(
            rfp_package_id=package.id,
            title="Writer Failure Contract",
            role=ProposalRole.PRIME,
            status=status,
        )
        db.add(proposal)
        db.flush()

        section_pk = None
        if with_section:
            section = ProposalSection(
                proposal_id=proposal.id,
                section_id="S1",
                section_title="Technical Approach",
                section_order=1,
                section_brief="Explain the technical approach.",
                draft_text_markdown="Prior approved draft.",
                current_revision_number=7,
                compliance_items_addressed_json=["REQ-001"],
                citations_json=[{"claim": "prior citation"}],
                needs_human_placeholders_json=[
                    {"description": "prior placeholder", "resolved_value": "answer"}
                ],
                shortfall_mitigations_applied_json=["GAP-001"],
                compliance_drift_pending=True,
            )
            db.add(section)
            db.flush()
            section_pk = section.id

        proposal_id = proposal.id
        db.commit()
        return proposal_id, section_pk


def _section_state(engine, section_pk: int) -> dict:
    from app.models import ProposalSection

    with Session(engine) as db:
        section = db.get(ProposalSection, section_pk)
        assert section is not None
        return {
            "draft": section.draft_text_markdown,
            "revision": section.current_revision_number,
            "citations": section.citations_json,
            "placeholders": section.needs_human_placeholders_json,
            "mitigations": section.shortfall_mitigations_applied_json,
            "drift": section.compliance_drift_pending,
        }


def _writer_section_snapshot(section_pk: int) -> dict:
    return {
        "pk": section_pk,
        "section_id": "S1",
        "section_title": "Technical Approach",
        "section_order": 1,
        "section_brief": "Explain the technical approach.",
        "page_limit": None,
        "word_limit": None,
        "requires_cost_analysis": False,
        "excluded_from_draft": False,
        "has_draft": True,
        "compliance_items_addressed": ["REQ-001"],
    }


def test_section_provider_failure_preserves_prior_draft(
    inmemory_db, monkeypatch,
):
    from app.core.enums import ProposalStatus
    from app.jobs import writer
    from app.services.cancellation import (
        clear_active_sections,
        get_active_sections,
    )

    proposal_id, section_pk = _seed_proposal(
        inmemory_db,
        status=ProposalStatus.DRAFT_READY,
        with_section=True,
    )
    assert section_pk is not None
    before = _section_state(inmemory_db, section_pk)
    clear_active_sections(proposal_id)

    monkeypatch.setattr(
        writer,
        "_snapshot_writer_inputs",
        lambda _proposal_id: {
            "sections": [_writer_section_snapshot(section_pk)],
            "gaps": [],
        },
    )
    monkeypatch.setattr(writer, "snapshot_resolved_placeholders", lambda _pk: [])
    monkeypatch.setattr(writer, "_build_writer_cached_prefix", lambda *_args: "prefix")
    monkeypatch.setattr(writer, "_compliance_text_lookup", lambda _snap: {})
    monkeypatch.setattr(writer, "_build_rfp_text_excerpt", lambda _pid: "")
    monkeypatch.setattr(writer, "_section_rfp_excerpt", lambda *_args: "")
    monkeypatch.setattr(writer, "_section_kb_context", lambda *_args: "")
    monkeypatch.setattr(writer, "_section_gaps_text", lambda *_args: "")
    monkeypatch.setattr(writer, "_section_outline_snippet", lambda *_args: "")
    monkeypatch.setattr(writer, "_set_stage", Mock())

    def _provider_failure(**_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(writer, "draft_section", _provider_failure)

    regenerated = writer.run_writer_for_section(proposal_id, section_pk)

    assert regenerated is False
    assert _section_state(inmemory_db, section_pk) == before
    assert get_active_sections(proposal_id) == set()


def test_cost_writer_provider_failure_preserves_prior_draft(
    inmemory_db, monkeypatch,
):
    from app.core.enums import ProposalStatus
    from app.jobs import cost_writer

    proposal_id, section_pk = _seed_proposal(
        inmemory_db,
        status=ProposalStatus.DRAFT_READY,
        with_section=True,
    )
    assert section_pk is not None
    before = _section_state(inmemory_db, section_pk)

    def _provider_failure(**_kwargs):
        raise RuntimeError("cost provider unavailable")

    persist = Mock()
    monkeypatch.setattr(cost_writer, "draft_cost_section", _provider_failure)
    monkeypatch.setattr(cost_writer, "persist_section_draft", persist)

    with pytest.raises(RuntimeError, match="cost provider unavailable"):
        cost_writer._draft_one_section(
            proposal_id=proposal_id,
            rfp_title="Test RFP",
            rfp_agency="Test Agency",
            pop_months=12,
            contract_type_signal="FFP",
            section={
                **_writer_section_snapshot(section_pk),
                "requires_cost_analysis": True,
            },
            outline_snippet="S1 Technical Approach",
            comp_text_lookup={"REQ-001": "Requirement text"},
            cached_prefix="prefix",
        )

    persist.assert_not_called()
    assert _section_state(inmemory_db, section_pk) == before


def test_auto_loop_keeps_findings_unresolved_when_regeneration_fails(
    monkeypatch,
):
    from app.jobs import reviewer, writer
    from app.services.cancellation import (
        clear_active_sections,
        get_active_sections,
    )

    proposal_id = 91
    section_pk = 42
    clear_active_sections(proposal_id)
    latest = {
        "pk": section_pk,
        "section_id": "S1",
        "section_title": "Technical Approach",
        "draft_md": "Current draft",
        "requires_cost_analysis": False,
    }
    finding = {"id": 7, "severity": "MAJOR"}
    mark_resolved = Mock()

    monkeypatch.setattr(reviewer, "_refresh_section_snapshot", lambda *_args: latest)
    monkeypatch.setattr(reviewer, "_review_one_section", Mock())
    monkeypatch.setattr(
        reviewer,
        "get_unresolved_findings_for_section",
        lambda _pk: [finding],
    )
    monkeypatch.setattr(reviewer, "accept_finding", Mock(return_value=True))
    monkeypatch.setattr(
        reviewer,
        "build_directive_from_findings",
        lambda _findings: "Apply the reviewer fix.",
    )
    monkeypatch.setattr(reviewer, "mark_findings_resolved", mark_resolved)
    monkeypatch.setattr(reviewer, "_set_stage", Mock())
    monkeypatch.setattr(writer, "run_writer_for_section", Mock(return_value=False))

    outcome = reviewer._process_one_section(
        section=latest,
        prefix_a="reviewer A prefix",
        prefix_b="reviewer B prefix",
        proposal_id=proposal_id,
        cancel_event=reviewer.threading.Event(),
        max_passes=2,
        section_idx=1,
        n_total=1,
    )

    assert outcome == "revision_failed"
    mark_resolved.assert_not_called()
    assert get_active_sections(proposal_id) == set()


def test_strategy_implementer_marks_writer_failure_not_done(monkeypatch):
    from app.jobs import strategy_implementer

    progress = Mock()
    monkeypatch.setattr(strategy_implementer, "_update_section_progress", progress)
    monkeypatch.setattr(
        strategy_implementer,
        "run_writer_for_section",
        Mock(return_value=False),
    )

    with pytest.raises(RuntimeError, match="Writer regeneration failed"):
        strategy_implementer._apply_one_directive(
            proposal_id=3,
            section_pk=8,
            section_id="S1",
            directive="Apply the strategy.",
        )

    assert progress.call_args_list == [
        call(3, "S1", "running"),
        call(3, "S1", "failed"),
    ]


@pytest.mark.parametrize(
    ("starting_status", "snapshot_result", "expected_status"),
    [
        ("awaiting_draft", {"sections": []}, "awaiting_draft"),
        ("draft_ready", RuntimeError("context failed"), "draft_ready"),
        ("reviewing", RuntimeError("context failed"), "draft_ready"),
    ],
)
def test_writer_startup_failure_restores_quiescent_status(
    inmemory_db,
    monkeypatch,
    starting_status,
    snapshot_result,
    expected_status,
):
    import app.db.session as db_session
    from app.jobs import writer
    from app.models import Proposal

    proposal_id, _ = _seed_proposal(
        inmemory_db,
        status=starting_status,
    )
    monkeypatch.setattr(writer, "session_scope", db_session.session_scope)
    monkeypatch.setattr(writer, "_set_stage", Mock())

    if isinstance(snapshot_result, Exception):
        def _snapshot_failure(_proposal_id):
            raise snapshot_result

        monkeypatch.setattr(writer, "_snapshot_writer_inputs", _snapshot_failure)
    else:
        monkeypatch.setattr(
            writer,
            "_snapshot_writer_inputs",
            lambda _proposal_id: snapshot_result,
        )

    writer.run_writer_team(proposal_id)

    with Session(inmemory_db) as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal is not None
        status_value = (
            proposal.status.value
            if hasattr(proposal.status, "value")
            else proposal.status
        )
        assert status_value == expected_status
