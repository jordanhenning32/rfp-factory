"""Reviewer Loop orchestration.

Three entry points, all defined here:

1. ``run_reviewer_loop`` — single review pass over every drafted section
   (no revisions). Used by the "Review only" button. Surfaces findings
   for the user to act on manually via the Findings tab.

2. ``run_reviewer_for_section`` — re-review one section after the user
   applies accepted findings, to verify the issues were addressed.

3. ``run_auto_review_revise_loop`` — parallelized review→regenerate
   cycle, driven by ``settings.auto_loop_workers`` (default 4). Each
   worker calls ``_process_one_section`` against its own section: review
   A+B → if findings, accept all + Writer Team regenerate → repeat,
   with stuck detection and pass-3+ escalation. Stops on convergence,
   pass cap, or stuck threshold.

Per-section work runs deterministic pre-flights (``_run_preflight``)
before Reviewer A: citation legitimacy + compliance coverage. Both feed
into the same ``persist_findings`` path with ``reviewer_agent='A'``.

Cancellation: workers poll ``cancel_event.is_set()`` at every safe
checkpoint (start of pass, after each LLM call) and return
``"cancelled"``. The orchestrator drains the executor naturally — total
tail latency on cancel ≈ longest in-flight LLM call.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.agents.reviewer_a import build_cached_prefix as build_a_prefix
from app.agents.reviewer_a import review_section as review_a
from app.agents.reviewer_b import build_cached_prefix as build_b_prefix
from app.agents.reviewer_b import review_section as review_b
from app.config import get_settings
from app.core.company_profile import get_company_profile
from app.core.enums import AgentRunStatus, ProposalStatus
from app.db.session import SessionLocal, session_scope
from app.models import (
    AgentRun,
    ComplianceMatrixItem,
    GapAnalysis,
    Proposal,
    ProposalSection,
    RfpPackageDocument,
)
from app.services.cancellation import (
    JOB_AUTO_REVIEW,
    add_active_section,
    remove_active_section,
)
from app.services.cancellation import (
    register as register_cancel,
)
from app.services.cancellation import (
    unregister as unregister_cancel,
)
from app.services.evaluation_criteria import format_evaluation_criteria_block
from app.services.findings import (
    accept_finding,
    build_directive_from_findings,
    clear_unresolved_for_section,
    get_pass_number_for_section,
    get_unresolved_findings_for_section,
    mark_findings_resolved,
    persist_findings,
)
from app.services.kb_context import build_shortfall_kb_context
from app.services.proposal_access import require_proposal_mutable
from app.services.review_coverage import (
    REVIEW_COVERAGE_AGENT,
    review_coverage_prompt_version,
)

log = logging.getLogger(__name__)


# Shared FK-safe stage-message logger; aliased so existing call sites
# in this module stay unchanged.
from app.services.stages import record_stage as _set_stage  # noqa: E402


@dataclass(frozen=True)
class SectionReviewResult:
    """Outcome of the composite review operation for one section.

    Finding counts alone cannot distinguish a genuinely clean response from a
    provider or persistence failure.  ``failures`` makes that distinction
    explicit while still allowing the healthy reviewer to finish when its
    peer fails.
    """

    n_findings_a: int = 0
    n_findings_b: int = 0
    failures: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.failures

    def __iter__(self):
        """Preserve the private helper's historical two-count unpacking."""
        yield self.n_findings_a
        yield self.n_findings_b


def _start_review_coverage_attempt(
    proposal_id: int,
    section_pk: int,
    revision: int,
) -> int:
    """Persist a RUNNING marker before any destructive review bookkeeping."""
    now = datetime.now(UTC)
    with session_scope() as db:
        marker = AgentRun(
            proposal_id=proposal_id,
            agent_name=REVIEW_COVERAGE_AGENT,
            model_used=None,
            prompt_version=review_coverage_prompt_version(section_pk, revision),
            started_at=now,
            status=AgentRunStatus.RUNNING,
        )
        db.add(marker)
        db.flush()
        marker_id = marker.id
    return marker_id


def _finish_review_coverage_attempt(
    marker_id: int,
    *,
    succeeded: bool,
    error_text: str | None = None,
) -> None:
    """Finalize one section/revision marker.

    A missing marker is an integrity failure, not a best-effort logging issue:
    without it submission readiness cannot prove that the section was fully
    reviewed.
    """
    with session_scope() as db:
        marker = db.get(AgentRun, marker_id)
        if marker is None:
            raise RuntimeError(
                f"review coverage marker {marker_id} disappeared before completion"
            )
        marker.status = (
            AgentRunStatus.COMPLETED if succeeded else AgentRunStatus.FAILED
        )
        marker.completed_at = datetime.now(UTC)
        marker.error_text = (error_text or "")[:2000] or None


def _build_rfp_text_excerpt(proposal_id: int) -> str:
    """Concatenate every parsed RFP document's full text. No truncation
    — the cached-prefix budget at the LLM-call layer is the only ceiling.
    Mirrors the writer's helper so reviewers see the same source."""
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


def _format_outline_for_review(sections: list[dict]) -> str:
    if not sections:
        return "(no outline)"
    lines = []
    for s in sections:
        marker = " [COST-DEFERRED]" if s.get("requires_cost_analysis") else ""
        lines.append(
            f"{s['section_id']} (#{s['section_order']}) {s['section_title']}{marker}\n"
            f"  Brief: {s.get('section_brief') or '(none)'}\n"
            f"  Addresses: {', '.join(s.get('compliance_items_addressed') or []) or '(none)'}"
        )
    return "\n".join(lines)


def _format_compliance_for_review(items: list[dict]) -> str:
    lines = []
    for it in items:
        line = f"{it['requirement_id']} [{it['requirement_type']}/{it['category']}"
        if it.get("weight"):
            line += f" w={it['weight']}"
        line += f"] {it['requirement_text']}"
        if it.get("source_section"):
            line += f"  ({it['source_section']})"
        lines.append(line)
    return "\n".join(lines) if lines else "(no compliance items)"


def _format_gaps_for_review(gaps: list[dict]) -> str:
    if not gaps:
        return "(no gaps)"
    blocks = []
    for g in gaps:
        sel_idx = g.get("selected_mitigation_index")
        rec_idx = g.get("recommended_index")
        chosen_idx = sel_idx if sel_idx is not None else rec_idx
        opts = g.get("mitigation_options") or []
        chosen_block = ""
        if chosen_idx is not None and 0 <= chosen_idx < len(opts):
            opt = opts[chosen_idx]
            chosen_block = (
                f"  CHOSEN MITIGATION (option {chosen_idx}): {opt.get('approach', '?')}\n"
                f"    Proposal language draft: {opt.get('proposal_language_draft', '')}\n"
                f"    Honesty check: {opt.get('honesty_check', '')}\n"
            )
            sel_partner = g.get("selected_partner_name")
            if sel_partner:
                chosen_block += f"    Selected partner: {sel_partner}\n"
        blocks.append(
            f"{g['gap_id']} [{g['severity']}] addresses {g['req_id']}\n"
            f"  Current state: {g.get('current_state', '')}\n"
            f"{chosen_block}"
        )
    return "\n".join(blocks)


def _snapshot_review_inputs(proposal_id: int) -> dict:
    """Pull everything the reviewers need in one pass."""
    with SessionLocal() as db:
        sec_rows = (
            db.execute(
                select(ProposalSection)
                .where(ProposalSection.proposal_id == proposal_id)
                .order_by(ProposalSection.section_order, ProposalSection.id)
            )
            .scalars()
            .all()
        )
        sections = [
            {
                "pk": s.id,
                "section_id": s.section_id,
                "section_title": s.section_title,
                "section_order": s.section_order,
                "section_brief": s.section_brief,
                "page_limit": s.page_limit,
                "word_limit": s.word_limit,
                "requires_cost_analysis": bool(s.requires_cost_analysis),
                "draft_md": s.draft_text_markdown,
                "revision": s.current_revision_number or 0,
                "citations": list(s.citations_json or []),
                "needs_human": list(s.needs_human_placeholders_json or []),
                "applied_gaps": list(s.shortfall_mitigations_applied_json or []),
                "compliance_items_addressed": list(s.compliance_items_addressed_json or []),
            }
            for s in sec_rows
        ]

        # Active rows only — superseded / removed must not reach the
        # reviewer's cached prefix.
        comp_rows = (
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
        compliance_items = [
            {
                "requirement_id": i.requirement_id,
                "requirement_text": i.requirement_text,
                "requirement_type": (
                    i.requirement_type.value
                    if hasattr(i.requirement_type, "value")
                    else str(i.requirement_type)
                ),
                "category": (i.category.value if hasattr(i.category, "value") else str(i.category)),
                "weight": float(i.weight) if i.weight is not None else None,
                "source_section": i.source_section,
                "source_page": i.source_page,
                "amendment_origin": i.amendment_origin,
            }
            for i in comp_rows
        ]

        # Build a lookup so we can attach `amended_items_for_section`
        # to each section dict (only items whose amendment_origin is
        # set count). Reviewer A consumes this via the AMENDED ITEMS
        # user-prompt block.
        req_id_to_origin: dict[str, str | None] = {i.requirement_id: i.amendment_origin for i in comp_rows}
        req_id_to_text: dict[str, str] = {i.requirement_id: i.requirement_text or "" for i in comp_rows}
        for sec in sections:
            amended_for_section: list[dict] = []
            for rid in sec.get("compliance_items_addressed") or []:
                origin = req_id_to_origin.get(rid)
                if origin:
                    amended_for_section.append(
                        {
                            "requirement_id": rid,
                            "requirement_text": req_id_to_text.get(rid, ""),
                            "amendment_origin": origin,
                        }
                    )
            sec["amended_items_for_section"] = amended_for_section

        # Active-row join — superseded ComplianceMatrixItem rows keep their
        # original GapAnalysis link, but those gaps describe a requirement
        # whose text no longer applies. Reviewer A's cached prefix would
        # otherwise carry stale gap context across passes.
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
        gaps = [
            {
                "gap_id": g.gap_id,
                "severity": (
                    g.gap_severity.value if hasattr(g.gap_severity, "value") else str(g.gap_severity)
                ),
                "current_state": g.current_state or "",
                "mitigation_options": g.mitigation_options_json or [],
                "recommended_index": g.recommended_mitigation_index,
                "selected_mitigation_index": g.selected_mitigation_index,
                "selected_partner_name": g.selected_partner_name,
                "req_id": req.requirement_id,
            }
            for g, req in gap_rows
        ]

    return {"sections": sections, "compliance_items": compliance_items, "gaps": gaps}


def _build_prefixes(proposal_id: int, snap: dict) -> tuple[str, str]:
    """Build the two cached prefixes — A wants profile + KB + outline +
    compliance + gaps; B wants profile + outline + compliance + RFP text."""
    profile_json = json.dumps(get_company_profile(), indent=2)
    kb_context = build_shortfall_kb_context()
    outline_text = _format_outline_for_review(snap["sections"])
    compliance_text = _format_compliance_for_review(snap["compliance_items"])
    gaps_text = _format_gaps_for_review(snap["gaps"])
    rfp_text = _build_rfp_text_excerpt(proposal_id)

    # Load evaluation criteria for the Reviewer A prefix block
    criteria_dict = None
    try:
        with SessionLocal() as db:
            proposal = db.get(Proposal, proposal_id)
            raw_ec = proposal.evaluation_criteria_json if proposal is not None else None
        if raw_ec:
            criteria_dict = json.loads(raw_ec)
    except Exception:
        criteria_dict = None
    evaluation_criteria_block = format_evaluation_criteria_block(criteria_dict)

    prefix_a = build_a_prefix(
        profile_json=profile_json,
        kb_context=kb_context,
        outline_text=outline_text,
        compliance_text=compliance_text,
        gaps_text=gaps_text,
        evaluation_criteria_block=evaluation_criteria_block,
    )
    prefix_b = build_b_prefix(
        profile_json=profile_json,
        outline_text=outline_text,
        compliance_text=compliance_text,
        rfp_text=rfp_text,
    )
    return prefix_a, prefix_b


def _run_preflight(
    label: str,
    check_fn,
    section: dict,
    proposal_id: int,
    failures: list[str] | None = None,
) -> list:
    """Run one pre-flight without preventing the independent checks.

    A raised check returns no findings, but its error is appended to
    ``failures`` so the composite section attempt cannot be certified clean.
    """
    try:
        return list(check_fn(section["pk"]))
    except Exception as exc:
        log.exception(
            "%s pre-flight failed for section %s (proposal %d)",
            label,
            section["section_id"],
            proposal_id,
        )
        _set_stage(
            proposal_id,
            f"⚠ {label.capitalize()} pre-flight failed on "
            f"{section['section_id']}: "
            f"{type(exc).__name__}: {str(exc)[:140]}",
            status="failed",
        )
        if failures is not None:
            failures.append(
                f"{label} pre-flight: {type(exc).__name__}: {str(exc)[:300]}"
            )
        return []


def _execute_review_one_section(
    section: dict,
    prefix_a: str,
    prefix_b: str,
    proposal_id: int,
) -> SectionReviewResult:
    """Run all section reviewers and return explicit completion state.

    Each reviewer's failure is isolated — a Reviewer A failure doesn't
    block Reviewer B and vice versa, but either error fails the composite pass.
    """
    # Late import keeps the pre-flight services off the module-load
    # path until first use (and avoids any tightening of the import graph
    # between agents/services/jobs).
    from app.services.citation_check import check_section_citations
    from app.services.grounding_check import check_section_credentials_grounded
    from app.services.preflight_checks import (
        check_compliance_coverage,
        check_section_credentials_allowlisted,
    )

    section_pk = section["pk"]
    next_pass = get_pass_number_for_section(section_pk) + 1
    clear_unresolved_for_section(section_pk)
    failures: list[str] = []

    # `applied_gaps` is a list of gap_id strings (from
    # shortfall_mitigations_applied_json on the section) — flat list.
    applied_gap_ids = list(section.get("applied_gaps") or [])

    # Pre-flight pass — deterministic checks that run BEFORE Reviewer A
    # to catch mechanical bugs the LLM occasionally misses:
    #   1. Citation legitimacy: past-perf claims sourced to non-citable
    #      KB classes + claim text grounded in the cited doc
    #      (FAR-actionable).
    #   2. Compliance coverage: assigned requirement_id whose salient
    #      terms are largely absent from the draft.
    #   3. Credential grounding: certs claimed in the draft cross-
    #      checked against company_profile (every pass) + web-grounded
    #      via Gemini (pass 1 only) for credentials that ARE in the
    #      profile, so a stale/wrong profile entry surfaces as a MAJOR.
    # All pre-flight findings persist as reviewer_agent='A' on the same
    # pass so the auto-loop's normal flow (auto-accept CRITICAL/MAJOR →
    # directive to writer) handles them. Reviewer A still runs after.
    # Each check is wrapped independently via _run_preflight so one
    # failure doesn't block the others.
    n_a = 0
    preflight_findings: list = []
    preflight_findings += _run_preflight(
        "citation", check_section_citations, section, proposal_id, failures,
    )
    preflight_findings += _run_preflight(
        "coverage", check_compliance_coverage, section, proposal_id, failures,
    )
    preflight_findings += _run_preflight(
        "grounding", check_section_credentials_grounded, section, proposal_id,
        failures,
    )
    preflight_findings += _run_preflight(
        "credential allowlist", check_section_credentials_allowlisted,
        section, proposal_id, failures,
    )
    if preflight_findings:
        try:
            n_pf = persist_findings(
                proposal_section_pk=section_pk,
                reviewer_agent="A",
                pass_number=next_pass,
                findings=preflight_findings,
            )
            n_a += n_pf
            log.info(
                "pre-flight (citation + coverage + grounding + credential allowlist): "
                "section %s -> %d finding(s)",
                section["section_id"], n_pf,
            )
        except Exception as exc:
            log.exception(
                "pre-flight finding persistence failed for section %s "
                "(proposal %d)",
                section["section_id"], proposal_id,
            )
            failures.append(
                "pre-flight persistence: "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            )
            _set_stage(
                proposal_id,
                f"Pre-flight finding persistence failed on "
                f"{section['section_id']}: {type(exc).__name__}: "
                f"{str(exc)[:140]}",
                status="failed",
            )

    # Run Reviewer A and Reviewer B in PARALLEL. They're independent
    # passes on the same draft (different models, different concerns —
    # Opus for compliance/honesty, Gemini for persuasion/voice). Running
    # serially burns wall time for no quality reason. Persist their
    # findings back in the main thread so DB writes stay serialized.
    # Per-reviewer failure is isolated: if one raises, the other still
    # persists its results.
    def _run_a():
        return review_a(
            proposal_id=proposal_id,
            section_id=section["section_id"],
            section_title=section["section_title"],
            page_limit=section.get("page_limit"),
            word_limit=section.get("word_limit"),
            compliance_item_ids=section.get("compliance_items_addressed") or [],
            assigned_gap_ids=applied_gap_ids,
            draft_markdown=section.get("draft_md") or "",
            citations=section.get("citations") or [],
            needs_human_placeholders=section.get("needs_human") or [],
            applied_gap_ids=applied_gap_ids,
            cached_prefix=prefix_a,
            amended_items=section.get("amended_items_for_section") or [],
        )

    def _run_b():
        return review_b(
            proposal_id=proposal_id,
            section_id=section["section_id"],
            section_title=section["section_title"],
            section_brief=section.get("section_brief") or "",
            page_limit=section.get("page_limit"),
            word_limit=section.get("word_limit"),
            draft_markdown=section.get("draft_md") or "",
            cached_prefix=prefix_b,
        )

    findings_a = None
    findings_b = None
    err_a: Exception | None = None
    err_b: Exception | None = None
    with ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix=f"rev-ab-sec{section_pk}",
    ) as ex:
        fut_a = ex.submit(_run_a)
        fut_b = ex.submit(_run_b)
        try:
            findings_a = fut_a.result()
        except Exception as exc:
            err_a = exc
        try:
            findings_b = fut_b.result()
        except Exception as exc:
            err_b = exc

    if err_a is None:
        try:
            n_a += persist_findings(
                proposal_section_pk=section_pk,
                reviewer_agent="A",
                pass_number=next_pass,
                findings=findings_a or [],
            )
        except Exception as exc:
            err_a = exc

    if err_a is not None:
        log.exception(
            "reviewer_a failed for section %s (proposal %d): %s",
            section["section_id"],
            proposal_id,
            err_a,
            exc_info=err_a,
        )
        _set_stage(
            proposal_id,
            f"⚠ Reviewer A failed on {section['section_id']}: "
            f"{type(err_a).__name__}: {str(err_a)[:140]}",
            status="failed",
        )

        failures.append(
            f"Reviewer A: {type(err_a).__name__}: {str(err_a)[:300]}"
        )

    n_b = 0
    if err_b is None:
        try:
            n_b = persist_findings(
                proposal_section_pk=section_pk,
                reviewer_agent="B",
                pass_number=next_pass,
                findings=findings_b or [],
            )
        except Exception as exc:
            err_b = exc

    if err_b is not None:
        log.exception(
            "reviewer_b failed for section %s (proposal %d): %s",
            section["section_id"],
            proposal_id,
            err_b,
            exc_info=err_b,
        )
        _set_stage(
            proposal_id,
            f"⚠ Reviewer B failed on {section['section_id']}: "
            f"{type(err_b).__name__}: {str(err_b)[:140]}",
            status="failed",
        )

        failures.append(
            f"Reviewer B: {type(err_b).__name__}: {str(err_b)[:300]}"
        )

    return SectionReviewResult(n_a, n_b, tuple(failures))


def _review_one_section(
    section: dict,
    prefix_a: str,
    prefix_b: str,
    proposal_id: int,
) -> SectionReviewResult:
    """Run and audit one complete section review.

    The synthetic coverage row becomes COMPLETED only after every pre-flight,
    both provider calls, and every finding write succeeds.  An incomplete
    attempt can therefore never be interpreted as a clean section.
    """
    section_pk = int(section["pk"])
    revision = int(section.get("revision") or 0)
    marker_id = _start_review_coverage_attempt(
        proposal_id, section_pk, revision,
    )
    try:
        result = _execute_review_one_section(
            section, prefix_a, prefix_b, proposal_id,
        )
    except Exception as exc:
        _finish_review_coverage_attempt(
            marker_id,
            succeeded=False,
            error_text=f"{type(exc).__name__}: {str(exc)}",
        )
        raise

    try:
        _finish_review_coverage_attempt(
            marker_id,
            succeeded=result.succeeded,
            error_text="; ".join(result.failures),
        )
    except Exception as exc:
        log.exception(
            "failed to finalize review coverage for section %s "
            "(proposal %d)",
            section.get("section_id", section_pk), proposal_id,
        )
        return SectionReviewResult(
            result.n_findings_a,
            result.n_findings_b,
            result.failures + (
                "review coverage persistence: "
                f"{type(exc).__name__}: {str(exc)[:300]}",
            ),
        )
    return result


def run_reviewer_loop(proposal_id: int) -> None:
    """Run Reviewer A + B across every drafted, non-cost-deferred section.

    Status flow: DRAFT_READY → REVIEWING → DRAFT_READY (with findings now
    populated). User reviews the findings and decides what to do.
    """
    require_proposal_mutable(proposal_id, operation="run proposal review")
    log.info("reviewer loop starting for proposal %d", proposal_id)
    try:
        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p:
                p.status = ProposalStatus.REVIEWING

        _set_stage(proposal_id, "Building reviewer context (profile + KB + outline)…")
        snap = _snapshot_review_inputs(proposal_id)

        eligible = [s for s in snap["sections"] if s.get("draft_md") and not s.get("requires_cost_analysis")]
        if not eligible:
            _set_stage(
                proposal_id,
                "No drafted sections to review. Run Writer Team first.",
                status="failed",
            )
            with session_scope() as db:
                p = db.get(Proposal, proposal_id)
                if p:
                    p.status = ProposalStatus.DRAFT_READY
            return

        prefix_a, prefix_b = _build_prefixes(proposal_id, snap)

        n_total = len(eligible)
        total_findings = 0
        failed_sections: list[str] = []
        for idx, section in enumerate(eligible, 1):
            _set_stage(
                proposal_id,
                f"Reviewing section {idx}/{n_total}: {section['section_id']} "
                f"{section['section_title']} (Opus + Gemini)…",
            )
            result = _review_one_section(
                section, prefix_a, prefix_b, proposal_id,
            )
            total_findings += result.n_findings_a + result.n_findings_b
            if not result.succeeded:
                failed_sections.append(section["section_id"])

        # Auto-accept pending findings per the configured severity
        # floor. Default config (floor=None) accepts everything so the
        # user only has to dismiss what they disagree with rather than
        # accept every single finding individually.
        accept_summary = ""
        try:
            from app.services.findings import bulk_accept_pending_findings

            floor = get_settings().auto_accept_findings_severity_floor or None
            if floor and floor.lower() in ("off", "none", "disabled"):
                floor_for_accept: str | None = "CRITICAL_BUT_NONE_MATCH"
                # Sentinel to skip auto-accept entirely.
            else:
                floor_for_accept = floor  # None = accept everything
            if floor_for_accept != "CRITICAL_BUT_NONE_MATCH":
                counts = bulk_accept_pending_findings(
                    proposal_id,
                    severity_floor=floor_for_accept,
                )
                if counts["accepted"]:
                    accept_summary = (
                        f" — auto-accepted {counts['accepted']} "
                        f"(C={counts['by_severity']['CRITICAL']} "
                        f"M={counts['by_severity']['MAJOR']} "
                        f"m={counts['by_severity']['MINOR']})"
                    )
        except Exception:
            log.exception(
                "auto-accept pending findings failed for proposal %d "
                "(non-fatal — review tab still shows them as pending)",
                proposal_id,
            )

        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p:
                p.status = ProposalStatus.DRAFT_READY

        if failed_sections:
            _set_stage(
                proposal_id,
                f"Review incomplete: {len(failed_sections)}/{n_total} "
                f"section(s) had a reviewer, pre-flight, or persistence "
                f"failure ({', '.join(failed_sections)}). "
                f"{total_findings} finding(s) from completed components were "
                f"preserved{accept_summary}. Re-run Reviewer Findings before "
                f"approval.",
                status="failed",
            )
        else:
            _set_stage(
                proposal_id,
                f"Review complete: {total_findings} finding(s) across {n_total} "
                f"section(s){accept_summary}. Open the Findings tab to act on them.",
            )

    except Exception:
        log.exception("reviewer loop failed for proposal %d", proposal_id)
        _set_stage(
            proposal_id,
            "Reviewer loop failed — check logs.",
            status="failed",
        )
        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p and p.status == ProposalStatus.REVIEWING:
                p.status = ProposalStatus.DRAFT_READY


def run_reviewer_for_section(proposal_id: int, proposal_section_pk: int) -> None:
    """Re-run Reviewer A + B on ONE section. Used after the user applies
    accepted findings to verify the issues were addressed."""
    require_proposal_mutable(
        proposal_id, operation="review proposal section",
    )
    log.info(
        "reviewer re-run for section pk=%d (proposal %d)",
        proposal_section_pk,
        proposal_id,
    )
    try:
        snap = _snapshot_review_inputs(proposal_id)
        section = next(
            (s for s in snap["sections"] if s["pk"] == proposal_section_pk),
            None,
        )
        if section is None or not section.get("draft_md"):
            _set_stage(
                proposal_id,
                f"Section pk={proposal_section_pk} not found or not yet drafted.",
                status="failed",
            )
            return
        if section.get("requires_cost_analysis"):
            _set_stage(
                proposal_id,
                f"Section {section['section_id']} is cost-deferred — reviewer skipped.",
            )
            return

        _set_stage(
            proposal_id,
            f"Re-reviewing section {section['section_id']} {section['section_title']}…",
        )
        prefix_a, prefix_b = _build_prefixes(proposal_id, snap)
        result = _review_one_section(
            section, prefix_a, prefix_b, proposal_id,
        )
        n_a = result.n_findings_a
        n_b = result.n_findings_b

        # Same auto-accept policy as the loop variant — keep behavior
        # identical so the user gets the same triaged-up-front view
        # whether they re-review one section or all of them.
        accept_summary = ""
        try:
            from app.services.findings import bulk_accept_pending_findings

            floor = get_settings().auto_accept_findings_severity_floor or None
            if not (floor and floor.lower() in ("off", "none", "disabled")):
                counts = bulk_accept_pending_findings(
                    proposal_id,
                    severity_floor=floor,
                )
                if counts["accepted"]:
                    accept_summary = f" — auto-accepted {counts['accepted']}"
        except Exception:
            log.exception(
                "auto-accept after section re-review failed (non-fatal)",
            )

        if result.succeeded:
            _set_stage(
                proposal_id,
                f"Section {section['section_id']} re-reviewed: "
                f"{n_a} A-finding(s), {n_b} B-finding(s){accept_summary}.",
            )
        else:
            _set_stage(
                proposal_id,
                f"Section {section['section_id']} review incomplete: "
                f"{'; '.join(result.failures)}. Re-run before approval.",
                status="failed",
            )

    except Exception:
        log.exception(
            "reviewer re-run failed for section pk=%d",
            proposal_section_pk,
        )
        _set_stage(
            proposal_id,
            "Section re-review failed — check logs.",
            status="failed",
        )


def spawn_reviewer_loop(proposal_id: int) -> threading.Thread:
    t = threading.Thread(
        target=run_reviewer_loop,
        args=(proposal_id,),
        name=f"reviewer-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


def spawn_reviewer_for_section(proposal_id: int, proposal_section_pk: int) -> threading.Thread:
    t = threading.Thread(
        target=run_reviewer_for_section,
        args=(proposal_id, proposal_section_pk),
        name=f"reviewer-sec-{proposal_section_pk}",
        daemon=True,
    )
    t.start()
    return t


# ---- Auto Review-Revise Loop --------------------------------------------

# Default cap. Tuned for cost: 6 passes × ~18 sections × ~$5/section =
# worst-case ~$540 per proposal. Most sections converge in 2-3 passes.
# Tuned up from 4 to give Reviewer B's MINOR findings + escalation passes
# enough room to drive convergence.
_DEFAULT_MAX_PASSES = 6

# Severities the auto-loop tries to resolve. CRITICAL/MAJOR only —
# MINOR findings (mostly stylistic suggestions from Reviewer B) often
# whack-a-mole when regenerated against (writer fixes 5 minor issues,
# reviewer flags 5 different minor issues on the new draft) and rarely
# improve the proposal's competitive position. Leaving them as
# unresolved findings on the Findings tab gives the user a clean
# manual-review queue instead. A section with zero CRITICAL/MAJOR
# findings on a given pass converges and exits — no further Opus /
# Gemini calls or Writer regenerates burned. Saves ~30-50% of typical
# auto-loop cost on RFPs where most issues are stylistic.
_AUTO_REVISE_SEVERITIES = ("CRITICAL", "MAJOR")

# How many consecutive non-progress passes trigger stuck-exit. With
# Reviewer B flagging stylistic MINOR issues, single-pass count compare
# is noisy (writer fixes 5, reviewer flags 5 new ones — same count,
# different findings). Require 2 consecutive flat passes before bailing.
_STUCK_THRESHOLD_PASSES = 2

# At which pass to start escalating the directive (telling the writer to
# delete or [NEEDS_HUMAN]-wrap content the reviewers keep flagging).
_ESCALATION_AFTER_PASS = 2

_ESCALATION_DIRECTIVE_SUFFIX = """

--- ESCALATION (revision pass {pass_num}) ---
Previous attempts did not fully resolve all findings, and reviewers keep flagging the same kinds of issues. For findings that recur:

1. STOP rephrasing the same claim. If reviewers consistently flag a sentence, the underlying claim isn't supportable — re-wording won't fix it.

2. Pick ONE of these per recurring finding:
   a. DELETE the offending text entirely. The section will be shorter but cleaner.
   b. Wrap the offending text in [NEEDS_HUMAN: <description of what's needed>] so the user sees it inline and can supply the missing fact, citation, or commitment from the Draft tab.
   c. RESTRUCTURE — replace the claim with a fundamentally different argument that DOES have evidence backing it (different past-performance example, different capability angle, different framing).

3. Honesty rules still apply. Do NOT fabricate citations or content to silence the reviewer. [NEEDS_HUMAN] is the right tool when honest framing isn't available.

Apply this aggressively. The user prefers an honest, shorter section with [NEEDS_HUMAN] markers over a polished section with un-resolvable critical findings."""


def _refresh_section_snapshot(proposal_id: int, section_pk: int) -> dict | None:
    """Re-snapshot ONE section's current state — used between passes to
    pick up the latest draft after a writer regenerate."""
    with SessionLocal() as db:
        s = db.get(ProposalSection, section_pk)
        if s is None or s.proposal_id != proposal_id:
            return None
        return {
            "pk": s.id,
            "section_id": s.section_id,
            "section_title": s.section_title,
            "section_order": s.section_order,
            "section_brief": s.section_brief,
            "page_limit": s.page_limit,
            "word_limit": s.word_limit,
            "requires_cost_analysis": bool(s.requires_cost_analysis),
            "draft_md": s.draft_text_markdown,
            "revision": s.current_revision_number or 0,
            "citations": list(s.citations_json or []),
            "needs_human": list(s.needs_human_placeholders_json or []),
            "applied_gaps": list(s.shortfall_mitigations_applied_json or []),
            "compliance_items_addressed": list(s.compliance_items_addressed_json or []),
        }


def _process_one_section(
    *,
    section: dict,
    prefix_a: str,
    prefix_b: str,
    proposal_id: int,
    cancel_event: threading.Event,
    max_passes: int,
    section_idx: int,
    n_total: int,
) -> str:
    """Run the full review-revise pass cycle on ONE section.

    Returns one of:
      "clean"          — section converged with zero findings
      "capped"         — pass cap reached; some findings remain
      "stuck"          — N consecutive no-progress passes; some remain
      "cancelled"      — cancel_event was set; exited at a checkpoint
      "cost_deferred"  — section was flagged cost-deferred mid-loop
      "no_draft"       — section lost its draft (regenerate failed?)
      "review_failed"  — a reviewer, pre-flight, or finding write failed
      "revision_failed" — writer could not produce a replacement draft
      "section_busy"   — another worker owns the section; skipped fail-closed

    Worker-safe: each call uses its own session_scope + its own LLM
    sessions. The shared mutable state is the cancel_event (thread-safe
    via threading.Event) and the active-sections registry in
    cancellation.py (lock-protected).
    """
    # Late import: keeps writer.py / reviewer.py off each other's import
    # path until call time.
    from app.jobs.writer import run_writer_for_section

    section_pk = section["pk"]
    section_label = f"{section['section_id']} {section['section_title']}"

    # Mark in-flight so the UI can render an indicator on this section
    # and the Apply-accepted-findings safety check refuses concurrent
    # writer regenerates.
    acquired = add_active_section(proposal_id, section_pk)
    if not acquired:
        _set_stage(
            proposal_id,
            f"[{section_idx}/{n_total}] {section_label} — skipped because "
            "another writer or polish worker is already changing it. Re-run "
            "Reviewer Findings after that work completes.",
            status="failed",
        )
        return "section_busy"
    try:
        prev_n_to_revise: int | None = None
        consecutive_no_progress = 0

        for pass_num in range(1, max_passes + 1):
            if cancel_event.is_set():
                return "cancelled"

            # Pull the latest draft — may have been regenerated in a prior pass.
            latest = _refresh_section_snapshot(proposal_id, section_pk)
            if latest is None or not latest.get("draft_md"):
                return "no_draft"

            # Honor mid-loop user toggle: if the user marked this section
            # cost-deferred while we were running, skip it.
            if latest.get("requires_cost_analysis"):
                _set_stage(
                    proposal_id,
                    f"[{section_idx}/{n_total}] {section_label} — "
                    f"now flagged cost-deferred; skipping for the "
                    f"Cost Analysis Agent.",
                )
                return "cost_deferred"

            _set_stage(
                proposal_id,
                f"[{section_idx}/{n_total}] {section_label} — pass {pass_num}/{max_passes}: reviewing…",
            )
            review_result = _review_one_section(
                latest, prefix_a, prefix_b, proposal_id,
            )
            if not review_result.succeeded:
                _set_stage(
                    proposal_id,
                    f"[{section_idx}/{n_total}] {section_label} — "
                    f"pass {pass_num}: review incomplete; "
                    f"{'; '.join(review_result.failures)}. "
                    f"Re-run before approval.",
                    status="failed",
                )
                return "review_failed"

            if cancel_event.is_set():
                return "cancelled"

            pending = get_unresolved_findings_for_section(section_pk)
            to_revise = [f for f in pending if f["severity"] in _AUTO_REVISE_SEVERITIES]

            if not to_revise:
                # Convergence: no CRITICAL/MAJOR findings remain. Any
                # MINOR findings stay surfaced on the Findings tab for
                # the user to triage manually — auto-revising MINOR
                # tends to produce whack-a-mole iteration without
                # measurable quality gain.
                n_minor_remaining = sum(1 for f in pending if f["severity"] == "MINOR")
                detail = "no critical/major findings" + (
                    f"; {n_minor_remaining} minor finding(s) left for review" if n_minor_remaining else ""
                )
                _set_stage(
                    proposal_id,
                    f"[{section_idx}/{n_total}] {section_label} — pass {pass_num}: clean ({detail}).",
                )
                return "clean"

            if pass_num >= max_passes:
                _set_stage(
                    proposal_id,
                    f"[{section_idx}/{n_total}] {section_label} — "
                    f"hit pass cap ({max_passes}) with "
                    f"{len(to_revise)} unresolved finding(s).",
                )
                return "capped"

            # Stuck detection — finding count didn't decrease after a
            # regenerate. Require N consecutive non-progress passes so
            # whack-a-mole (fix 5 / surface 5 different ones) doesn't
            # kill an actually-progressing loop.
            if prev_n_to_revise is not None and len(to_revise) >= prev_n_to_revise:
                consecutive_no_progress += 1
            else:
                consecutive_no_progress = 0

            if consecutive_no_progress >= _STUCK_THRESHOLD_PASSES:
                _set_stage(
                    proposal_id,
                    f"[{section_idx}/{n_total}] {section_label} — "
                    f"stuck after {consecutive_no_progress} "
                    f"consecutive no-progress passes; "
                    f"{len(to_revise)} unresolved. "
                    f"Surfacing for user review.",
                )
                return "stuck"

            # Auto-accept all + regenerate. Build the directive and (on
            # pass 3+) append the escalation suffix telling the writer to
            # delete or [NEEDS_HUMAN]-wrap recurring claims rather than
            # rephrase them.
            for f in to_revise:
                accept_finding(f["id"])
            directive = build_directive_from_findings(to_revise)
            if pass_num > _ESCALATION_AFTER_PASS:
                directive += _ESCALATION_DIRECTIVE_SUFFIX.format(
                    pass_num=pass_num,
                )
            escalation_tag = " (escalated)" if pass_num > _ESCALATION_AFTER_PASS else ""
            _set_stage(
                proposal_id,
                f"[{section_idx}/{n_total}] {section_label} — "
                f"pass {pass_num}: writer applying "
                f"{len(to_revise)} fix(es){escalation_tag}…",
            )
            regenerated = run_writer_for_section(
                proposal_id, section_pk,
                user_directive=directive,
                pass_num=pass_num,
            )
            if not regenerated:
                _set_stage(
                    proposal_id,
                    f"[{section_idx}/{n_total}] {section_label} — "
                    f"pass {pass_num}: writer regeneration failed; "
                    f"findings remain unresolved.",
                    status="failed",
                )
                return "revision_failed"
            mark_findings_resolved(
                [f["id"] for f in to_revise],
                pass_num,
            )

            prev_n_to_revise = len(to_revise)

        # Defensive — pass loop should always return. If we fall out
        # without hitting return, treat as capped.
        return "capped"
    finally:
        remove_active_section(proposal_id, section_pk)


def _run_consistency_pass(proposal_id: int) -> None:
    """Run Reviewer C across all drafted sections of a proposal. Each
    inconsistency persists ONE ReviewerFinding row per affected section
    so the conflict shows up on every involved section's Findings tab.

    Uses reviewer_agent='C' and category='cross_section_inconsistency'
    so the UI can distinguish these findings from per-section reviewer
    output. Pass number is the highest existing pass + 1 for each
    section (so the finding sits at the same level as the latest
    Reviewer A/B output).
    """
    from app.agents.consistency_checker import (
        check_proposal_consistency,
    )
    from app.agents.reviewer_a import ReviewerFindingDraft

    snap = _snapshot_review_inputs(proposal_id)
    drafted_sections = [
        s
        for s in snap["sections"]
        if (s.get("draft_md") or "").strip()
        and not s.get("requires_cost_analysis")
        and not s.get("excluded_from_draft")
    ]
    if len(drafted_sections) < 2:
        return

    _set_stage(
        proposal_id,
        f"Cross-section consistency check (Haiku) over {len(drafted_sections)} drafted section(s)…",
    )

    findings = check_proposal_consistency(
        proposal_id=proposal_id,
        sections=drafted_sections,
    )

    if not findings:
        _set_stage(
            proposal_id,
            f"Cross-section consistency check: no inconsistencies "
            f"found across {len(drafted_sections)} section(s).",
        )
        return

    # Build a section_id → pk lookup so we can target each finding's
    # affected sections by primary key. Sections referenced by ID but
    # not present (e.g., the model hallucinated an ID) are dropped with
    # a warning — better to lose a finding than to crash the loop.
    sec_id_to_pk: dict[str, int] = {s["section_id"]: s["pk"] for s in drafted_sections}

    n_persisted = 0
    for f in findings:
        finding_text = f"Cross-section inconsistency — {f.subject}.\n\n{f.description}"
        suggested_fix = (
            f"{f.suggested_resolution}\n\n"
            f"Affected sections: {', '.join(f.affected_section_ids)}. "
            f"This finding is mirrored on every affected section so "
            f"you'll see it from each. Pick a canonical value, then "
            f"resolve manually (per-section Regenerate or inline edit) "
            f"OR accept the finding here and re-run the auto-loop "
            f"after picking the canonical value to apply via Writer."
        )
        for sec_id in f.affected_section_ids:
            section_pk = sec_id_to_pk.get(sec_id)
            if section_pk is None:
                log.warning(
                    "consistency_pass: finding refs unknown section_id "
                    "%r — dropping for that section. subject=%r",
                    sec_id,
                    f.subject,
                )
                continue
            next_pass = get_pass_number_for_section(section_pk) + 1
            persist_findings(
                proposal_section_pk=section_pk,
                reviewer_agent="C",
                pass_number=next_pass,
                findings=[
                    ReviewerFindingDraft(
                        severity=f.severity,
                        category="cross_section_inconsistency",
                        finding_text=finding_text,
                        suggested_fix=suggested_fix,
                    )
                ],
            )
            n_persisted += 1

    _set_stage(
        proposal_id,
        f"Cross-section consistency check: {len(findings)} "
        f"inconsistency(ies) found, {n_persisted} finding row(s) "
        f"persisted across affected sections.",
    )


def run_auto_review_revise_loop(proposal_id: int, max_passes: int = _DEFAULT_MAX_PASSES) -> None:
    """Run Reviewer A+B and Writer Team across every drafted section,
    PARALLELIZED across `settings.auto_loop_workers` workers. Each worker
    iterates its own section through the full pass cycle (review → if
    findings, regenerate → repeat) with stuck detection and pass-3+
    escalation.

    Stop conditions per section (returned by `_process_one_section`):
    - clean   — zero findings of any severity
    - capped  — pass cap reached; remaining findings preserved
    - stuck   — N consecutive no-progress passes
    - cancelled — cancel_event set mid-loop
    - cost_deferred — section was toggled cost-deferred mid-loop
    - no_draft — section lost its draft (rare; surface for inspection)
    - review_failed — reviewer/pre-flight/finding persistence was incomplete
    - revision_failed — writer could not produce a replacement draft
    - section_busy — another worker owned the section, so review was skipped

    Status flow: DRAFT_READY → REVIEWING → DRAFT_READY (with any remaining
    findings populated). The user decides what to do next.

    Cancel semantics: workers check `cancel_event.is_set()` at every safe
    checkpoint (start of pass + after each LLM call). The orchestrator
    waits for all in-flight workers to finish naturally — they typically
    exit within ~30-90s of the cancel signal (bounded by the longest
    in-flight LLM call).
    """
    require_proposal_mutable(
        proposal_id, operation="run auto review-revise loop",
    )
    settings = get_settings()
    workers = max(1, int(settings.auto_loop_workers or 1))
    log.info(
        "auto review-revise loop starting for proposal %d (max_passes=%d, workers=%d)",
        proposal_id,
        max_passes,
        workers,
    )
    cancel_event = register_cancel(JOB_AUTO_REVIEW, proposal_id)
    if cancel_event is None:
        # Another auto-loop is already running for this proposal. Concurrent
        # runs corrupt the registry + double-write findings; refuse to start.
        log.warning(
            "auto review-revise loop refusing to start for proposal %d — another loop is already running.",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            "Auto loop refused to start: another auto-review loop is "
            "already running for this proposal. Cancel the running one "
            "first, or restart the app (Ctrl+C in terminal) to clear it.",
            status="failed",
        )
        return

    cancelled = False
    try:
        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p:
                p.status = ProposalStatus.REVIEWING

        snap = _snapshot_review_inputs(proposal_id)
        eligible = [s for s in snap["sections"] if s.get("draft_md") and not s.get("requires_cost_analysis")]
        if not eligible:
            _set_stage(
                proposal_id,
                "No drafted sections to review.",
                status="failed",
            )
            with session_scope() as db:
                p = db.get(Proposal, proposal_id)
                if p:
                    p.status = ProposalStatus.DRAFT_READY
            return

        prefix_a, prefix_b = _build_prefixes(proposal_id, snap)

        n_total = len(eligible)
        n_clean = 0
        n_capped = 0
        n_stuck = 0
        n_review_failed = 0
        n_revision_failed = 0
        n_worker_failed = 0
        n_section_busy = 0

        _set_stage(
            proposal_id,
            f"Auto review-revise starting · {n_total} section(s) · {workers} worker(s) in parallel",
        )

        # Submit every eligible section to the executor. Each worker
        # processes one section's full pass cycle and returns its outcome.
        # Workers exit early at their next checkpoint when cancel_event is
        # set; the orchestrator then drains the rest as they complete.
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"auto-rev-{proposal_id}",
        ) as executor:
            future_to_section = {
                executor.submit(
                    _process_one_section,
                    section=section,
                    prefix_a=prefix_a,
                    prefix_b=prefix_b,
                    proposal_id=proposal_id,
                    cancel_event=cancel_event,
                    max_passes=max_passes,
                    section_idx=section_idx,
                    n_total=n_total,
                ): section
                for section_idx, section in enumerate(eligible, 1)
            }
            for future in as_completed(future_to_section):
                section = future_to_section[future]
                try:
                    result = future.result()
                except Exception:
                    n_worker_failed += 1
                    log.exception(
                        "auto-loop worker failed for section %s (proposal %d)",
                        section.get("section_id"),
                        proposal_id,
                    )
                    _set_stage(
                        proposal_id,
                        f"⚠ Worker for {section.get('section_id', '?')} "
                        f"raised an exception — see logs.",
                        status="failed",
                    )
                    continue

                if result == "clean":
                    n_clean += 1
                elif result == "capped":
                    n_capped += 1
                elif result == "stuck":
                    n_stuck += 1
                elif result == "cancelled":
                    cancelled = True
                elif result == "review_failed":
                    n_review_failed += 1
                elif result == "revision_failed":
                    n_revision_failed += 1
                elif result == "section_busy":
                    n_section_busy += 1
                # cost_deferred / no_draft: don't bump any tally; they're
                # surfaced via stage messages already.

        # A cancel can arrive in the narrow window after the last worker
        # returns but before the consistency pass starts. Worker outcomes alone
        # would miss that signal and launch one more costly provider call.
        cancelled = cancelled or cancel_event.is_set()

        # Cross-section consistency pass — runs once after the per-section
        # workers all finish (whether converged, capped, or stuck). Catches
        # conflicts that single-section reviewers can't see (different
        # numbers for the same thing across sections, conflicting names,
        # etc.). Best-effort; a failure here doesn't roll back the loop.
        consistency_error: str | None = None
        if not cancelled and not n_section_busy:
            try:
                _run_consistency_pass(proposal_id)
            except Exception as exc:
                log.exception(
                    "consistency check failed for proposal %d — continuing with per-section findings only",
                    proposal_id,
                )
                consistency_error = str(exc) or type(exc).__name__

        # Synchronous provider calls cannot be interrupted mid-call. If cancel
        # arrived during consistency, still honor it for terminal status.
        cancelled = cancelled or cancel_event.is_set()

        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p:
                p.status = ProposalStatus.DRAFT_READY

        if cancelled:
            _set_stage(
                proposal_id,
                f"🛑 Auto review-revise CANCELLED by user. "
                f"{n_clean} clean, {n_stuck} stuck, {n_capped} capped, "
                f"{n_review_failed} review failures, "
                f"{n_revision_failed} writer failures, "
                f"{n_section_busy} busy-section skips, "
                f"{n_worker_failed} worker failures before stop. "
                f"Open the Findings tab to see what was completed.",
                status="cancelled",
            )
        else:
            parts = [f"Auto review-revise complete: {n_clean}/{n_total} fully clean"]
            if n_stuck:
                parts.append(f"{n_stuck} stuck")
            if n_capped:
                parts.append(f"{n_capped} hit pass cap")
            if n_review_failed:
                parts.append(f"{n_review_failed} incomplete section review(s)")
            if n_revision_failed:
                parts.append(
                    f"{n_revision_failed} writer regeneration(s) failed"
                )
            if n_worker_failed:
                parts.append(f"{n_worker_failed} worker(s) failed")
            if n_section_busy:
                parts.append(
                    f"{n_section_busy} section(s) skipped because another "
                    "worker owned them"
                )
            if (
                n_stuck
                or n_capped
                or n_review_failed
                or n_revision_failed
                or n_worker_failed
                or n_section_busy
            ):
                parts.append(
                    "Open the Findings tab — un-resolvable issues need human review."
                )
            else:
                parts.append("All sections converged with zero findings.")
            if consistency_error:
                parts.append(
                    "Cross-section consistency check FAILED — per-section "
                    "findings are reliable, but inter-section conflicts "
                    "were not verified this run. See log for details."
                )
            _set_stage(
                proposal_id,
                " · ".join(parts),
                status=(
                    "failed"
                    if (
                        n_revision_failed
                        or n_review_failed
                        or n_worker_failed
                        or n_section_busy
                        or consistency_error
                    )
                    else "completed"
                ),
            )

    except Exception:
        log.exception(
            "auto review-revise loop failed for proposal %d",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            "Auto review-revise loop failed — check logs.",
            status="failed",
        )
        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p and p.status == ProposalStatus.REVIEWING:
                p.status = ProposalStatus.DRAFT_READY
    finally:
        # Each worker owns and removes its section marker in
        # _process_one_section's finally block. Do not blanket-clear the
        # proposal here: a manual writer/final-polish worker may legitimately
        # be active for the same proposal.
        unregister_cancel(JOB_AUTO_REVIEW, proposal_id, cancel_event)


def spawn_auto_review_revise_loop(
    proposal_id: int,
    max_passes: int = _DEFAULT_MAX_PASSES,
) -> threading.Thread:
    t = threading.Thread(
        target=run_auto_review_revise_loop,
        args=(proposal_id, max_passes),
        name=f"auto-review-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t
