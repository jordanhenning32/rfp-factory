"""SubmissionCommitment CRUD — user-tracked deliverable artifacts.

Companion to the Compliance Matrix's mandatory_form / certification
items. Where the matrix items come from RFP-mandated submissions, these
come from the proposal's own commitments — when the writer (or user)
says "Quadratic will deliver X by submission day", the user can flag
that commitment for tracking so it shows up on the Submission Checklist
alongside the form-fill items. Pure CRUD; no LLM calls.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import session_scope
from app.models import ComplianceMatrixItem, SubmissionCommitment
from app.services.proposal_access import ensure_proposal_mutable

log = logging.getLogger(__name__)


@contextmanager
def _read_session(existing: Session | None) -> Iterator[Session]:
    """Use a caller-owned transaction or open one for read-only callers."""
    if existing is not None:
        yield existing
        return
    with session_scope() as owned:
        yield owned


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
        ensure_proposal_mutable(
            db, proposal_id, operation="add submission commitment",
        )
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


def list_submission_commitments(
    proposal_id: int,
    *,
    db: Session | None = None,
) -> list[dict]:
    """Snapshot every commitment for a proposal as plain dicts. Sorted
    obtained-last-then-newest-first so the user's open checklist
    items rise to the top."""
    with _read_session(db) as db:
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
        ensure_proposal_mutable(
            db, c.proposal_id, operation="update submission commitment",
        )
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
        ensure_proposal_mutable(
            db, c.proposal_id, operation="update submission commitment",
        )
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
        ensure_proposal_mutable(
            db, c.proposal_id, operation="delete submission commitment",
        )
        db.delete(c)
    return True


def set_rfp_required_item_obtained(item_pk: int, value: bool) -> bool:
    """Toggle a required form/certification checklist item.

    This write belongs in the service layer so archived-record enforcement
    cannot be bypassed by a stale Submission Checklist tab.
    """
    with session_scope() as db:
        item = db.get(ComplianceMatrixItem, item_pk)
        if item is None:
            return False
        ensure_proposal_mutable(
            db, item.proposal_id, operation="update submission checklist",
        )
        item.submission_obtained = bool(value)
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


def compute_system_verified_items(
    proposal_id: int,
    *,
    db: Session | None = None,
) -> list[dict]:
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
    from app.core.enums import AgentRunStatus, GapSeverity
    from app.models import (
        AgentRun,
        ComplianceMatrixItem,
        CostReviewFinding,
        GapAnalysis,
        PricingPackage,
        Proposal,
        ProposalSection,
        ProposalTeamMember,
        ReviewerFinding,
    )
    from app.services.review_coverage import (
        REVIEW_COVERAGE_AGENT,
        review_coverage_prompt_version,
    )
    from app.services.review_freshness import (
        get_cost_review_freshness,
        payment_cost_review_is_current,
        payment_market_scan_is_current,
    )
    from app.services.service_line import SERVICE_LINE_PAYMENT_SYSTEMS

    out: list[dict] = []
    with _read_session(db) as db:
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

        service_line = (prop.service_line or "it_services").strip()

        # Cost build present? IT services use the three persisted H/M/L
        # packages. Payment systems has no PricingPackage rows; its persisted
        # market scan is the corresponding pricing basis.
        n_packages = (
            db.query(PricingPackage)
            .filter(PricingPackage.proposal_id == proposal_id)
            .count()
        )
        if service_line == SERVICE_LINE_PAYMENT_SYSTEMS:
            payment_scan_data: dict = {}
            if (prop.payment_market_scan_json or "").strip():
                try:
                    decoded_scan = json.loads(prop.payment_market_scan_json)
                    if isinstance(decoded_scan, dict):
                        payment_scan_data = decoded_scan
                except (TypeError, json.JSONDecodeError):
                    pass
            payment_scan_exists = bool(payment_scan_data)
            payment_scan_ok = (
                payment_scan_exists
                and payment_market_scan_is_current(payment_scan_data)
            )
            cost_build_verified = payment_scan_ok
            cost_build_detail = (
                "Payment market scan and profit math are current"
                if payment_scan_ok
                else (
                    "Payment profit math is stale after a shared cost-basis "
                    "change; refresh it from the Cost tab"
                    if payment_scan_exists
                    else "Payment Market Research hasn't produced a valid scan yet"
                )
            )
        else:
            cost_build_verified = n_packages >= 3
            cost_build_detail = (
                f"{n_packages} scenario(s) persisted"
                if n_packages
                else "Cost Analyst hasn't run yet"
            )
        out.append({
            "key": "cost_build",
            "label": "Cost build complete",
            "verified": cost_build_verified,
            "detail": cost_build_detail,
            "severity": "warning",
        })

        # A supplied buyer workbook is a required deliverable, not a manual
        # checklist item. Approval/submission stays blocked until each matrix
        # is generated from the current reviewed pricing basis.
        from app.services.cost_matrix import cost_matrix_submission_check
        matrix_check = cost_matrix_submission_check(proposal_id, db=db)
        if matrix_check["count"] or matrix_check.get("pending"):
            out.append({
                "key": "cost_matrices_current",
                "label": "Buyer cost matrices completed",
                "verified": matrix_check["verified"],
                "detail": matrix_check["detail"],
                "severity": "critical",
            })

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

        n_unresolved_gaps = (
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
        out.append({
            "key": "all_gaps_resolved",
            "label": "All active gaps resolved",
            "verified": n_unresolved_gaps == 0,
            "detail": (
                "All resolved"
                if n_unresolved_gaps == 0
                else f"{n_unresolved_gaps} unresolved gap(s) â€” open the Gaps tab"
            ),
            "severity": "critical" if n_unresolved_gaps else "info",
        })

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

        # Cost Reviewer run? A clean review produces zero finding rows, so
        # findings cannot be used as the run marker. Provider calls persist
        # completed AgentRun rows even when the result is clean. The payment
        # flow persists its complete result JSON directly on Proposal.
        n_findings = (
            db.query(CostReviewFinding)
            .join(
                PricingPackage,
                CostReviewFinding.pricing_package_id == PricingPackage.id,
            )
            .filter(PricingPackage.proposal_id == proposal_id)
            .count()
        )
        if service_line == SERVICE_LINE_PAYMENT_SYSTEMS:
            payment_review_data: dict = {}
            raw_payment_review = prop.payment_cost_review_findings_json or ""
            if raw_payment_review.strip():
                try:
                    decoded = json.loads(raw_payment_review)
                    if isinstance(decoded, dict):
                        payment_review_data = decoded
                except json.JSONDecodeError:
                    pass
            payment_review_exists = bool(payment_review_data)
            payment_review_ran = (
                payment_review_exists
                and payment_cost_review_is_current(
                    proposal_id,
                    payment_review_data,
                    db=db,
                )
            )
            payment_findings = payment_review_data.get("findings") or []
            payment_pending = sum(
                1 for finding in payment_findings
                if (finding.get("user_action") or "pending").lower() == "pending"
            )
            out.append({
                "key": "cost_review_run",
                "label": "Cost Reviewer run",
                "verified": payment_review_ran,
                "detail": (
                    f"Payment review complete ({len(payment_findings)} finding(s))"
                    if payment_review_ran
                    else (
                        "Payment cost review is stale; rerun it against the "
                        "current scan, pricing data, and cost narrative"
                        if payment_review_exists
                        else "Payment Cost Reviewer hasn't run yet"
                    )
                ),
                "severity": "info",
            })
            if payment_review_ran:
                out.append({
                    "key": "no_pending_findings",
                    "label": "All cost-review findings actioned",
                    "verified": payment_pending == 0,
                    "detail": (
                        "All accepted or rejected"
                        if payment_pending == 0
                        else f"{payment_pending} still pending"
                    ),
                    "severity": "warning",
                })
        elif n_packages > 0:
            selected_scenario = (
                prop.proposed_scenario or "MEDIUM"
            ).upper().strip()
            cost_review_freshness = get_cost_review_freshness(
                db,
                proposal_id,
                scenario=selected_scenario,
            )
            out.append({
                "key": "cost_review_run",
                "label": "Cost Reviewer run",
                "verified": cost_review_freshness["verified"],
                "detail": cost_review_freshness["detail"],
                "severity": "info",
            })

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
        if service_line != SERVICE_LINE_PAYMENT_SYSTEMS and n_findings > 0:
            out.append({
                "key": "no_pending_findings",
                "label": "All cost-review findings actioned",
                "verified": n_pending_findings == 0,
                "detail": (
                    "All accepted or rejected"
                    if n_pending_findings == 0
                    else f"{n_pending_findings} still pending"
                ),
                "severity": "warning",
            })

        # Main reviewer coverage is section- and revision-specific. Provider
        # rows alone are insufficient: they are written before finding
        # persistence and do not say which section was reviewed. The
        # orchestrator's synthetic marker becomes COMPLETED only after every
        # pre-flight, Reviewer A, Reviewer B, and finding write succeeds.
        coverage_keys = {
            review_coverage_prompt_version(
                section.id, section.current_revision_number or 0,
            )
            for section in eligible
        }
        latest_coverage: dict[str, AgentRun] = {}
        if coverage_keys:
            coverage_rows = (
                db.query(AgentRun)
                .filter(
                    AgentRun.proposal_id == proposal_id,
                    AgentRun.agent_name == REVIEW_COVERAGE_AGENT,
                    AgentRun.prompt_version.in_(coverage_keys),
                )
                .order_by(AgentRun.id.asc())
                .all()
            )
            for row in coverage_rows:
                if row.prompt_version:
                    latest_coverage[row.prompt_version] = row

        completed_review_runs = sum(
            1
            for key in coverage_keys
            if (
                key in latest_coverage
                and latest_coverage[key].status == AgentRunStatus.COMPLETED
            )
        )
        failed_review_runs = sum(
            1
            for key in coverage_keys
            if (
                key in latest_coverage
                and latest_coverage[key].status == AgentRunStatus.FAILED
            )
        )
        review_verified = (
            n_eligible > 0
            and n_drafted == n_eligible
            and completed_review_runs == n_eligible
        )
        if review_verified:
            review_detail = (
                f"All {n_eligible} current section revision(s) fully reviewed"
            )
        elif failed_review_runs:
            review_detail = (
                f"{completed_review_runs}/{n_eligible} current section "
                f"revision(s) fully reviewed; {failed_review_runs} latest "
                f"attempt(s) failed"
            )
        else:
            review_detail = (
                f"{completed_review_runs}/{n_eligible} current section "
                f"revision(s) fully reviewed; run Reviewer Findings"
                if n_eligible
                else "No reviewable sections"
            )
        out.append({
            "key": "review_run",
            "label": "Proposal reviewer run",
            "verified": review_verified,
            "detail": review_detail,
            "severity": "warning",
        })

        unresolved_review_findings = (
            db.query(ReviewerFinding)
            .join(
                ProposalSection,
                ReviewerFinding.proposal_section_id == ProposalSection.id,
            )
            .filter(
                ProposalSection.proposal_id == proposal_id,
                ReviewerFinding.resolved_in_pass_number.is_(None),
                ReviewerFinding.dismissed_at.is_(None),
            )
            .count()
        )
        out.append({
            "key": "no_unresolved_review_findings",
            "label": "All proposal-review findings actioned",
            "verified": unresolved_review_findings == 0,
            "detail": (
                "All resolved or dismissed"
                if unresolved_review_findings == 0
                else f"{unresolved_review_findings} unresolved finding(s)"
            ),
            "severity": "warning",
        })

    return out


def get_submission_checklist_snapshot(
    proposal_id: int,
    *,
    db: Session | None = None,
) -> dict:
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
    if db is None:
        with session_scope() as owned_db:
            return get_submission_checklist_snapshot(
                proposal_id,
                db=owned_db,
            )

    from app.core.enums import RequirementCategory, RequirementType
    from app.models import ComplianceMatrixItem

    out: dict = {
        "rfp_required": [],
        "user_commitments": [],
        "system_checks": [],
        "totals": {},
    }

    with _read_session(db) as db:
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
    out["user_commitments"] = list_submission_commitments(
        proposal_id,
        db=db,
    )

    # System-verified readiness checks.
    out["system_checks"] = compute_system_verified_items(
        proposal_id,
        db=db,
    )

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


def evaluate_submission_readiness(
    proposal_id: int,
    *,
    db: Session | None = None,
) -> dict:
    """Return a fail-closed submission-readiness decision.

    The UI previously displayed readiness checks but approval ignored them.
    This aggregates the same system checks plus every user-obtained item into
    one authoritative decision that workflow transitions can enforce.
    """
    from app.models import Proposal

    if db is None:
        with session_scope() as owned_db:
            return evaluate_submission_readiness(
                proposal_id,
                db=owned_db,
            )

    if db.get(Proposal, proposal_id) is None:
        return {
            "ready": False,
            "reason": "not_found",
            "blockers": ["Proposal not found."],
            "snapshot": None,
        }

    snapshot = get_submission_checklist_snapshot(proposal_id, db=db)
    blockers: list[str] = []

    for check in snapshot.get("system_checks") or []:
        if check.get("verified"):
            continue
        label = (check.get("label") or "Readiness check").strip()
        detail = (check.get("detail") or "Not verified").strip()
        blockers.append(f"{label}: {detail}")

    for item in snapshot.get("rfp_required") or []:
        if not item.get("obtained"):
            req_id = (item.get("requirement_id") or "Required item").strip()
            description = (item.get("description") or "not obtained").strip()
            blockers.append(f"{req_id}: {description}")

    for commitment in snapshot.get("user_commitments") or []:
        if not commitment.get("obtained"):
            description = (commitment.get("description") or "Drafting commitment").strip()
            blockers.append(f"Submission commitment: {description}")

    # Section jobs acquire ownership under the same per-proposal process lock
    # held by approve/submit. This makes an in-flight regeneration/polish pass
    # an authoritative blocker rather than allowing approval against a draft
    # that is known to be changing but has not persisted its next revision yet.
    from app.services.cancellation import get_active_sections

    active_sections = get_active_sections(proposal_id)
    if active_sections:
        blockers.append(
            "Section work in progress: wait for "
            f"{len(active_sections)} active section worker(s) to finish."
        )

    return {
        "ready": not blockers,
        "reason": None if not blockers else "readiness_incomplete",
        "blockers": blockers,
        "snapshot": snapshot,
    }


__all__ = [
    "add_submission_commitment",
    "compute_system_verified_items",
    "get_submission_checklist_snapshot",
    "list_submission_commitments",
    "set_commitment_obtained",
    "set_rfp_required_item_obtained",
    "update_commitment",
    "delete_commitment",
    "count_unobtained",
    "evaluate_submission_readiness",
]
