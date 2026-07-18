"""Cost Volume Writer orchestration.

Two entry points:
  - run_cost_writer(proposal_id) — sync; called by background thread
  - spawn_cost_writer(proposal_id) — daemon thread launcher for the
    "Run Cost Volume Writer" button on the Cost tab

Loops over every ProposalSection where requires_cost_analysis=True
AND excluded_from_draft=False, drafts each one in parallel via the
existing writer_workers pool size. Re-running replaces existing
drafts (clear_section_draft → draft → persist_section_draft).

Prerequisites surfaced as stage banners:
  - Pricing packages must exist (Cost Analyst must have run first).
  - Market scan is optional but recommended (agent omits market-
    comparison narrative if absent).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select

from app.agents.cost_writer import (
    DEFAULT_PROPOSED_SCENARIO,
    CostWriterContext,
    build_cached_prefix,
    draft_cost_section,
)
from app.config import get_settings
from app.core.company_profile import get_company_profile
from app.db.session import session_scope
from app.models import (
    ComplianceMatrixItem,
    Proposal,
    ProposalSection,
)
from app.services.market_scan import get_market_scan_snapshot
from app.services.pricing import (
    format_cost_build_block_for_writer,
    get_pricing_packages_snapshot,
    get_proposed_scenario,
)
from app.services.sections import (
    clear_section_draft,
    persist_section_draft,
)
from app.services.service_line import (
    SERVICE_LINE_IT_SERVICES,
    SERVICE_LINE_PAYMENT_SYSTEMS,
    get_service_line,
)
from app.services.stages import record_stage as _set_stage

log = logging.getLogger(__name__)


def _format_payment_accepted_directives_block(
    directives: list[dict],
) -> str:
    """Render accepted Cost Reviewer findings as the ACCEPTED REVIEWER
    DIRECTIVES cached-prefix block. Empty string when no directives —
    the prefix template's slot then renders cleanly. Each directive
    surfaces the cited verbatim quote (so the writer locates the
    exact text to replace) and the canonical fix the user accepted
    (or edited)."""
    if not directives:
        return ""

    n_critical = sum(1 for d in directives if d.get("severity") == "CRITICAL")
    n_major = sum(1 for d in directives if d.get("severity") == "MAJOR")
    n_minor = sum(1 for d in directives if d.get("severity") == "MINOR")

    parts: list[str] = [
        "=== ACCEPTED REVIEWER DIRECTIVES — APPLY THESE FIXES VERBATIM ===",
        (
            f"The user has reviewed the previous draft and accepted "
            f"{len(directives)} reviewer finding(s) — "
            f"{n_critical} CRITICAL, {n_major} MAJOR, {n_minor} MINOR. "
            f"Each directive below identifies a passage in the "
            f"previous draft that needs to change. Your job: "
            f"incorporate each `Fix` into the corresponding section. "
            f"Apply CRITICAL directives non-negotiably; apply MAJOR "
            f"verbatim; MINOR may be paraphrased to fit narrative "
            f"flow as long as the underlying correction lands. Do "
            f"NOT re-introduce any of the cited drift in your new "
            f"draft."
        ),
        "",
    ]
    for d in directives:
        parts.append(
            f"--- {d.get('finding_id', '?')} "
            f"[{d.get('severity', 'MINOR')}/{d.get('category', 'OTHER')}] "
            f"in {d.get('section_id', '?')} ---"
        )
        if d.get("finding_text"):
            parts.append(f"  Finding: {d['finding_text']}")
        if d.get("cited_quote"):
            parts.append(f"  Cited drift: “{d['cited_quote']}”")
        edited_marker = " (USER-EDITED)" if d.get("edited") else ""
        parts.append(f"  Fix{edited_marker}: {d.get('canonical_fix', '')}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n\n"


def _build_quadratic_summary() -> str:
    """Compact firm summary tailored to cost narrative — emphasizes
    competitive edge (AI-accelerated delivery) which the writer can
    use in cost-realism arguments."""
    profile = get_company_profile()
    bits: list[str] = []
    name = profile.get("legal_name") or profile.get("name") or "Quadratic Digital"
    bits.append(name)
    if size := profile.get("employee_count") or profile.get("size"):
        bits.append(f"Headcount: {size}")
    if loc := profile.get("hq_location") or profile.get("headquarters"):
        bits.append(f"HQ: {loc}")
    bits.append(
        "Competitive edge: AI-accelerated custom delivery — leaner "
        "team than typical for the scope, compensated by accelerated "
        "delivery velocity. Use this where it strengthens cost-"
        "realism arguments."
    )
    return ". ".join(bits)


def _detect_contract_type_signal(proposal_id: int) -> str:
    """Best-effort pull of contract-type hints from compliance items.
    Returns a short string the agent can fold into cost-narrative
    framing (FFP vs T&M vs cost-plus)."""
    keywords_ffp = ("firm fixed price", "fixed price", "ffp")
    keywords_tm = ("time and materials", "time & materials", "t&m", "labor hour")
    keywords_costplus = ("cost reimbursement", "cost-plus", "cpff", "cpif")
    with session_scope() as db:
        rows = db.execute(
            select(ComplianceMatrixItem.requirement_text).where(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
        ).all()
    blob = "\n".join((r[0] or "").lower() for r in rows)
    found = []
    if any(k in blob for k in keywords_ffp):
        found.append("Firm Fixed Price (FFP)")
    if any(k in blob for k in keywords_tm):
        found.append("Time & Materials (T&M) / Labor Hour")
    if any(k in blob for k in keywords_costplus):
        found.append("Cost Reimbursement / Cost-Plus")
    if not found:
        return "(unknown - assume Firm Fixed Price for state IT services)"
    return " or ".join(found)


def _snapshot_writer_inputs(proposal_id: int) -> dict | None:
    """Pull everything the orchestrator needs to drive the writer.
    Returns None if the proposal doesn't exist or has no cost-deferred
    sections to draft."""
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return None

        rfp_title = (prop.title or "").strip()
        rfp_agency = (prop.agency or "").strip()

        # Sections to draft — only cost-deferred + not-excluded.
        eligible = db.execute(
            select(
                ProposalSection.id,
                ProposalSection.section_id,
                ProposalSection.section_title,
                ProposalSection.section_order,
                ProposalSection.section_brief,
                ProposalSection.page_limit,
                ProposalSection.word_limit,
                ProposalSection.compliance_items_addressed_json,
            )
            .where(
                ProposalSection.proposal_id == proposal_id,
                ProposalSection.requires_cost_analysis == True,  # noqa: E712
                ProposalSection.excluded_from_draft == False,  # noqa: E712
            )
            .order_by(ProposalSection.section_order)
        ).all()
        sections: list[dict] = [
            {
                "pk": pk,
                "section_id": sid,
                "section_title": title,
                "section_order": order,
                "section_brief": brief or "",
                "page_limit": page_limit,
                "word_limit": word_limit,
                "compliance_items_addressed": list(comp_ids or []),
            }
            for (pk, sid, title, order, brief, page_limit, word_limit, comp_ids) in eligible
        ]

        # Outline snippet for cross-reference — list ALL sections
        # (including non-cost) so the writer doesn't duplicate
        # technical-volume content.
        all_sections = db.execute(
            select(
                ProposalSection.section_id,
                ProposalSection.section_title,
                ProposalSection.requires_cost_analysis,
            )
            .where(
                ProposalSection.proposal_id == proposal_id,
            )
            .order_by(ProposalSection.section_order)
        ).all()
        outline_lines: list[str] = []
        for sid, title, requires_cost in all_sections:
            tag = " [cost]" if requires_cost else ""
            outline_lines.append(f"  - {sid}{tag}: {title}")
        outline_snippet = "\n".join(outline_lines) if outline_lines else ""

        # Pull a compact compliance-text mapping keyed by req_id so
        # we can render the section's referenced items inline. Cost
        # sections often reference cost-realism / basis-of-estimate
        # requirements by ID.
        comp_rows = db.execute(
            select(
                ComplianceMatrixItem.requirement_id,
                ComplianceMatrixItem.requirement_text,
                ComplianceMatrixItem.category,
            ).where(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
        ).all()
        comp_text_lookup: dict[str, str] = {req_id: (text or "") for (req_id, text, _category) in comp_rows}

    pop_months = 12  # same default as cost analyst orchestrator
    contract_type_signal = _detect_contract_type_signal(proposal_id)

    return {
        "rfp_title": rfp_title,
        "rfp_agency": rfp_agency,
        "pop_months": pop_months,
        "contract_type_signal": contract_type_signal,
        "sections": sections,
        "outline_snippet": outline_snippet,
        "comp_text_lookup": comp_text_lookup,
    }


def _compliance_text_for_section(
    compliance_item_ids: list[str],
    comp_text_lookup: dict[str, str],
) -> str:
    """Render the section's referenced compliance items inline. Cost
    sections typically map to a small number of cost-realism /
    basis-of-estimate requirements; surface those verbatim so the
    writer can address them specifically."""
    if not compliance_item_ids:
        return ""
    rows: list[str] = []
    for req_id in compliance_item_ids:
        text = (comp_text_lookup.get(req_id) or "").strip()
        if not text:
            continue
        # Truncate per-item to keep the user prompt under control.
        snippet = text[:1200] + ("..." if len(text) > 1200 else "")
        rows.append(f"  [{req_id}] {snippet}")
    return "\n".join(rows)


def run_cost_writer(proposal_id: int) -> None:
    """Sync entry point. Drafts every cost-deferred section in
    parallel, persisting via the existing Writer Team's path."""
    log.info("cost writer starting for proposal %d", proposal_id)
    try:
        _set_stage(
            proposal_id,
            "Cost Volume Writer: snapshotting cost build + scope…",
        )
        inputs = _snapshot_writer_inputs(proposal_id)
        if inputs is None:
            _set_stage(
                proposal_id,
                f"Cost Volume Writer: proposal {proposal_id} not found.",
            )
            return
        if not inputs["sections"]:
            _set_stage(
                proposal_id,
                "Cost Volume Writer: no cost-deferred sections to draft "
                "(none flagged requires_cost_analysis=True). Nothing "
                "to do.",
            )
            return

        # Service-line branch: payment_systems skips the labor-flow
        # gates (no PricingPackage rows exist; the Cost Writer pulls
        # its narrative from data/pricing/payment_systems.json).
        service_line = get_service_line(proposal_id)
        market_scan = get_market_scan_snapshot(proposal_id)

        if service_line == SERVICE_LINE_PAYMENT_SYSTEMS:
            payment_systems_block = format_cost_build_block_for_writer(proposal_id)
            if not payment_systems_block.strip():
                _set_stage(
                    proposal_id,
                    "Cost Volume Writer: payment_systems data files "
                    "missing or empty (data/pricing/payment_systems.json"
                    " + _payment_systems_context.json). Cannot proceed.",
                )
                return
            # Pull accepted Cost Reviewer findings from the prior pass
            # (if any) and render them as an ACCEPTED REVIEWER
            # DIRECTIVES block. The agent treats this as the highest-
            # priority instruction set on the re-draft. Empty string
            # = no findings (first-time draft) → block doesn't render.
            from app.services.payment_cost_review import (
                get_accepted_payment_findings_for_writer,
            )

            accepted_directives = get_accepted_payment_findings_for_writer(proposal_id)
            directives_block = _format_payment_accepted_directives_block(
                accepted_directives,
            )
            ctx = CostWriterContext(
                pricing_packages_snapshot=[],
                market_scan_snapshot=market_scan,
                executive_summary="",
                quadratic_summary=_build_quadratic_summary(),
                proposed_scenario=DEFAULT_PROPOSED_SCENARIO,
                contract_type_signal=inputs["contract_type_signal"],
                service_line=SERVICE_LINE_PAYMENT_SYSTEMS,
                payment_systems_cost_block=payment_systems_block,
                accepted_directives_block=directives_block,
            )
            cached_prefix = build_cached_prefix(ctx)
            n_total = len(inputs["sections"])
            n_directives = len(accepted_directives)
            stage_tail = f" applying {n_directives} accepted reviewer directive(s)" if n_directives else ""
            _set_stage(
                proposal_id,
                f"Cost Volume Writer (Payment Systems): drafting "
                f"{n_total} cost-deferred section(s) from fee "
                f"schedule{stage_tail}…",
            )
        else:
            pricing_packages = get_pricing_packages_snapshot(proposal_id)
            if len(pricing_packages) < 3:
                _set_stage(
                    proposal_id,
                    f"Cost Volume Writer: only {len(pricing_packages)} "
                    f"pricing package(s) found; need 3 (LOW/MEDIUM/HIGH). "
                    f"Run Cost Analyst first.",
                )
                return

            # Read the user's persisted scenario choice; falls back to
            # DEFAULT_PROPOSED_SCENARIO ("MEDIUM") when the user hasn't
            # explicitly picked one.
            proposed_scenario = get_proposed_scenario(proposal_id)
            proposed_pkg = next(
                (p for p in pricing_packages if p["scenario"] == proposed_scenario),
                None,
            )
            if proposed_pkg is None:
                _set_stage(
                    proposal_id,
                    f"Cost Volume Writer: missing {proposed_scenario} scenario; cannot proceed.",
                )
                return

            # Executive summary lives in pnl_projection_json under the
            # 'executive_summary' key (placed there by the Cost Analyst).
            exec_summary = proposed_pkg.get("pnl_projection_json", {}).get("executive_summary") or ""

            ctx = CostWriterContext(
                pricing_packages_snapshot=pricing_packages,
                market_scan_snapshot=market_scan,
                executive_summary=exec_summary,
                quadratic_summary=_build_quadratic_summary(),
                proposed_scenario=proposed_scenario,
                contract_type_signal=inputs["contract_type_signal"],
                service_line=SERVICE_LINE_IT_SERVICES,
            )
            cached_prefix = build_cached_prefix(ctx)

            n_total = len(inputs["sections"])
            proposed_price = proposed_pkg.get("total_proposed_price") or 0
            _set_stage(
                proposal_id,
                f"Cost Volume Writer: drafting {n_total} cost-deferred "
                f"section(s) at {proposed_scenario} scenario "
                f"(${proposed_price:,.0f})…",
            )

        # Parallel section drafting using the same worker pool as the
        # main writer. Each call hits the Anthropic prompt cache after
        # the first writer warms it.
        workers = max(1, int(get_settings().writer_workers or 1))
        log.info(
            "cost_writer: drafting %d sections with %d worker(s)",
            n_total,
            workers,
        )

        n_done = 0
        n_failed = 0
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="cost-writer",
        ) as ex:
            futures = {
                ex.submit(
                    _draft_one_section,
                    proposal_id=proposal_id,
                    rfp_title=inputs["rfp_title"],
                    rfp_agency=inputs["rfp_agency"],
                    pop_months=inputs["pop_months"],
                    contract_type_signal=inputs["contract_type_signal"],
                    section=section,
                    outline_snippet=inputs["outline_snippet"],
                    comp_text_lookup=inputs["comp_text_lookup"],
                    cached_prefix=cached_prefix,
                ): section
                for section in inputs["sections"]
            }
            for fut in as_completed(futures):
                section = futures[fut]
                try:
                    fut.result()
                    n_done += 1
                    _set_stage(
                        proposal_id,
                        f"Cost Volume Writer: drafted "
                        f"[{n_done}/{n_total}] "
                        f"{section['section_id']} "
                        f"{section['section_title']}.",
                    )
                except Exception:
                    log.exception(
                        "cost_writer: failed for section %s (pk=%d)",
                        section["section_id"],
                        section["pk"],
                    )
                    n_failed += 1

        if n_failed:
            _set_stage(
                proposal_id,
                f"Cost Volume Writer complete: {n_done}/{n_total} "
                f"drafted, {n_failed} failed (see logs). Open the "
                f"Findings tab — failed sections need a re-run.",
            )
        else:
            # `proposed_scenario` only exists in the it_services
            # branch — payment_systems has no LOW/MEDIUM/HIGH concept,
            # so the stage message has to branch too.
            if service_line == SERVICE_LINE_PAYMENT_SYSTEMS:
                tail = "from the payment-systems fee schedule"
            else:
                tail = f"at {proposed_scenario} scenario"
            _set_stage(
                proposal_id,
                f"Cost Volume Writer complete: {n_done}/{n_total} "
                f"cost-deferred section(s) drafted {tail}. Open the "
                f"Draft tab to review.",
            )

        # Status advance for payment_systems: there's no Cost Analyst
        # to flip AWAITING_COST_BUILD → AWAITING_DRAFT (the labor
        # flow's path at jobs/cost_analyst.py:361). The Cost Writer
        # IS the cost build for this service line, so we do the flip
        # here once at least one cost-deferred section drafted
        # cleanly. Status is left alone otherwise — re-running the
        # Cost Writer after a draft exists doesn't rewind. Forward-
        # only transitions per the architecture doc.
        if service_line == SERVICE_LINE_PAYMENT_SYSTEMS and n_done > 0:
            from app.core.enums import ProposalStatus

            with session_scope() as db:
                p = db.get(Proposal, proposal_id)
                if p is not None and p.status == ProposalStatus.AWAITING_COST_BUILD:
                    p.status = ProposalStatus.AWAITING_DRAFT
                    log.info(
                        "cost_writer (payment_systems): proposal=%d "
                        "advanced AWAITING_COST_BUILD -> AWAITING_DRAFT",
                        proposal_id,
                    )
    except Exception:
        log.exception(
            "cost writer failed for proposal %d",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            "Cost Volume Writer failed — check logs.",
        )


def _draft_one_section(
    *,
    proposal_id: int,
    rfp_title: str,
    rfp_agency: str,
    pop_months: int,
    contract_type_signal: str,
    section: dict,
    outline_snippet: str,
    comp_text_lookup: dict[str, str],
    cached_prefix: str,
) -> None:
    """Draft a single cost-deferred section and persist. Runs in a
    worker thread; raises on agent failure so the orchestrator can
    count failures."""
    compliance_text = _compliance_text_for_section(
        section["compliance_items_addressed"],
        comp_text_lookup,
    )

    # Wipe any prior draft so the new one isn't appended.
    clear_section_draft(section["pk"])

    draft = draft_cost_section(
        proposal_id=proposal_id,
        section_id=section["section_id"],
        section_title=section["section_title"],
        section_order=section["section_order"],
        section_brief=section["section_brief"],
        compliance_item_ids=section["compliance_items_addressed"],
        compliance_text=compliance_text,
        page_limit=section["page_limit"],
        word_limit=section["word_limit"],
        cached_prefix=cached_prefix,
        rfp_title=rfp_title,
        rfp_agency=rfp_agency,
        pop_months=pop_months,
        contract_type_signal=contract_type_signal,
        outline_snippet=outline_snippet,
    )

    persist_section_draft(
        proposal_section_pk=section["pk"],
        draft_text_markdown=draft.draft_text_markdown,
        citations=draft.citations,
        needs_human_placeholders=draft.needs_human_placeholders,
        shortfall_mitigations_applied=draft.shortfall_mitigations_applied,
    )


def spawn_cost_writer(proposal_id: int) -> threading.Thread:
    """Daemon thread launcher for the 'Run Cost Volume Writer' button."""
    t = threading.Thread(
        target=run_cost_writer,
        args=(proposal_id,),
        name=f"cost-writer-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


__all__ = [
    "run_cost_writer",
    "spawn_cost_writer",
]
