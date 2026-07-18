"""Outline generation job — runs the Outline Agent in a background thread.

Triggered by the user clicking 'Generate Draft Outline' on Proposal Review
(after sign-off-scope). Produces ProposalSection rows and transitions the
proposal to AWAITING_OUTLINE_APPROVAL so the user can review before the
Writer Team begins drafting.

Same daemon-thread pattern as intake.py — single-user app, no need for RQ.
"""

from __future__ import annotations

import json
import logging
import threading

from sqlalchemy import select

from app.agents.outline_agent import build_cached_prefix, generate_outline
from app.core.company_profile import get_company_profile
from app.core.enums import ProposalStatus
from app.db.session import SessionLocal, session_scope
from app.models import (
    ComplianceMatrixItem,
    GapAnalysis,
    Proposal,
    RfpPackageDocument,
)
from app.services.kb_context import build_shortfall_kb_context
from app.services.sections import replace_outline

log = logging.getLogger(__name__)


# Per-doc cap on how much RFP text the Outline Agent sees. Section L/M is
# usually in the first 30-50 pages; cap at 60K chars (~15K tokens) per doc to
# Shared FK-safe stage-message logger; aliased so existing call sites
# in this module stay unchanged.
from app.services.stages import record_stage as _set_stage  # noqa: E402


def _build_rfp_text_excerpt(proposal_id: int) -> str:
    """Concatenate every parsed RFP document's full text. No truncation
    — the Outline Agent benefits from seeing the entire RFP (Section
    L/M instructions, attachment specs, evaluator priorities); the
    cached-prefix budget at the LLM-call layer is the only ceiling.
    """
    with SessionLocal() as db:
        docs = (
            db.execute(
                select(RfpPackageDocument)
                .join(Proposal, Proposal.rfp_package_id == RfpPackageDocument.rfp_package_id)
                .where(Proposal.id == proposal_id)
                .order_by(RfpPackageDocument.id)
            )
            .scalars()
            .all()
        )
        items = [{"filename": d.filename, "text": (d.extracted_text_md or "")} for d in docs]

    return "".join(f"\n--- RFP FILE: {it['filename']} ---\n{it['text']}\n" for it in items)


# Requirement types/categories the Outline Agent should NOT see. These
# items are handled by the Submission Checklist UI tab — they're forms
# the user fills out and submits, not narrative content the Writer Team
# drafts. Letting the Outline Agent see them produces wrapper sections
# like "Attachment E - Vendor Certification Form" that the Writer then
# wastes tokens drafting prose for.
_OUTLINE_EXCLUDED_TYPES = frozenset({"mandatory_form", "submission_format"})
_OUTLINE_EXCLUDED_CATEGORIES = frozenset({"certification"})


def _is_outline_relevant(requirement_type: str, category: str) -> bool:
    """True if this compliance item should reach the Outline Agent.
    Form-fill / certification / submission-format items are owned by
    the Submission Checklist tab, not the narrative outline."""
    if requirement_type in _OUTLINE_EXCLUDED_TYPES:
        return False
    if category in _OUTLINE_EXCLUDED_CATEGORIES:
        return False
    return True


def _is_submission_directive(requirement_type: str, category: str) -> bool:
    """True for items that MAY contain explicit section / structure /
    page-count / cover-page / TOC directives — i.e. the buyer telling
    the bidder how to STRUCTURE their response. Filtered out of the
    assignable matrix (they're not narrative), but surfaced in a
    dedicated 'MANDATORY STRUCTURE DIRECTIVES' block so the Outline
    Agent honors them verbatim. mandatory_form items are included
    here because some RFPs encode 'submit your response with the
    following sections' as a mandatory_form requirement."""
    if category in _OUTLINE_EXCLUDED_CATEGORIES:
        return False  # certifications never carry structure
    return requirement_type in _OUTLINE_EXCLUDED_TYPES


def _snapshot_compliance_and_gaps(
    proposal_id: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Snapshot compliance items + gaps with user resolution state out of the
    session so the LLM call doesn't trip DetachedInstanceError later.

    Returns three lists:
      - compliance_dicts: NARRATIVE items the Outline Agent must
        assign to exactly one section.
      - gap_dicts: gap analyses (with user resolution state) whose
        requirement is in the assignable matrix.
      - submission_directives: submission_format / mandatory_form
        items that may carry STRUCTURE / SECTION-LIST / PAGE-LIMIT
        / TOC directives. The Outline Agent reads these as
        authoritative structural guidance — when the buyer lists
        "include the following sections: 1. X 2. Y 3. Z", the
        outline MUST mirror that list verbatim. These are NOT
        assigned to sections — they constrain the outline shape.
    """
    with SessionLocal() as db:
        # Active rows only — re-running outline after an amendment
        # must build on the current spec, not superseded entries.
        items = (
            db.execute(
                select(ComplianceMatrixItem)
                .where(
                    ComplianceMatrixItem.proposal_id == proposal_id,
                    ComplianceMatrixItem.status == "active",
                )
                .order_by(ComplianceMatrixItem.id)
            )
            .scalars()
            .all()
        )
        compliance_dicts: list[dict] = []
        submission_directives: list[dict] = []
        for i in items:
            req_type = (
                i.requirement_type.value if hasattr(i.requirement_type, "value") else str(i.requirement_type)
            )
            cat = i.category.value if hasattr(i.category, "value") else str(i.category)
            entry = {
                "requirement_id": i.requirement_id,
                "requirement_text": i.requirement_text,
                "requirement_type": req_type,
                "category": cat,
                "weight": float(i.weight) if i.weight is not None else None,
                "source_section": i.source_section,
                "source_page": i.source_page,
            }
            if _is_submission_directive(req_type, cat):
                submission_directives.append(entry)
                continue
            if not _is_outline_relevant(req_type, cat):
                continue
            compliance_dicts.append(entry)
        kept_req_ids = {c["requirement_id"] for c in compliance_dicts}

        # Active-row join — re-outline after a `modified_items` amendment
        # must build on the new spec, not the gap rows still FK-pointed at
        # the superseded ComplianceMatrixItem.
        gap_rows = db.execute(
            select(GapAnalysis, ComplianceMatrixItem)
            .join(
                ComplianceMatrixItem,
                ComplianceMatrixItem.id == GapAnalysis.requirement_id_fk,
            )
            .where(
                GapAnalysis.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
            .order_by(GapAnalysis.id)
        ).all()
        gap_dicts = [
            {
                "gap_id": g.gap_id,
                "severity": g.gap_severity.value if hasattr(g.gap_severity, "value") else str(g.gap_severity),
                "current_state": g.current_state or "",
                "mitigation_options": g.mitigation_options_json or [],
                "recommended_index": g.recommended_mitigation_index,
                "selected_mitigation_index": g.selected_mitigation_index,
                "selected_partner_name": g.selected_partner_name,
                "resolved": bool(g.resolved),
                "resolution_notes": g.resolution_notes or "",
                "req_id": req.requirement_id,
            }
            for g, req in gap_rows
            if req.requirement_id in kept_req_ids
        ]
    return compliance_dicts, gap_dicts, submission_directives


def run_outline_generation(proposal_id: int) -> None:
    """Generate a section outline for a proposal and persist it.

    Status flow: DRAFTING (or AWAITING_OUTLINE_APPROVAL on regenerate)
                 → AWAITING_OUTLINE_APPROVAL.

    Re-running this clears any prior ProposalSection rows (drafts included).
    See replace_outline() for the rationale.
    """
    log.info("outline generation starting for proposal %d", proposal_id)
    try:
        _set_stage(proposal_id, "Building outline context (profile + KB + RFP text)…")

        compliance_dicts, gap_dicts, submission_directives = _snapshot_compliance_and_gaps(proposal_id)
        if not compliance_dicts:
            _set_stage(proposal_id, "No compliance items — cannot outline. Run intake first.")
            return
        if submission_directives:
            log.info(
                "outline: %d submission directive(s) detected — passing as "
                "MANDATORY STRUCTURE DIRECTIVES block (proposal %d)",
                len(submission_directives),
                proposal_id,
            )

        profile_json = json.dumps(get_company_profile(), indent=2)
        kb_context = build_shortfall_kb_context()
        rfp_text = _build_rfp_text_excerpt(proposal_id)
        cached_prefix = build_cached_prefix(
            profile_json=profile_json,
            kb_context=kb_context,
            rfp_text=rfp_text,
        )

        _set_stage(proposal_id, "Outline Agent thinking (Sonnet)…")
        sections = generate_outline(
            proposal_id=proposal_id,
            compliance_items=compliance_dicts,
            gaps=gap_dicts,
            cached_prefix=cached_prefix,
            submission_directives=submission_directives,
        )

        if not sections:
            _set_stage(proposal_id, "Outline Agent returned no sections — check logs.")
            return

        # Sanity check: every requirement_id should appear in exactly one section.
        all_req_ids = {c["requirement_id"] for c in compliance_dicts}
        assigned: dict[str, list[str]] = {}
        for s in sections:
            for rid in s.compliance_items_addressed:
                assigned.setdefault(rid, []).append(s.section_id)
        unassigned = sorted(all_req_ids - set(assigned.keys()))
        duplicated = {rid: where for rid, where in assigned.items() if len(where) > 1}
        if unassigned:
            log.warning(
                "outline_agent: %d unassigned requirement(s) for proposal %d: %s",
                len(unassigned),
                proposal_id,
                unassigned[:10],
            )
        if duplicated:
            log.warning(
                "outline_agent: %d requirement(s) assigned to multiple sections for proposal %d: %s",
                len(duplicated),
                proposal_id,
                dict(list(duplicated.items())[:5]),
            )

        n_written = replace_outline(proposal_id, sections)

        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p:
                p.status = ProposalStatus.AWAITING_OUTLINE_APPROVAL

        msg = f"Outline ready: {n_written} section(s)."
        if unassigned:
            msg += f" ⚠ {len(unassigned)} unassigned compliance item(s) — see logs."
        _set_stage(proposal_id, msg)

    except Exception:
        log.exception("outline generation failed for proposal %d", proposal_id)
        _set_stage(proposal_id, "Outline generation failed — check logs.")


def spawn_outline_generation(proposal_id: int) -> threading.Thread:
    t = threading.Thread(
        target=run_outline_generation,
        args=(proposal_id,),
        name=f"outline-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t
