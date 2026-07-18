"""Tests for LLM cost-tracking persistence guards."""

from __future__ import annotations

import importlib
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.enums import AgentRunStatus, ProposalRole, ProposalStatus
from app.models import AgentRun, Proposal, RfpPackage


def _llm_module():
    import app.services.llm as llm

    return importlib.reload(llm)


def _run_kwargs(proposal_id: int | None) -> dict:
    now = datetime.now(UTC)
    return {
        "proposal_id": proposal_id,
        "agent_name": "pytest-agent",
        "model": "pytest-model",
        "input_tokens": 123,
        "output_tokens": 45,
        "cost_usd": 1.23,
        "started_at": now,
        "completed_at": now,
        "status": AgentRunStatus.COMPLETED,
        "error_text": None,
    }


def _agent_run_count(db) -> int:
    return db.scalar(select(func.count()).select_from(AgentRun))


def _seed_proposal(db) -> Proposal:
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
        status=ProposalStatus.DRAFT_READY,
    )
    db.add(proposal)
    db.flush()
    return proposal


def test_record_run_skips_when_proposal_id_does_not_exist(
    inmemory_db,
    caplog,
) -> None:
    from app.db.session import SessionLocal as InMemorySession

    llm = _llm_module()

    with caplog.at_level(logging.INFO):
        llm._record_run(**_run_kwargs(proposal_id=99999))

    with InMemorySession() as db:
        assert _agent_run_count(db) == 0

    assert any(record.levelno == logging.INFO for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_record_run_skips_when_proposal_id_is_none(inmemory_db) -> None:
    from app.db.session import SessionLocal as InMemorySession

    llm = _llm_module()

    llm._record_run(**_run_kwargs(proposal_id=None))

    with InMemorySession() as db:
        assert _agent_run_count(db) == 0


def test_record_run_inserts_when_proposal_exists(inmemory_db) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        proposal = _seed_proposal(db)
        proposal_id = proposal.id
        db.commit()

    llm = _llm_module()
    llm._record_run(**_run_kwargs(proposal_id=proposal_id))

    with InMemorySession() as db:
        rows = db.execute(select(AgentRun)).scalars().all()

    assert len(rows) == 1
    assert rows[0].proposal_id == proposal_id
    assert rows[0].input_tokens == 123
    assert rows[0].output_tokens == 45
    assert float(rows[0].cost_usd) == 1.23
