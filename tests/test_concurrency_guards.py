from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import Mock

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker


def test_section_ownership_is_reentrant_but_excludes_other_threads() -> None:
    from app.services.cancellation import (
        add_active_section,
        clear_active_sections,
        get_active_sections,
        remove_active_section,
    )

    proposal_id = 930_001
    section_pk = 73
    clear_active_sections(proposal_id)

    # Auto-review owns the section and its nested Writer call re-enters on the
    # same worker thread.
    assert add_active_section(proposal_id, section_pk) is True
    assert add_active_section(proposal_id, section_pk) is True

    result: dict[str, bool] = {}

    def _conflicting_worker() -> None:
        result["acquired"] = add_active_section(proposal_id, section_pk)
        result["released"] = remove_active_section(proposal_id, section_pk)

    worker = threading.Thread(target=_conflicting_worker, name="conflict")
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result == {"acquired": False, "released": False}
    assert get_active_sections(proposal_id) == {section_pk}

    # Both same-thread lease levels must drain before the UI marker disappears.
    assert remove_active_section(proposal_id, section_pk) is True
    assert get_active_sections(proposal_id) == {section_pk}
    assert remove_active_section(proposal_id, section_pk) is True
    assert get_active_sections(proposal_id) == set()


def test_manual_regenerate_skips_before_snapshot_when_section_is_owned(
    monkeypatch,
) -> None:
    from app.jobs import writer
    from app.services.cancellation import (
        add_active_section,
        clear_active_sections,
        get_active_sections,
        remove_active_section,
    )

    proposal_id = 930_002
    section_pk = 91
    clear_active_sections(proposal_id)
    assert add_active_section(proposal_id, section_pk) is True

    snapshot = Mock(side_effect=AssertionError("conflicting worker took snapshot"))
    stages = Mock()
    monkeypatch.setattr(writer, "require_proposal_mutable", lambda *_a, **_k: True)
    monkeypatch.setattr(writer, "_snapshot_writer_inputs", snapshot)
    monkeypatch.setattr(writer, "_set_stage", stages)

    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(
            writer.run_writer_for_section(proposal_id, section_pk),
        ),
        name="manual-regenerate-conflict",
    )
    worker.start()
    worker.join(timeout=2)

    try:
        assert not worker.is_alive()
        assert result == [False]
        snapshot.assert_not_called()
        assert get_active_sections(proposal_id) == {section_pk}
        assert stages.call_args.kwargs["status"] == "failed"
        assert "already being changed" in stages.call_args.args[1]
    finally:
        assert remove_active_section(proposal_id, section_pk) is True


def test_readiness_fails_closed_while_section_work_is_active(
    inmemory_db,
    monkeypatch,
) -> None:
    import app.db.session as db_session
    from app.core.enums import ProposalStatus
    from app.models import Proposal, RfpPackage
    from app.services import submission_commitments as commitments
    from app.services.cancellation import (
        add_active_section,
        clear_active_sections,
        remove_active_section,
    )

    monkeypatch.setattr(commitments, "session_scope", db_session.session_scope)
    with db_session.session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://active-readiness",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Active section readiness",
            status=ProposalStatus.DRAFT_READY,
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id

    monkeypatch.setattr(
        commitments,
        "get_submission_checklist_snapshot",
        lambda _proposal_id, *, db: {
            "system_checks": [],
            "rfp_required": [],
            "user_commitments": [],
        },
    )

    section_pk = 404
    clear_active_sections(proposal_id)
    assert add_active_section(proposal_id, section_pk) is True
    try:
        result = commitments.evaluate_submission_readiness(proposal_id)
        assert result["ready"] is False
        assert result["reason"] == "readiness_incomplete"
        assert result["blockers"] == [
            "Section work in progress: wait for 1 active section worker(s) "
            "to finish."
        ]
    finally:
        assert remove_active_section(proposal_id, section_pk) is True


def test_sqlite_readiness_transition_waits_for_prior_writer_and_revalidates(
    tmp_path,
    monkeypatch,
) -> None:
    """A writer holding SQLite's slot must commit before approval validates.

    This deterministically exercises two independent sessions. The old
    validate-then-open-a-new-session flow could read ``obtained=True`` and
    approve after the competing session committed ``False``.
    """
    import app.models  # noqa: F401 -- register every model on Base.metadata
    from app.core.enums import ProposalStatus
    from app.db.base import Base
    from app.models import Proposal, RfpPackage, SubmissionCommitment
    from app.services import workflow

    db_path = tmp_path / "workflow-race.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(
        bind=engine,
        autoflush=True,
        autocommit=False,
        future=True,
    )

    @contextmanager
    def _session_scope():
        db = TestSession()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    with _session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://concurrency",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Serialized readiness",
            status=ProposalStatus.DRAFT_READY,
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id
        commitment = SubmissionCommitment(
            proposal_id=proposal_id,
            description="Attach signed form",
            obtained=True,
        )
        db.add(commitment)
        db.flush()
        commitment_id = commitment.id

    monkeypatch.setattr(workflow, "session_scope", _session_scope)

    readiness_called = threading.Event()

    def _readiness(proposal_id: int, *, db) -> dict:
        readiness_called.set()
        row = db.get(SubmissionCommitment, commitment_id)
        ready = bool(row and row.obtained)
        return {
            "ready": ready,
            "reason": None if ready else "readiness_incomplete",
            "blockers": [] if ready else ["Attach signed form"],
        }

    monkeypatch.setattr(workflow, "evaluate_submission_readiness", _readiness)

    writer_has_lock = threading.Event()
    release_writer = threading.Event()
    writer_errors: list[BaseException] = []

    def _prior_writer() -> None:
        try:
            with _session_scope() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                db.get(SubmissionCommitment, commitment_id).obtained = False
                db.flush()
                writer_has_lock.set()
                assert release_writer.wait(timeout=2)
        except BaseException as exc:  # surfaced in the main test thread
            writer_errors.append(exc)

    writer_thread = threading.Thread(target=_prior_writer, name="prior-writer")
    writer_thread.start()
    assert writer_has_lock.wait(timeout=2)

    approval_result: list[dict] = []
    approval_errors: list[BaseException] = []

    def _approve() -> None:
        try:
            approval_result.append(workflow.approve_for_submission(proposal_id))
        except BaseException as exc:  # surfaced in the main test thread
            approval_errors.append(exc)

    approval_thread = threading.Thread(target=_approve, name="approval")
    approval_thread.start()

    # Approval cannot reach readiness while the earlier writer owns SQLite's
    # write slot. Releasing it establishes the deterministic serialization
    # order without relying on which Python thread happened to start first.
    assert not readiness_called.wait(timeout=0.2)
    release_writer.set()
    writer_thread.join(timeout=2)
    approval_thread.join(timeout=2)

    assert not writer_thread.is_alive()
    assert not approval_thread.is_alive()
    assert writer_errors == []
    assert approval_errors == []
    assert readiness_called.is_set()
    assert approval_result == [{
        "ok": False,
        "reason": "readiness_incomplete",
        "blockers": ["Attach signed form"],
    }]
    with _session_scope() as db:
        assert db.get(Proposal, proposal_id).status == ProposalStatus.DRAFT_READY
        assert db.get(SubmissionCommitment, commitment_id).obtained is False

    engine.dispose()
