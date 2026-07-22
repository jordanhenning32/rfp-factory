"""Stage rows carry explicit status through persistence and Progress UI."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, call

from sqlalchemy import select
from sqlalchemy.orm import Session


def _seed_proposal(engine) -> int:
    from app.core.enums import ProposalRole, ProposalStatus
    from app.models import Proposal, RfpPackage

    with Session(engine) as db:
        package = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://stage-status",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Stage Status",
            role=ProposalRole.PRIME,
            status=ProposalStatus.INTAKING,
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id
        db.commit()
        return proposal_id


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def test_record_stage_defaults_completed_and_persists_explicit_failure(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.models import AgentRun
    from app.services import stages

    monkeypatch.setattr(stages, "session_scope", db_session.session_scope)
    proposal_id = _seed_proposal(inmemory_db)

    stages.record_stage(proposal_id, "Ordinary progress complete.")
    stages.record_stage(
        proposal_id,
        "A real failure occurred.",
        status="failed",
    )

    with Session(inmemory_db) as db:
        rows = db.execute(
            select(AgentRun)
            .where(
                AgentRun.proposal_id == proposal_id,
                AgentRun.agent_name == "_stage",
            )
            .order_by(AgentRun.id)
        ).scalars().all()

    assert [_status_value(row.status) for row in rows] == [
        "completed",
        "failed",
    ]


def test_progress_visual_uses_stage_status_not_failure_words():
    from app.core.enums import AgentRunStatus
    from app.ui.pages import _progress_run_visual

    explicitly_failed = SimpleNamespace(
        agent_name="_stage",
        status=AgentRunStatus.FAILED,
        error_text="Provider stopped responding.",
    )
    failure_sounding_but_completed = SimpleNamespace(
        agent_name="_stage",
        status=AgentRunStatus.COMPLETED,
        error_text="Pipeline failed — check logs.",
    )

    assert _progress_run_visual(explicitly_failed, 0) == (
        "error",
        "text-red-700",
    )
    assert _progress_run_visual(failure_sounding_but_completed, 0) == (
        "check_circle",
        "text-green-700",
    )


def test_outline_fatal_prerequisite_explicitly_marks_failed(monkeypatch):
    from app.jobs import outline

    stages = Mock()
    monkeypatch.setattr(outline, "_set_stage", stages)
    monkeypatch.setattr(
        outline,
        "require_proposal_mutable",
        lambda _proposal_id, **_kwargs: None,
    )
    monkeypatch.setattr(
        outline,
        "_snapshot_compliance_and_gaps",
        lambda _proposal_id: ([], [], []),
    )

    outline.run_outline_generation(17)

    assert stages.call_args_list == [
        call(17, "Building outline context (profile + KB + RFP text)…"),
        call(
            17,
            "No compliance items — cannot outline. Run intake first.",
            status="failed",
        ),
    ]
