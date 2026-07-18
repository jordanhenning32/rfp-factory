"""SubmissionCommitment CRUD — user-tracked deliverable artifacts.

Companion to the Compliance Matrix's mandatory_form / certification
items. Where the matrix items come from RFP-mandated submissions, these
come from the proposal's own commitments — when the writer (or user)
says "Quadratic will deliver X by submission day", the user can flag
that commitment for tracking so it shows up on the Submission Checklist
alongside the form-fill items. Pure CRUD; no LLM calls.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.session import session_scope
from app.models import SubmissionCommitment

log = logging.getLogger(__name__)


def add_submission_commitment(
    *,
    proposal_id: int,
    description: str,
    source: str = "manual",
    source_section_id: int | None = None,
    notes: str | None = None,
) -> int:
    """Insert a commitment row. Returns the new pk. Caller is
    responsible for ensuring `proposal_id` exists — FK constraint
    will raise on bad input."""
    with session_scope() as db:
        c = SubmissionCommitment(
            proposal_id=proposal_id,
            description=description.strip(),
            source=source,
            source_section_id=source_section_id,
            notes=notes,
        )
        db.add(c)
        db.flush()
        new_pk = c.id
    log.info(
        "submission_commitments: added pk=%d for proposal %d (source=%s, from_section=%s): %r",
        new_pk,
        proposal_id,
        source,
        source_section_id,
        description[:80],
    )
    return new_pk


def list_submission_commitments(proposal_id: int) -> list[dict]:
    """Snapshot every commitment for a proposal as plain dicts. Sorted
    obtained-last-then-newest-first so the user's open checklist
    items rise to the top."""
    with session_scope() as db:
        rows = (
            db.execute(
                select(SubmissionCommitment)
                .where(SubmissionCommitment.proposal_id == proposal_id)
                .order_by(
                    SubmissionCommitment.obtained.asc(),
                    SubmissionCommitment.id.desc(),
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": c.id,
                "description": c.description,
                "source": c.source,
                "source_section_id": c.source_section_id,
                "obtained": bool(c.obtained),
                "notes": c.notes or "",
            }
            for c in rows
        ]


def set_commitment_obtained(commitment_pk: int, value: bool) -> bool:
    """Toggle the obtained flag. Returns True if the row was found
    and updated."""
    with session_scope() as db:
        c = db.get(SubmissionCommitment, commitment_pk)
        if c is None:
            return False
        c.obtained = bool(value)
    return True


def update_commitment(
    commitment_pk: int,
    *,
    description: str | None = None,
    notes: str | None = None,
) -> bool:
    """Edit a commitment's description or notes. None means 'leave
    unchanged'. Returns True if the row was found and updated."""
    with session_scope() as db:
        c = db.get(SubmissionCommitment, commitment_pk)
        if c is None:
            return False
        if description is not None:
            stripped = description.strip()
            if stripped:
                c.description = stripped
        if notes is not None:
            c.notes = notes.strip() or None
    return True


def delete_commitment(commitment_pk: int) -> bool:
    """Remove a commitment row. Returns True on success."""
    with session_scope() as db:
        c = db.get(SubmissionCommitment, commitment_pk)
        if c is None:
            return False
        db.delete(c)
    return True


def count_unobtained(proposal_id: int) -> int:
    """How many commitments still need attention. Used for the
    Submission Checklist tab badge so the count includes BOTH the
    auto-extracted forms/certs AND the user's draft commitments."""
    with session_scope() as db:
        return (
            db.query(SubmissionCommitment)
            .filter(
                SubmissionCommitment.proposal_id == proposal_id,
                SubmissionCommitment.obtained == False,  # noqa: E712
            )
            .count()
        )


def compute_system_verified_items(proposal_id: int) -> list[dict]:
    """Compute the system-verifiable readiness checks for the
    Submission Checklist tab. Each returned dict is:

      key           short stable identifier (e.g. 'team_approved')
      label         user-facing label
      verified      True iff the system can confirm this is done
      detail        short status sentence ("5 members on 2026-04-28"
                    when verified, or "0 members" when not)
      severity      'critical' / 'warning' / 'info' — drives chip
                    color when not verified

    The Submission Checklist tab renders these as a system-verified
    section ABOVE the user-tickable compliance items. Verified rows
    show a green check; unverified rows show a status chip and link
    the user to the relevant tab. No persistence — recomputed on
    every render."""
    from app.core.enums import GapSeverity
    from app.models import (
        ComplianceMatrixItem,
        CostReviewFinding,
        GapAnalysis,
        PricingPackage,
        Proposal,
        ProposalSection,
        ProposalTeamMember,
    )

    out: list[dict] = []
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return []

        # Team approved?
        n_team = db.query(ProposalTeamMember).filter(ProposalTeamMember.proposal_id == proposal_id).count()
        approved_at = prop.team_approved_at
        if approved_at is not None and n_team > 0:
            try:
                date_str = approved_at.strftime("%Y-%m-%d")
            except Exception:
                date_str = str(approved_at)
            out.append(
                {
                    "key": "team_approved",
                    "label": "Team approved",
                    "verified": True,
                    "detail": f"{n_team} member(s) on {date_str}",
                    "severity": "info",
                }
            )
        else:
            out.append(
                {
                    "key": "team_approved",
                    "label": "Team approved",
                    "verified": False,
                    "detail": (
                        f"{n_team} member(s); not yet approved"
                        if n_team
                        else "No team members; open the Team tab"
                    ),
                    "severity": "critical" if n_team == 0 else "warning",
                }
            )

        # Cost build present? (any pricing package)
        n_packages = db.query(PricingPackage).filter(PricingPackage.proposal_id == proposal_id).count()
        out.append(
            {
                "key": "cost_build",
                "label": "Cost build complete",
                "verified": n_packages > 0,
                "detail": (
                    f"{n_packages} scenario(s) persisted" if n_packages else "Cost Analyst hasn't run yet"
                ),
                "severity": "warning",
            }
        )

        # No deal-breaker gaps? Active rows only — gaps tied to a
        # superseded/removed requirement are orphans, not real blockers.
        n_dealbreaker = (
            db.query(GapAnalysis)
            .join(
                ComplianceMatrixItem,
                ComplianceMatrixItem.id == GapAnalysis.requirement_id_fk,
            )
            .filter(
                GapAnalysis.proposal_id == proposal_id,
                GapAnalysis.gap_severity == GapSeverity.DEAL_BREAKER,
                GapAnalysis.resolved == False,  # noqa: E712
                ComplianceMatrixItem.status == "active",
            )
            .count()
        )
        out.append(
            {
                "key": "no_deal_breakers",
                "label": "No deal-breaker gaps",
                "verified": n_dealbreaker == 0,
                "detail": (
                    "Clear"
                    if n_dealbreaker == 0
                    else f"{n_dealbreaker} unresolved deal-breaker(s) — open the Gaps tab"
                ),
                "severity": "critical",
            }
        )

        # All eligible sections drafted? (excludes cost-deferred and
        # excluded-from-draft)
        sections = db.query(ProposalSection).filter(ProposalSection.proposal_id == proposal_id).all()
        eligible = [s for s in sections if not s.requires_cost_analysis and not s.excluded_from_draft]
        n_eligible = len(eligible)
        n_drafted = sum(1 for s in eligible if s.draft_text_markdown and str(s.draft_text_markdown).strip())
        out.append(
            {
                "key": "sections_drafted",
                "label": "All sections drafted",
                "verified": n_eligible > 0 and n_drafted == n_eligible,
                "detail": (f"{n_drafted}/{n_eligible} drafted" if n_eligible else "No outline yet"),
                "severity": "warning",
            }
        )

        # Cost-deferred sections drafted? (Cost Volume Writer ran)
        cost_sections = [s for s in sections if s.requires_cost_analysis and not s.excluded_from_draft]
        if cost_sections:
            n_cost_drafted = sum(
                1 for s in cost_sections if s.draft_text_markdown and str(s.draft_text_markdown).strip()
            )
            out.append(
                {
                    "key": "cost_sections_drafted",
                    "label": "Cost narrative drafted",
                    "verified": n_cost_drafted == len(cost_sections),
                    "detail": (f"{n_cost_drafted}/{len(cost_sections)} cost-deferred section(s) drafted"),
                    "severity": "warning",
                }
            )

        # Cost Reviewer run? (any findings persisted, even if zero)
        n_findings = (
            db.query(CostReviewFinding)
            .join(
                PricingPackage,
                CostReviewFinding.pricing_package_id == PricingPackage.id,
            )
            .filter(PricingPackage.proposal_id == proposal_id)
            .count()
        )
        # Only meaningful when the cost build exists.
        if n_packages > 0:
            out.append(
                {
                    "key": "cost_review_run",
                    "label": "Cost Reviewer run",
                    "verified": n_findings > 0,
                    "detail": (
                        f"{n_findings} finding(s) persisted" if n_findings else "Cost Reviewer hasn't run yet"
                    ),
                    "severity": "info",
                }
            )

        # All NEEDS_HUMAN placeholders resolved? Only meaningful
        # once at least one section has been drafted; before
        # that, the right "detail" is N/A rather than "All resolved".
        n_unresolved = 0
        for s in sections:
            for ph in s.needs_human_placeholders_json or []:
                if not ph.get("resolved"):
                    n_unresolved += 1
        if n_eligible == 0:
            unresolved_detail = "No sections yet"
        elif n_unresolved == 0:
            unresolved_detail = "All resolved"
        else:
            unresolved_detail = f"{n_unresolved} placeholder(s) still pending"
        out.append(
            {
                "key": "no_unresolved_placeholders",
                "label": "All NEEDS_HUMAN placeholders resolved",
                "verified": n_unresolved == 0 and n_eligible > 0,
                "detail": unresolved_detail,
                "severity": "warning",
            }
        )

        # All cost-review findings actioned? (no pending)
        n_pending_findings = (
            db.query(CostReviewFinding)
            .join(
                PricingPackage,
                CostReviewFinding.pricing_package_id == PricingPackage.id,
            )
            .filter(
                PricingPackage.proposal_id == proposal_id,
                CostReviewFinding.user_action == "pending",
            )
            .count()
        )
        if n_findings > 0:
            out.append(
                {
                    "key": "no_pending_findings",
                    "label": "All cost-review findings actioned",
                    "verified": n_pending_findings == 0,
                    "detail": (
                        "All accepted or rejected"
                        if n_pending_findings == 0
                        else f"{n_pending_findings} still pending"
                    ),
                    "severity": "warning",
                }
            )

    return out


def get_submission_checklist_snapshot(proposal_id: int) -> dict:
    """Aggregate every Submission Checklist input so callers (DOCX
    appendix, future export pipelines, etc.) can render the full
    checklist without re-querying three different sources.

    Returns:
        {
          "rfp_required": [   # mandatory_form / certification matrix
              {"requirement_id", "requirement_type", "category",
               "description", "source_section", "source_page",
               "obtained", "notes"}, ...
          ],
          "user_commitments": [
              {"id", "description", "source", "obtained", "notes"}, ...
          ],
          "system_checks": [
              {"key", "label", "verified", "detail", "severity"}, ...
          ],
          "totals": {
              "rfp_required_total", "rfp_required_obtained",
              "commitments_total", "commitments_obtained",
              "system_checks_verified", "system_checks_total",
              "all_obtained_pending",  # count of "needs user attention"
          },
        }

    Order: pending items first within each category so the deliverable
    appendix reads as an action list rather than an audit trail.
    """
    from app.core.enums import RequirementCategory, RequirementType
    from app.models import ComplianceMatrixItem

    out: dict = {
        "rfp_required": [],
        "user_commitments": [],
        "system_checks": [],
        "totals": {},
    }

    with session_scope() as db:
        # mandatory_form + certification items from the compliance
        # matrix — these are the RFP-prescribed deliverables (Form X,
        # Certification Y, etc.) the user must obtain before submission.
        rows = (
            db.execute(
                select(ComplianceMatrixItem)
                .where(
                    ComplianceMatrixItem.proposal_id == proposal_id,
                    ComplianceMatrixItem.status == "active",
                )
                .where(
                    (ComplianceMatrixItem.requirement_type == RequirementType.MANDATORY_FORM)
                    | (ComplianceMatrixItem.category == RequirementCategory.CERTIFICATION)
                )
                .order_by(
                    ComplianceMatrixItem.submission_obtained.asc(),
                    ComplianceMatrixItem.requirement_id.asc(),
                )
            )
            .scalars()
            .all()
        )
        for ci in rows:
            req_type = (
                ci.requirement_type.value
                if hasattr(ci.requirement_type, "value")
                else str(ci.requirement_type)
            )
            cat = ci.category.value if hasattr(ci.category, "value") else str(ci.category)
            out["rfp_required"].append(
                {
                    "requirement_id": ci.requirement_id,
                    "requirement_type": req_type,
                    "category": cat,
                    "description": (ci.requirement_text or "").strip(),
                    "source_section": ci.source_section or "",
                    "source_page": ci.source_page,
                    "obtained": bool(ci.submission_obtained),
                    "notes": (ci.submission_notes or "").strip(),
                }
            )

    # User-tracked commitments — already pending-first ordered by
    # list_submission_commitments.
    out["user_commitments"] = list_submission_commitments(proposal_id)

    # System-verified readiness checks.
    out["system_checks"] = compute_system_verified_items(proposal_id)

    # Roll-ups for the appendix header banner.
    rfp_total = len(out["rfp_required"])
    rfp_done = sum(1 for r in out["rfp_required"] if r["obtained"])
    com_total = len(out["user_commitments"])
    com_done = sum(1 for c in out["user_commitments"] if c["obtained"])
    sys_done = sum(1 for s in out["system_checks"] if s["verified"])
    sys_total = len(out["system_checks"])
    pending = (rfp_total - rfp_done) + (com_total - com_done) + (sys_total - sys_done)
    out["totals"] = {
        "rfp_required_total": rfp_total,
        "rfp_required_obtained": rfp_done,
        "commitments_total": com_total,
        "commitments_obtained": com_done,
        "system_checks_verified": sys_done,
        "system_checks_total": sys_total,
        "all_obtained_pending": pending,
    }
    return out


__all__ = [
    "add_submission_commitment",
    "compute_system_verified_items",
    "get_submission_checklist_snapshot",
    "list_submission_commitments",
    "set_commitment_obtained",
    "update_commitment",
    "delete_commitment",
    "count_unobtained",
]
