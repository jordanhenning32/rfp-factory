"""Authoritative write guard for archived proposal records.

Archived proposals are retained as immutable audit records.  UI controls are
disabled as a convenience, but callers may still hold a stale browser tab or
invoke a service directly.  Every proposal-owned write path should therefore
call the helpers in this module inside the same transaction that performs the
mutation.

Read-only operations (including export) do not call this guard.  Outcome and
debrief edits do call it: capture those before archiving, because archive is
the point at which the entire proposal record becomes immutable.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.enums import ProposalStatus
from app.models import Proposal


class ArchivedProposalError(PermissionError):
    """Raised when a caller tries to mutate an archived proposal record."""

    def __init__(self, proposal_id: int, operation: str = "modify proposal") -> None:
        self.proposal_id = proposal_id
        self.operation = operation
        super().__init__(
            f"Proposal {proposal_id} is archived and read-only; "
            f"cannot {operation}."
        )


_proposal_lock_registry_guard = threading.Lock()
_proposal_write_locks: dict[int, threading.RLock] = {}


@contextmanager
def proposal_write_lock(proposal_id: int) -> Iterator[None]:
    """Serialize multi-step writes for one proposal inside this process.

    Use this around workflows whose archive/status guard and durable mutation
    cannot live in one database statement or transaction (for example, a DB
    check followed by an atomic JSON-file replace). Database transactions are
    still required for cross-session/process serialization.
    """
    with _proposal_lock_registry_guard:
        lock = _proposal_write_locks.setdefault(
            int(proposal_id),
            threading.RLock(),
        )
    with lock:
        yield


def acquire_proposal_write_fence(db: Session, proposal_id: int) -> None:
    """Acquire the database write boundary before a guarded read/transition.

    SQLite has no row-level ``FOR UPDATE``; ``BEGIN IMMEDIATE`` reserves the
    database write slot before the first validation query. Callers must invoke
    this on a fresh Session before any DB access. Server databases use a row
    lock on the proposal instead.
    """
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        if db.in_transaction():
            raise RuntimeError(
                "SQLite proposal write fence must be acquired before any "
                "database access in the transaction."
            )
        db.execute(text("BEGIN IMMEDIATE"))
        return

    db.execute(
        select(Proposal.id)
        .where(Proposal.id == proposal_id)
        .with_for_update()
    )


def _status_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def ensure_proposal_mutable(
    db: Session,
    proposal_id: int,
    *,
    operation: str = "modify proposal",
) -> Proposal | None:
    """Return the proposal, raising when it is archived.

    ``None`` preserves each caller's existing not-found contract (some return
    ``False`` while others raise ``ValueError``).  The archive decision is the
    only policy centralized here.
    """
    proposal = db.get(Proposal, proposal_id)
    if (
        proposal is not None
        and _status_value(proposal.status) == ProposalStatus.ARCHIVED.value
    ):
        raise ArchivedProposalError(proposal_id, operation)
    return proposal


def ensure_owned_row_mutable(
    db: Session,
    row,
    *,
    operation: str = "modify proposal data",
) -> None:
    """Guard a proposal-owned ORM row exposing a direct ``proposal_id`` FK."""
    if row is not None:
        ensure_proposal_mutable(
            db,
            int(row.proposal_id),
            operation=operation,
        )


def require_proposal_mutable(
    proposal_id: int,
    *,
    operation: str = "run proposal workflow",
) -> bool:
    """Fail fast at a background-job boundary if a proposal is archived.

    Jobs call this before changing status, recording an AgentRun, or spending
    on an LLM.  The session module is resolved at call time so test harnesses
    that replace ``session_scope`` remain isolated.
    """
    from app.db import session as db_session

    with db_session.session_scope() as db:
        return ensure_proposal_mutable(
            db, proposal_id, operation=operation,
        ) is not None


__all__ = [
    "acquire_proposal_write_fence",
    "ArchivedProposalError",
    "ensure_owned_row_mutable",
    "ensure_proposal_mutable",
    "proposal_write_lock",
    "require_proposal_mutable",
]
