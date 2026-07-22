"""ReviewerFinding persistence helpers.

The reviewer agents return ReviewerFindingDraft instances. This module
turns them into ReviewerFinding rows, and exposes the user-action
helpers (accept, dismiss, apply-as-directive).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from app.core.enums import FindingCategory, FindingSeverity, ReviewerAgent
from app.db.session import session_scope
from app.models import ProposalSection, ReviewerFinding
from app.services.proposal_access import ensure_proposal_mutable

log = logging.getLogger(__name__)


def _ensure_section_mutable(db, proposal_section_pk: int, operation: str) -> None:
    section = db.get(ProposalSection, proposal_section_pk)
    if section is not None:
        ensure_proposal_mutable(
            db, section.proposal_id, operation=operation,
        )


def persist_findings(
    *,
    proposal_section_pk: int,
    reviewer_agent: str,  # "A" or "B"
    pass_number: int,
    findings: list,  # list[ReviewerFindingDraft] from either reviewer
) -> int:
    """Insert reviewer_findings rows and return the count written."""
    written = 0
    with session_scope() as db:
        _ensure_section_mutable(
            db, proposal_section_pk, "persist reviewer findings",
        )
        for f in findings:
            try:
                sev = FindingSeverity(f.severity)
            except ValueError:
                log.warning("persist_findings: bad severity %r — defaulting to MINOR", f.severity)
                sev = FindingSeverity.MINOR
            try:
                cat = FindingCategory(f.category)
            except ValueError:
                log.warning("persist_findings: bad category %r — defaulting to other", f.category)
                # No "other" category exists; pick a sensible neutral default.
                cat = FindingCategory.WEAK_PERSUASION
            try:
                agent = ReviewerAgent(reviewer_agent)
            except ValueError:
                log.warning("persist_findings: bad agent %r", reviewer_agent)
                continue

            db.add(
                ReviewerFinding(
                    proposal_section_id=proposal_section_pk,
                    reviewer_agent=agent,
                    pass_number=pass_number,
                    severity=sev,
                    category=cat,
                    finding_text=f.finding_text,
                    suggested_fix=f.suggested_fix or None,
                )
            )
            written += 1
    return written


def clear_unresolved_for_section(proposal_section_pk: int) -> int:
    """Delete unresolved + un-dismissed findings for one section so a
    re-review starts with a clean slate. Resolved, dismissed, OR
    user-accepted findings persist:

    - Resolved (resolved_in_pass_number set) — audit trail.
    - Dismissed (dismissed_at set) — won't-fix decisions.
    - Accepted (accepted_at set) — the user is about to apply these or
      has queued them for application. Deleting them mid-loop would
      silently drop the user's intent.
    """
    with session_scope() as db:
        _ensure_section_mutable(
            db, proposal_section_pk, "clear reviewer findings",
        )
        result = db.query(ReviewerFinding).filter(
            ReviewerFinding.proposal_section_id == proposal_section_pk,
            ReviewerFinding.resolved_in_pass_number.is_(None),
            ReviewerFinding.dismissed_at.is_(None),
            ReviewerFinding.accepted_at.is_(None),
        ).delete(synchronize_session=False)
    return int(result or 0)


def get_pass_number_for_section(proposal_section_pk: int) -> int:
    """Highest pass_number on any existing finding for this section, or 0."""
    with session_scope() as db:
        rows = (
            db.query(ReviewerFinding.pass_number)
            .filter(ReviewerFinding.proposal_section_id == proposal_section_pk)
            .all()
        )
    return max((r[0] for r in rows), default=0)


def accept_finding(finding_id: int) -> bool:
    """Mark a finding accepted — it'll be included in the next directive
    when the user clicks 'Apply accepted findings' on this section."""
    with session_scope() as db:
        f = db.get(ReviewerFinding, finding_id)
        if f is None:
            return False
        _ensure_section_mutable(
            db, f.proposal_section_id, "accept reviewer finding",
        )
        f.accepted_at = datetime.utcnow()
        f.dismissed_at = None
        f.dismissed_reason = None
    return True


# Severity rank — lower number = more severe. Used by bulk-accept's
# severity_floor: a finding is in-range when its rank is <= the floor's
# rank (so floor="MINOR" lets MINOR + MAJOR + CRITICAL through;
# floor="MAJOR" lets MAJOR + CRITICAL through; floor="CRITICAL" lets
# only CRITICAL through; None lets everything through).
_SEVERITY_RANK = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}


def bulk_accept_pending_findings(
    proposal_id: int,
    *,
    severity_floor: str | None = None,
) -> dict:
    """Bulk-accept every pending ReviewerFinding for this proposal at
    or above the severity_floor. Pending = not accepted, not dismissed,
    not auto-resolved by a later pass.

    Mirrors the Cost Reviewer's `auto_accept_consensus_findings`
    pattern: when reviewers are reliable enough that the user almost
    always accepts, save them the click. The user can still dismiss
    any auto-accepted finding before clicking Apply on its section.

    severity_floor:
      None     — accept every pending finding regardless of severity.
      "MINOR"  — same effect as None (MINOR is the lowest tier).
      "MAJOR"  — accept CRITICAL + MAJOR; leave MINOR pending.
      "CRITICAL" — accept only CRITICAL; leave MAJOR + MINOR pending.

    Returns: {"accepted": N, "by_severity": {"CRITICAL": ..., "MAJOR": ..., "MINOR": ...}}
    """
    floor_rank = _SEVERITY_RANK.get((severity_floor or "").upper(), 99) if severity_floor else 99

    counts = {
        "accepted": 0,
        "by_severity": {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0},
    }
    now = datetime.utcnow()
    with session_scope() as db:
        ensure_proposal_mutable(
            db, proposal_id, operation="accept reviewer findings",
        )
        sec_pks = [
            r[0]
            for r in db.execute(
                select(ProposalSection.id).where(ProposalSection.proposal_id == proposal_id)
            ).all()
        ]
        if not sec_pks:
            return counts

        rows = (
            db.execute(
                select(ReviewerFinding).where(
                    ReviewerFinding.proposal_section_id.in_(sec_pks),
                    ReviewerFinding.accepted_at.is_(None),
                    ReviewerFinding.dismissed_at.is_(None),
                    ReviewerFinding.resolved_in_pass_number.is_(None),
                )
            )
            .scalars()
            .all()
        )

        for f in rows:
            sev = (f.severity.value if hasattr(f.severity, "value") else str(f.severity)).upper()
            rank = _SEVERITY_RANK.get(sev, 99)
            if rank > floor_rank:
                continue
            f.accepted_at = now
            counts["accepted"] += 1
            if sev in counts["by_severity"]:
                counts["by_severity"][sev] += 1

    log.info(
        "bulk_accept_pending_findings: proposal %d, severity_floor=%r, accepted=%d by_severity=%s",
        proposal_id,
        severity_floor,
        counts["accepted"],
        counts["by_severity"],
    )
    return counts


def dismiss_finding(finding_id: int, reason: str | None = None) -> bool:
    """Mark a finding dismissed (won't fix). Sets dismissed_at + reason."""
    with session_scope() as db:
        f = db.get(ReviewerFinding, finding_id)
        if f is None:
            return False
        _ensure_section_mutable(
            db, f.proposal_section_id, "dismiss reviewer finding",
        )
        f.dismissed_at = datetime.utcnow()
        f.dismissed_reason = (reason or "").strip() or None
        f.accepted_at = None
    return True


def unmark_finding(finding_id: int) -> bool:
    """Clear accept/dismiss state — back to pending."""
    with session_scope() as db:
        f = db.get(ReviewerFinding, finding_id)
        if f is None:
            return False
        _ensure_section_mutable(
            db, f.proposal_section_id, "reset reviewer finding",
        )
        f.accepted_at = None
        f.dismissed_at = None
        f.dismissed_reason = None
    return True


def get_unresolved_findings_for_section(proposal_section_pk: int) -> list[dict]:
    """All un-resolved + un-dismissed findings for a section. Used by the
    auto review-revise loop to read what's still pending each pass."""
    with session_scope() as db:
        rows = (
            db.query(ReviewerFinding)
            .filter(
                ReviewerFinding.proposal_section_id == proposal_section_pk,
                ReviewerFinding.resolved_in_pass_number.is_(None),
                ReviewerFinding.dismissed_at.is_(None),
            )
            .order_by(ReviewerFinding.severity, ReviewerFinding.id)
            .all()
        )
        return [
            {
                "id": f.id,
                "reviewer_agent": (
                    f.reviewer_agent.value if hasattr(f.reviewer_agent, "value") else str(f.reviewer_agent)
                ),
                "severity": (f.severity.value if hasattr(f.severity, "value") else str(f.severity)),
                "category": (f.category.value if hasattr(f.category, "value") else str(f.category)),
                "finding_text": f.finding_text,
                "suggested_fix": f.suggested_fix or "",
            }
            for f in rows
        ]


def mark_findings_resolved(finding_ids: list[int], pass_number: int) -> int:
    """Set resolved_in_pass_number on the given findings. The auto loop
    calls this after a writer regenerate to mark the addressed findings
    as resolved-by-revision; subsequent passes' findings are independent.
    """
    if not finding_ids:
        return 0
    with session_scope() as db:
        proposal_ids = set(
            db.execute(
                select(ProposalSection.proposal_id)
                .join(
                    ReviewerFinding,
                    ReviewerFinding.proposal_section_id == ProposalSection.id,
                )
                .where(ReviewerFinding.id.in_(finding_ids))
            ).scalars().all()
        )
        for proposal_id in proposal_ids:
            ensure_proposal_mutable(
                db, proposal_id, operation="resolve reviewer findings",
            )
        result = (
            db.query(ReviewerFinding)
            .filter(
                ReviewerFinding.id.in_(finding_ids),
                ReviewerFinding.resolved_in_pass_number.is_(None),
            )
            .update(
                {ReviewerFinding.resolved_in_pass_number: pass_number},
                synchronize_session=False,
            )
        )
    return int(result or 0)


def get_accepted_findings_for_section(proposal_section_pk: int) -> list[dict]:
    """Snapshot the accepted-but-not-yet-resolved findings for a section so
    the caller can build a Writer directive."""
    with session_scope() as db:
        rows = (
            db.query(ReviewerFinding)
            .filter(
                ReviewerFinding.proposal_section_id == proposal_section_pk,
                ReviewerFinding.accepted_at.isnot(None),
                ReviewerFinding.resolved_in_pass_number.is_(None),
            )
            .order_by(ReviewerFinding.severity, ReviewerFinding.id)
            .all()
        )
        return [
            {
                "id": f.id,
                "reviewer_agent": (
                    f.reviewer_agent.value if hasattr(f.reviewer_agent, "value") else str(f.reviewer_agent)
                ),
                "severity": (f.severity.value if hasattr(f.severity, "value") else str(f.severity)),
                "category": (f.category.value if hasattr(f.category, "value") else str(f.category)),
                "finding_text": f.finding_text,
                "suggested_fix": f.suggested_fix or "",
            }
            for f in rows
        ]


def build_directive_from_findings(findings: list[dict]) -> str:
    """Concatenate accepted findings into a directive string the Writer Team
    can consume via its existing user_directive parameter."""
    if not findings:
        return ""
    lines: list[str] = [
        "The reviewers found the following issues with this section. "
        "Address each one in your revision while preserving honesty rules, "
        "citation requirements, and the assigned compliance items:",
        "",
    ]
    for i, f in enumerate(findings, 1):
        lines.append(
            f"{i}. [{f['severity']}][{f['category']}] (Reviewer {f['reviewer_agent']}) {f['finding_text']}"
        )
        if f.get("suggested_fix"):
            lines.append(f"   SUGGESTED FIX: {f['suggested_fix']}")
        lines.append("")
    return "\n".join(lines).strip()
