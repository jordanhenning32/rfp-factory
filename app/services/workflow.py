"""Authoritative human-gate transitions for the proposal lifecycle."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.enums import ProposalStatus
from app.db.session import session_scope
from app.models import (
    ComplianceMatrixItem,
    GapAnalysis,
    Proposal,
    RfpPackageDocument,
)
from app.services.proposal_access import (
    acquire_proposal_write_fence,
    proposal_write_lock,
)
from app.services.requirements_review import normalize_requirements_review
from app.services.submission_commitments import evaluate_submission_readiness


@contextmanager
def _serialized_transition(proposal_id: int) -> Iterator[Session]:
    """Open a transaction that owns the write slot before readiness reads.

    ``BEGIN IMMEDIATE`` prevents another SQLite session from committing a
    readiness-affecting write between validation and status persistence. On a
    server database, a row-level ``FOR UPDATE`` lock provides the equivalent
    lifecycle serialization point.
    """
    with proposal_write_lock(proposal_id):
        with session_scope() as db:
            acquire_proposal_write_fence(db, proposal_id)
            yield db


def _status_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


_SAFE_REQUIREMENTS_REVIEW_STATES = frozenset({
    "complete",
    "review_required",
    "degraded",
    "not_applicable",
})


def blocking_requirements_reviews(
    db: Session,
    *,
    rfp_package_id: int,
) -> list[tuple[str, str]]:
    """Return durable per-document reviews that have not safely finished.

    Older proposals predate ``requirements_review`` and intentionally have no
    review state.  Absence therefore remains backward compatible; once a
    document has a durable state, however, an active or incomplete review must
    not be mistaken for a clean scope package.
    """
    documents = (
        db.query(RfpPackageDocument)
        .filter(RfpPackageDocument.rfp_package_id == rfp_package_id)
        .order_by(RfpPackageDocument.id)
        .all()
    )
    blocked: list[tuple[str, str]] = []
    for document in documents:
        structure = document.structure_json
        if not isinstance(structure, dict):
            continue
        if "requirements_review" not in structure:
            continue
        review = normalize_requirements_review(structure)
        status = str(review.get("status") or "unknown").strip().lower()
        if status not in _SAFE_REQUIREMENTS_REVIEW_STATES:
            blocked.append((document.filename, status))
    return blocked


def sign_off_scope(proposal_id: int) -> dict:
    """Advance the scope gate only when intake produced reviewable scope.

    Every gap attached to an active requirement must be explicitly resolved.
    A zero-item matrix is never a valid scope package.
    """
    with _serialized_transition(proposal_id) as db:
        proposal = db.get(Proposal, proposal_id)
        if proposal is None:
            return {"ok": False, "reason": "not_found", "blockers": ["Proposal not found."]}
        current = _status_value(proposal.status)
        if current != ProposalStatus.AWAITING_SCOPE_SIGNOFF.value:
            return {
                "ok": False,
                "reason": "invalid_status",
                "blockers": [
                    "Scope sign-off is only available after intake completes "
                    f"(current status: {current})."
                ],
            }

        active_items = (
            db.query(ComplianceMatrixItem)
            .filter(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
            .count()
        )
        unresolved_gaps = (
            db.query(GapAnalysis)
            .join(
                ComplianceMatrixItem,
                ComplianceMatrixItem.id == GapAnalysis.requirement_id_fk,
            )
            .filter(
                GapAnalysis.proposal_id == proposal_id,
                GapAnalysis.resolved == False,  # noqa: E712
                ComplianceMatrixItem.status == "active",
            )
            .count()
        )
        blocked_reviews = blocking_requirements_reviews(
            db,
            rfp_package_id=proposal.rfp_package_id,
        )

        blockers: list[str] = []
        if active_items == 0:
            blockers.append(
                "The compliance matrix is empty. Retry intake before signing off scope."
            )
        if unresolved_gaps:
            blockers.append(
                f"Resolve all active gaps first ({unresolved_gaps} remaining)."
            )
        if blocked_reviews:
            review_summary = ", ".join(
                f"{filename} ({status})" for filename, status in blocked_reviews
            )
            blockers.append(
                "Requirements review is not complete for "
                f"{review_summary}. Finish or retry the review before signing off scope."
            )
        if blockers:
            return {"ok": False, "reason": "scope_incomplete", "blockers": blockers}

        proposal.status = ProposalStatus.DRAFTING
        return {"ok": True, "reason": None, "blockers": []}


def approve_for_submission(proposal_id: int) -> dict:
    """Set APPROVED only from a finished draft with no readiness blockers."""
    with _serialized_transition(proposal_id) as db:
        proposal = db.get(Proposal, proposal_id)
        if proposal is None:
            return {"ok": False, "reason": "not_found", "blockers": ["Proposal not found."]}
        readiness = evaluate_submission_readiness(proposal_id, db=db)
        if not readiness["ready"]:
            return {
                "ok": False,
                "reason": readiness["reason"],
                "blockers": readiness["blockers"],
            }
        current = _status_value(proposal.status)
        allowed = {
            ProposalStatus.DRAFT_READY.value,
            ProposalStatus.AWAITING_APPROVAL.value,
        }
        if current not in allowed:
            return {
                "ok": False,
                "reason": "invalid_status",
                "blockers": [
                    "Approval is only available after drafting and review are complete "
                    f"(current status: {current})."
                ],
            }
        proposal.status = ProposalStatus.APPROVED
    return {"ok": True, "reason": None, "blockers": []}


def mark_submitted(proposal_id: int) -> dict:
    """Mark an already-approved, still-ready proposal as submitted."""
    with _serialized_transition(proposal_id) as db:
        proposal = db.get(Proposal, proposal_id)
        if proposal is None:
            return {"ok": False, "reason": "not_found", "blockers": ["Proposal not found."]}
        readiness = evaluate_submission_readiness(proposal_id, db=db)
        if not readiness["ready"]:
            return {
                "ok": False,
                "reason": readiness["reason"],
                "blockers": readiness["blockers"],
            }
        current = _status_value(proposal.status)
        if current != ProposalStatus.APPROVED.value:
            return {
                "ok": False,
                "reason": "invalid_status",
                "blockers": [
                    "The proposal must be approved before it can be marked submitted "
                    f"(current status: {current})."
                ],
            }
        proposal.status = ProposalStatus.SUBMITTED
        proposal.submitted_at = datetime.now(UTC)
    return {"ok": True, "reason": None, "blockers": []}


def archive_proposal(proposal_id: int) -> dict:
    """Archive a submitted proposal as an immutable audit record.

    Archiving is deliberately narrower than deletion: it preserves the full
    proposal graph and package files, and is available only after submission.
    """
    with _serialized_transition(proposal_id) as db:
        proposal = db.get(Proposal, proposal_id)
        if proposal is None:
            return {"ok": False, "reason": "not_found", "blockers": ["Proposal not found."]}
        current = _status_value(proposal.status)
        if current != ProposalStatus.SUBMITTED.value:
            return {
                "ok": False,
                "reason": "invalid_status",
                "blockers": [
                    "Only a submitted proposal can be archived "
                    f"(current status: {current})."
                ],
            }
        proposal.status = ProposalStatus.ARCHIVED
    return {"ok": True, "reason": None, "blockers": []}


__all__ = [
    "approve_for_submission",
    "archive_proposal",
    "mark_submitted",
    "sign_off_scope",
]
