"""Writer Team orchestration — drafts ProposalSections in parallel.

Triggered by the user clicking 'Approve Outline & Begin Drafting'. Reads
the persisted outline, builds a cached prefix once, then drafts each
eligible section concurrently against that prefix.

Parallel via ThreadPoolExecutor with `settings.writer_workers` workers —
same pattern as the Shortfall Strategist. Each worker runs draft_section
in its own thread; persistence happens back in the main thread inside
the as_completed loop so DB writes stay serialized. Per-section failures
are isolated (logged + a stage banner; remaining sections continue).

Per-section regenerate uses run_writer_for_section() instead of the
full run_writer_team().
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select

from app.agents.writer_team import (
    build_cached_prefix,
    draft_section,
    format_held_certifications_block,
)
from app.config import get_settings
from app.core.company_profile import get_company_profile
from app.core.decisions import format_decisions_for_prompt
from app.core.enums import ComplianceStatus, ProposalStatus
from app.core.teaming_partners import get_teaming_partners
from app.db.session import SessionLocal, session_scope
from app.models import (
    ComplianceMatrixItem,
    GapAnalysis,
    Proposal,
    ProposalSection,
    RfpPackageDocument,
)
from app.services.kb_context import build_section_kb_context
from app.services.needs_human import (
    auto_resolve_obvious_placeholders,
    auto_resolve_via_llm,
    carry_forward_resolved_placeholders,
    snapshot_resolved_placeholders,
)
from app.services.rfp_retrieval import build_section_rfp_excerpt
from app.services.sections import clear_section_draft, persist_section_draft

log = logging.getLogger(__name__)


# Shared FK-safe stage-message logger; aliased so existing call sites
# in this module stay unchanged.
from app.services.stages import record_stage as _set_stage  # noqa: E402


def _build_rfp_text_excerpt(proposal_id: int) -> str:
    """Concatenate every parsed RFP document's full text into one
    string. No truncation — downstream consumers (the per-section
    retriever in app.services.rfp_retrieval, plus each agent's
    cached-prefix budget) decide how much actually reaches the LLM.
    Truncating at the source meant the retriever never saw material
    past ~60K chars on long RFPs (Section M evaluation criteria,
    attachment specs, etc.); now it does."""
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


def _format_compliance_for_writer(items: list[dict]) -> str:
    lines: list[str] = []
    for it in items:
        line = f"{it['requirement_id']} [{it['requirement_type']}/{it['category']}"
        if it.get("weight"):
            line += f" w={it['weight']}"
        line += f"] {it['requirement_text']}"
        if it.get("source_section"):
            line += f"  ({it['source_section']})"
        lines.append(line)
    return "\n".join(lines) if lines else "(no compliance items)"


def _format_gaps_for_writer(gaps: list[dict]) -> str:
    if not gaps:
        return "(no gaps)"
    blocks: list[str] = []
    for g in gaps:
        sel_idx = g.get("selected_mitigation_index")
        rec_idx = g.get("recommended_index")
        chosen_idx = sel_idx if sel_idx is not None else rec_idx
        opts = g.get("mitigation_options") or []
        chosen_block = ""
        if chosen_idx is not None and 0 <= chosen_idx < len(opts):
            opt = opts[chosen_idx]
            sel_partner = g.get("selected_partner_name")
            chosen_block = (
                "  *** AUTHORITATIVE GAP RESOLUTION — DO NOT EXCEED ***\n"
                f"  CHOSEN MITIGATION (option {chosen_idx}, "
                f"{'user-selected' if sel_idx is not None else 'agent-recommended'}): "
                f"{opt.get('approach', '?')}\n"
                f"    Proposal language draft: {opt.get('proposal_language_draft', '')}\n"
                f"    Honesty check: {opt.get('honesty_check', '')}\n"
            )
            if sel_partner:
                chosen_block += f"    Selected partner: {sel_partner}\n"
        notes = g.get("resolution_notes") or ""
        notes_block = f"  Resolution notes: {notes}\n" if notes else ""
        resolved = " (resolved)" if g.get("resolved") else ""
        blocks.append(
            f"{g['gap_id']} [{g['severity']}{resolved}] addresses {g['req_id']}\n"
            f"  Current state: {g.get('current_state', '')}\n"
            f"{chosen_block}{notes_block}"
        )
    return "\n".join(blocks)


def _snapshot_writer_inputs(proposal_id: int) -> dict:
    """Pull every dict the Writer needs out of the DB in one go."""
    with SessionLocal() as db:
        # Sections (the outline)
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
                "excluded_from_draft": bool(s.excluded_from_draft),
                "has_draft": bool(s.draft_text_markdown),
                "compliance_items_addressed": list(s.compliance_items_addressed_json or []),
            }
            for s in sec_rows
        ]

        # Compliance items — active rows only. Superseded / removed rows
        # are kept for amendment audit transparency but must NOT reach the
        # writer's cached prefix (they carry stale requirement_text under
        # the same requirement_id).
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
                "requirement_type": i.requirement_type.value
                if hasattr(i.requirement_type, "value")
                else str(i.requirement_type),
                "category": i.category.value if hasattr(i.category, "value") else str(i.category),
                "weight": float(i.weight) if i.weight is not None else None,
                "source_section": i.source_section,
                "source_page": i.source_page,
            }
            for i in comp_rows
        ]

        # Gaps — joined ComplianceMatrixItem must be active, otherwise the
        # writer's cached prefix carries stale gap context (current_state +
        # mitigation_options) for amended requirements while the new active
        # row's text is what the section is actually drafting against.
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
        ]

    return {"sections": sections, "compliance_items": compliance_items, "gaps": gaps}


def _build_writer_cached_prefix(proposal_id: int, snap: dict) -> str:
    """Build the SHARED cached prefix for the Writer Team.

    Holds only content that's truly common across sections — profile,
    teaming, the user-approved team roster (when present), the decisions
    ledger, the full compliance matrix (for cross-section awareness),
    and the COTS orientation flag. KB context, sibling outline snippet,
    and assigned gaps now live in the per-section user prompt (see
    app.services.kb_context.build_section_kb_context and the helpers
    below). The RFP excerpt was already moved out in an earlier change.

    The team roster block is empty when the user hasn't approved a
    team — in that state the writer correctly defers to NEEDS_HUMAN
    for staffing decisions. Once the team is approved, the block
    appears and the writer uses the named personnel + allocations
    directly.
    """
    profile = get_company_profile()
    profile_json = json.dumps(profile, indent=2)
    teaming_partners_json = json.dumps(get_teaming_partners(), indent=2)
    decisions_text = format_decisions_for_prompt()

    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        cots_orientation = bool(p.cots_orientation) if p else False

    from app.services.framing import format_framing_block_for_writer
    from app.services.pricing import format_cost_build_block_for_writer
    from app.services.team import format_team_block_for_writer

    team_roster_block = format_team_block_for_writer(proposal_id)
    cost_build_block = format_cost_build_block_for_writer(proposal_id)
    framing_block = format_framing_block_for_writer(proposal_id)

    return build_cached_prefix(
        profile_json=profile_json,
        teaming_partners_json=teaming_partners_json,
        decisions_text=decisions_text,
        compliance_text=_format_compliance_for_writer(snap["compliance_items"]),
        cots_orientation=cots_orientation,
        held_certifications_block=format_held_certifications_block(profile),
        team_roster_block=team_roster_block,
        cost_build_block=cost_build_block,
        framing_block=framing_block,
    )


def _gaps_for_section(section: dict, gaps: list[dict]) -> list[str]:
    """Find the gap_ids whose req_id is in this section's compliance_items_addressed."""
    section_req_ids = set(section.get("compliance_items_addressed") or [])
    return [g["gap_id"] for g in gaps if g.get("req_id") in section_req_ids]


def _compliance_text_lookup(snap: dict) -> dict[str, str]:
    """req_id → requirement_text map from the writer snapshot."""
    return {c["requirement_id"]: c.get("requirement_text") or "" for c in snap.get("compliance_items", [])}


def _section_rfp_excerpt(rfp_full_text: str, section: dict, comp_text_lookup: dict[str, str]) -> str:
    """Build a focused RFP excerpt for one section: Section L/M-style
    governance plus paragraphs whose terms match the section's brief +
    its assigned compliance items. Empty rfp text → empty excerpt."""
    if not rfp_full_text:
        return ""
    req_ids = section.get("compliance_items_addressed") or []
    comp_texts = [comp_text_lookup.get(rid, "") for rid in req_ids]
    return build_section_rfp_excerpt(
        rfp_full_text,
        section_title=section.get("section_title") or "",
        section_brief=section.get("section_brief") or "",
        compliance_item_texts=comp_texts,
    )


def _section_kb_context(section: dict, comp_text_lookup: dict[str, str]) -> str:
    """Per-section scoped KB excerpt — only the citable docs that
    pertain to this section's title, brief, and assigned compliance
    items. Replaces the wholesale build_shortfall_kb_context dump
    that used to live in the cached prefix."""
    req_ids = section.get("compliance_items_addressed") or []
    comp_texts = [comp_text_lookup.get(rid, "") for rid in req_ids]
    return build_section_kb_context(
        section_title=section.get("section_title") or "",
        section_brief=section.get("section_brief") or "",
        compliance_item_texts=comp_texts,
    )


def _section_outline_snippet(sections: list[dict], current_pk: int) -> str:
    """Compact outline view for the user prompt: one line per sibling
    section so the writer knows what's covered elsewhere and avoids
    duplication. The current section is shown with a marker so the
    writer can locate itself in the structure."""
    if not sections:
        return "(no outline)"
    lines: list[str] = []
    for s in sections:
        marker = ">>>" if s.get("pk") == current_pk else "   "
        n_items = len(s.get("compliance_items_addressed") or [])
        page_lim = f", page_limit={s['page_limit']}" if s.get("page_limit") else ""
        word_lim = f", word_limit={s['word_limit']}" if s.get("word_limit") else ""
        flags: list[str] = []
        if s.get("requires_cost_analysis"):
            flags.append("cost-deferred")
        if s.get("excluded_from_draft"):
            flags.append("excluded-from-draft")
        flag_str = f" [{'/'.join(flags)}]" if flags else ""
        lines.append(
            f"{marker} {s['section_id']} (#{s['section_order']}) "
            f"{s['section_title']}{page_lim}{word_lim}{flag_str} "
            f"— {n_items} compliance item(s)"
        )
    return "\n".join(lines)


def _section_gaps_text(section: dict, all_gaps: list[dict]) -> str:
    """Filter the full gap list down to gaps whose req_id is in this
    section's compliance_items_addressed, then render via the existing
    formatter so the prompt voice matches what the writer is used to."""
    section_req_ids = set(section.get("compliance_items_addressed") or [])
    section_gaps = [g for g in all_gaps if g.get("req_id") in section_req_ids]
    return _format_gaps_for_writer(section_gaps)


def run_writer_team(proposal_id: int) -> None:
    """Draft every eligible ProposalSection for a proposal in parallel.

    Status: AWAITING_OUTLINE_APPROVAL → DRAFT_IN_PROGRESS → DRAFT_READY.

    Parallelism is bounded by `settings.writer_workers` (default 4).
    Each worker runs draft_section against the shared cached prefix;
    the first worker to land writes the Anthropic prompt cache, the
    rest read it. Persistence and the DRAFTED-status update happen in
    the main thread (inside as_completed) to keep DB writes serialized.
    """
    log.info("writer team starting for proposal %d", proposal_id)
    try:
        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p:
                p.status = ProposalStatus.DRAFT_IN_PROGRESS

        _set_stage(proposal_id, "Building Writer Team context (profile + KB + outline)…")
        snap = _snapshot_writer_inputs(proposal_id)
        if not snap["sections"]:
            _set_stage(proposal_id, "No outline to draft from — generate the outline first.")
            return

        cached_prefix = _build_writer_cached_prefix(proposal_id, snap)

        # Pull the raw RFP text once (it's identical across all sections)
        # so every per-section excerpt is built from the same source.
        # The retrieval module re-splits paragraphs per call — cheap on
        # 170k-char text. Sharing the source string avoids N copies.
        rfp_full_text = _build_rfp_text_excerpt(proposal_id)
        comp_text_lookup = _compliance_text_lookup(snap)

        # Initial drafts use the cheaper "initial" writer model. Revisions
        # (run_writer_for_section) keep Opus — that's where reasoning
        # quality matters most because we're targeting reviewer-flagged issues.
        settings = get_settings()
        initial_model = settings.model_writer_team_initial
        workers = max(1, int(settings.writer_workers or 1))

        # Pre-pass: split sections into "draft now" vs "skip". Skip
        # categories:
        #  - excluded_from_draft (user-flagged wrapper sections)
        #  - cost-deferred (Cost Analysis Agent territory)
        #  - already drafted (resume after an interrupted run — don't
        #    redo work that survived in the DB)
        # Per-section regenerate paths (run_writer_for_section, the
        # Regenerate button) bypass run_writer_team entirely, so the
        # already-drafted skip never gets in the way of an explicit
        # redraft.
        # In-flight sections — currently being regenerated by another
        # path (typically `spawn_writer_for_section` from Apply All on
        # the Reviewer Findings tab). Skipping them here closes a
        # genuine race window: without this guard, the batch would see
        # a section as "no draft yet" while a per-section regen is
        # mid-flight and clobber the regen's output (losing the user's
        # accepted-findings directive in the process).
        from app.services.cancellation import get_active_sections

        active_pks = get_active_sections(proposal_id)

        # Per-section accepted findings — when present, drive the
        # batch's draft via `user_directive` so an interrupted Apply-
        # All-then-batch sequence still produces drafts that reflect
        # the user's accepted findings, not fresh-from-brief drafts.
        from app.services.findings import (
            build_directive_from_findings,
            get_accepted_findings_for_section,
        )

        len(snap["sections"])
        sections_to_draft: list[dict] = []
        directive_by_pk: dict[int, str] = {}
        n_skipped_excluded = 0
        n_skipped_cost = 0
        n_skipped_in_flight = 0
        n_already_drafted = 0
        for section in snap["sections"]:
            if section.get("excluded_from_draft"):
                n_skipped_excluded += 1
                continue
            if section.get("requires_cost_analysis"):
                n_skipped_cost += 1
                continue
            if section["pk"] in active_pks:
                n_skipped_in_flight += 1
                log.info(
                    "writer_team: skipping section %s (pk=%d) — another "
                    "writer is regenerating it right now; not racing.",
                    section["section_id"],
                    section["pk"],
                )
                continue
            if section.get("has_draft"):
                n_already_drafted += 1
                continue
            # Section will be drafted. If the user has accepted findings
            # on it, build the directive now so draft_section gets it
            # exactly as if the user had clicked "Apply N accepted
            # findings → regenerate" on that section card.
            try:
                accepted = get_accepted_findings_for_section(section["pk"])
                if accepted:
                    directive_by_pk[section["pk"]] = build_directive_from_findings(accepted)
            except Exception:
                log.exception(
                    "writer_team: failed to load accepted findings for "
                    "section pk=%d — drafting without directive.",
                    section["pk"],
                )
            sections_to_draft.append(section)
        n_skipped = n_skipped_excluded + n_skipped_cost + n_skipped_in_flight

        if not sections_to_draft:
            done_msg = (
                f"No sections to draft — "
                f"{n_already_drafted} already drafted, "
                f"{n_skipped} skipped (excluded / cost-deferred)."
            )
            _set_stage(proposal_id, done_msg)
            with session_scope() as db:
                p = db.get(Proposal, proposal_id)
                if p:
                    p.status = ProposalStatus.DRAFT_READY
            return

        resume_part = f"; resuming after {n_already_drafted} previously drafted" if n_already_drafted else ""
        in_flight_part = (
            f"; {n_skipped_in_flight} in flight elsewhere (not racing)" if n_skipped_in_flight else ""
        )
        directive_part = (
            f"; {len(directive_by_pk)} with accepted-findings directive" if directive_by_pk else ""
        )
        _set_stage(
            proposal_id,
            f"Drafting {len(sections_to_draft)} section(s) × "
            f"{workers} worker(s) in parallel ({initial_model})"
            + (
                f"; {n_skipped_excluded + n_skipped_cost} skipped (excluded / cost-deferred)"
                if (n_skipped_excluded + n_skipped_cost)
                else ""
            )
            + in_flight_part
            + resume_part
            + directive_part
            + "…",
        )

        n_drafted = 0
        n_failed = 0
        completed = 0
        n_to_draft = len(sections_to_draft)

        # Run draft_section concurrently. The cached_prefix is shared —
        # whichever worker lands first writes the prompt cache; the
        # rest read it (~10% input cost on those tokens).
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"writer-{proposal_id}",
        ) as executor:
            future_to_section = {
                executor.submit(
                    draft_section,
                    proposal_id=proposal_id,
                    section_id=section["section_id"],
                    section_title=section["section_title"],
                    section_order=section["section_order"],
                    section_brief=section.get("section_brief") or "",
                    compliance_item_ids=section.get("compliance_items_addressed") or [],
                    assigned_gap_ids=_gaps_for_section(section, snap["gaps"]),
                    page_limit=section.get("page_limit"),
                    word_limit=section.get("word_limit"),
                    cached_prefix=cached_prefix,
                    rfp_excerpt=_section_rfp_excerpt(
                        rfp_full_text,
                        section,
                        comp_text_lookup,
                    ),
                    kb_context_excerpt=_section_kb_context(
                        section,
                        comp_text_lookup,
                    ),
                    gaps_for_section=_section_gaps_text(section, snap["gaps"]),
                    outline_snippet=_section_outline_snippet(
                        snap["sections"],
                        section["pk"],
                    ),
                    user_directive=directive_by_pk.get(section["pk"]),
                    model=initial_model,
                ): section
                for section in sections_to_draft
            }
            for future in as_completed(future_to_section):
                section = future_to_section[future]
                completed += 1
                try:
                    draft = future.result()
                except Exception as exc:
                    n_failed += 1
                    log.exception(
                        "writer_team: section %s failed for proposal %d — moving on",
                        section["section_id"],
                        proposal_id,
                    )
                    _set_stage(
                        proposal_id,
                        f"⚠ Section {section['section_id']} "
                        f"{section['section_title']} drafting failed "
                        f"({completed}/{n_to_draft}): {type(exc).__name__}: "
                        f"{str(exc)[:120]}",
                    )
                    continue

                persist_section_draft(
                    proposal_section_pk=section["pk"],
                    draft_text_markdown=draft.draft_text_markdown,
                    citations=draft.citations,
                    needs_human_placeholders=draft.needs_human_placeholders,
                    shortfall_mitigations_applied=draft.shortfall_mitigations_applied,
                )
                # Auto-resolve signatures + doc-creation dates so
                # the user never has to action those (CEO signs
                # everything; submission date = today). Same
                # rule applies on initial draft and on regenerate.
                auto_resolve_obvious_placeholders(section["pk"])
                # Phase B: LLM resolver picks up whatever's left
                # after the deterministic pass. Sees the cached
                # context (profile, decisions, team roster, cost
                # build) and resolves anything unambiguously
                # answerable; conservatively skips the rest.
                auto_resolve_via_llm(section["pk"])

                # Mark addressed compliance items as DRAFTED. Active rows
                # only — superseded rows must keep their pre-amendment
                # compliance_status so the audit trail stays accurate.
                with session_scope() as db:
                    req_ids = section.get("compliance_items_addressed") or []
                    if req_ids:
                        db.query(ComplianceMatrixItem).filter(
                            ComplianceMatrixItem.proposal_id == proposal_id,
                            ComplianceMatrixItem.requirement_id.in_(req_ids),
                            ComplianceMatrixItem.status == "active",
                        ).update(
                            {ComplianceMatrixItem.compliance_status: ComplianceStatus.DRAFTED},
                            synchronize_session=False,
                        )

                n_drafted += 1
                _set_stage(
                    proposal_id,
                    f"Draft progress: {completed}/{n_to_draft} done — "
                    f"{section['section_id']} {section['section_title']}",
                )

        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p:
                p.status = ProposalStatus.DRAFT_READY

        total_done = n_drafted + n_already_drafted
        msg = f"Draft ready: {total_done} section(s) written"
        if n_already_drafted:
            msg += f" ({n_drafted} this run + {n_already_drafted} resumed from prior runs)"
        msg += "."
        if n_failed:
            msg += f" ⚠ {n_failed} failed — check logs."
        if n_skipped_cost:
            msg += f" {n_skipped_cost} cost-deferred section(s) await the Cost Analysis Agent (Weeks 12-13)."
        if n_skipped_excluded:
            msg += f" {n_skipped_excluded} section(s) excluded from draft (user-flagged)."
        _set_stage(proposal_id, msg)

    except Exception:
        log.exception("writer team failed for proposal %d", proposal_id)
        _set_stage(proposal_id, "Writer Team failed — check logs.")


def run_writer_for_section(
    proposal_id: int,
    proposal_section_pk: int,
    user_directive: str | None = None,
    pass_num: int | None = None,
) -> None:
    """Regenerate ONE section. Used by the per-section Regenerate and
    Refine-with-AI buttons on the Draft tab. Does not change proposal status.

    `user_directive` is optional natural-language revision guidance — the
    Writer Team agent receives it as a USER DIRECTIVE block in the per-call
    user prompt.

    `pass_num` is the auto-Review-Revise loop's pass number when this is
    being called from `_review_revise_one_section`. When provided, the
    revision model is picked from the pass-bracketed schedule
    (Settings.model_writer_team_for_pass). When None (manual Regenerate /
    Refine-with-AI), we use the legacy `model_writer_team` default — those
    are user-initiated and the user wants the heaviest model.
    """
    log.info(
        "writer team regenerating section pk=%d for proposal %d (directive=%r)",
        proposal_section_pk,
        proposal_id,
        (user_directive[:80] + "…") if user_directive and len(user_directive) > 80 else user_directive,
    )
    # Mark this section as actively in-flight so the writer team
    # batch (run_writer_team) won't see it as "needs drafting" and
    # race us with a fresh draft that loses the user's accepted-
    # findings directive. The Reviewer Findings tab also reads this
    # set to disable per-section actions while a regen is running.
    from app.services.cancellation import (
        add_active_section,
        remove_active_section,
    )

    add_active_section(proposal_id, proposal_section_pk)
    try:
        snap = _snapshot_writer_inputs(proposal_id)
        section = next(
            (s for s in snap["sections"] if s["pk"] == proposal_section_pk),
            None,
        )
        if section is None:
            _set_stage(proposal_id, f"Section pk={proposal_section_pk} not found.")
            return

        if section.get("excluded_from_draft"):
            _set_stage(
                proposal_id,
                f"Section {section['section_id']} is excluded from draft "
                f"(user-flagged on Outline tab). Skipping.",
            )
            return
        if section.get("requires_cost_analysis"):
            _set_stage(
                proposal_id,
                f"Section {section['section_id']} is cost-deferred — the Cost "
                f"Analysis Agent (Weeks 12-13) drafts this section. Skipping.",
            )
            return

        directive_suffix = " (refining)" if user_directive else ""
        _set_stage(
            proposal_id,
            f"Regenerating section {section['section_id']} {section['section_title']}{directive_suffix}…",
        )
        # Snapshot the user's resolved [NEEDS_HUMAN] answers BEFORE the
        # clear wipes them. These are passed to the Writer Team prompt so
        # it bakes the values into the new prose, and re-applied as a
        # safety net via carry_forward_resolved_placeholders after persist
        # in case the model still re-emits matching markers.
        prior_resolved = snapshot_resolved_placeholders(proposal_section_pk)
        clear_section_draft(proposal_section_pk)

        cached_prefix = _build_writer_cached_prefix(proposal_id, snap)
        comp_text_lookup = _compliance_text_lookup(snap)
        rfp_excerpt = _section_rfp_excerpt(
            _build_rfp_text_excerpt(proposal_id),
            section,
            comp_text_lookup,
        )
        kb_excerpt = _section_kb_context(section, comp_text_lookup)
        gaps_text = _section_gaps_text(section, snap["gaps"])
        outline_snippet = _section_outline_snippet(
            snap["sections"],
            section["pk"],
        )
        chosen_model = get_settings().model_writer_team_for_pass(pass_num)
        try:
            draft = draft_section(
                proposal_id=proposal_id,
                section_id=section["section_id"],
                section_title=section["section_title"],
                section_order=section["section_order"],
                section_brief=section.get("section_brief") or "",
                compliance_item_ids=section.get("compliance_items_addressed") or [],
                assigned_gap_ids=_gaps_for_section(section, snap["gaps"]),
                page_limit=section.get("page_limit"),
                word_limit=section.get("word_limit"),
                cached_prefix=cached_prefix,
                rfp_excerpt=rfp_excerpt,
                kb_context_excerpt=kb_excerpt,
                gaps_for_section=gaps_text,
                outline_snippet=outline_snippet,
                user_directive=user_directive,
                model=chosen_model,
                prior_resolved_placeholders=prior_resolved,
            )
        except Exception:
            log.exception(
                "writer_team: regenerate failed for section pk=%d",
                proposal_section_pk,
            )
            _set_stage(
                proposal_id,
                f"Section {section['section_id']} regenerate failed — see logs.",
            )
            return

        persist_section_draft(
            proposal_section_pk=proposal_section_pk,
            draft_text_markdown=draft.draft_text_markdown,
            citations=draft.citations,
            needs_human_placeholders=draft.needs_human_placeholders,
            shortfall_mitigations_applied=draft.shortfall_mitigations_applied,
        )
        # Clear the amendment-drift flag — the section was just
        # re-drafted against the current compliance matrix, so any
        # prior staleness from an amendment is now resolved.
        with session_scope() as db:
            sec_row = db.get(ProposalSection, proposal_section_pk)
            if sec_row is not None:
                sec_row.compliance_drift_pending = False
        # Auto-resolve placeholders that have safe defaults
        # (CEO-signs-everything signatures + doc-creation dates)
        # so the user isn't asked for the obvious stuff. Runs
        # before carry-forward so prior user resolutions still
        # win on collisions.
        n_auto = auto_resolve_obvious_placeholders(proposal_section_pk)
        carried = carry_forward_resolved_placeholders(
            proposal_section_pk,
            prior_resolved,
        )
        # Phase B: LLM-driven resolver runs LAST so it only sees
        # placeholders the deterministic + carry-forward passes
        # didn't already handle. Conservative — when the cached
        # context can't unambiguously supply a value, the agent
        # skips and the user resolves manually.
        n_llm = auto_resolve_via_llm(proposal_section_pk)
        carry_bits: list[str] = []
        if carried:
            carry_bits.append(f"{carried} prior human input{'s' if carried != 1 else ''} carried forward")
        if n_auto:
            carry_bits.append(f"{n_auto} auto-resolved (signatures / dates)")
        if n_llm:
            carry_bits.append(f"{n_llm} resolved by LLM from cached context")
        suffix = f" ({'; '.join(carry_bits)})" if carry_bits else ""
        _set_stage(
            proposal_id,
            f"Section {section['section_id']} regenerated"
            f"{' with directive applied' if user_directive else ''}"
            f"{suffix}.",
        )

    except Exception:
        log.exception("regenerate failed for section pk=%d", proposal_section_pk)
        _set_stage(proposal_id, "Section regenerate failed — check logs.")
    finally:
        remove_active_section(proposal_id, proposal_section_pk)


def spawn_writer_team(proposal_id: int) -> threading.Thread:
    t = threading.Thread(
        target=run_writer_team,
        args=(proposal_id,),
        name=f"writer-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


def spawn_writer_for_section(
    proposal_id: int,
    proposal_section_pk: int,
    user_directive: str | None = None,
) -> threading.Thread:
    t = threading.Thread(
        target=run_writer_for_section,
        args=(proposal_id, proposal_section_pk, user_directive),
        name=f"writer-sec-{proposal_section_pk}",
        daemon=True,
    )
    t.start()
    return t
