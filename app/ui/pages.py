"""NiceGUI page registrations for the complete RFP workflow."""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from nicegui import ui
from nicegui.timer import Timer as BackgroundTimer
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.agents.intake_metadata import ExtractedMetadata, extract_metadata_from_text
from app.agents.kb_classify import ClassificationResult, classify_kb_upload
from app.config import get_settings
from app.core.company_profile import (
    get_capability_areas,
    get_certifications,
    get_clearances_inventory,
    get_company_profile,
    get_deep_specializations,
    get_key_personnel,
    get_labor_rate_card,
)
from app.core.enums import KbDocumentClass, KbDocumentStatus, ProposalStatus
from app.db.session import SessionLocal, session_scope
from app.jobs.cost_writer import spawn_cost_writer
from app.jobs.intake import spawn_intake, spawn_section_m_only, spawn_shortfall
from app.jobs.kb_ingest import spawn_kb_ingest
from app.jobs.kb_reclassify import (
    get_progress as get_reclassify_progress,
)
from app.jobs.kb_reclassify import (
    spawn_reclassify_all,
)
from app.jobs.payment_market_researcher import spawn_payment_market_research
from app.jobs.reviewer import (
    spawn_auto_review_revise_loop,
    spawn_reviewer_for_section,
    spawn_reviewer_loop,
)
from app.jobs.writer import spawn_writer_for_section, spawn_writer_team
from app.models import (
    AgentRun,
    ComplianceMatrixItem,
    GapAnalysis,
    KnowledgeBaseDocument,
    ProfileSuggestion,
    Proposal,
    ProposalSection,
    ReviewerFinding,
)
from app.services.findings import (
    build_directive_from_findings,
    get_accepted_findings_for_section,
)
from app.services.kb import (
    MAX_KB_BATCH_BYTES,
    MAX_KB_FILE_BYTES,
    KbUploadedFile,
    create_kb_documents,
    delete_kb_document,
    find_duplicate_documents,
)
from app.services.lessons import (
    approve_rule,
    archive_rule,
    delete_rule,
    get_category_action_rates,
    list_rules,
    update_rule_text,
)
from app.services.pdf_extract import extract_pdf_text, extract_text_for_path
from app.services.profile_suggestions import apply_suggestion, reject_suggestion
from app.services.proposals import (
    MAX_PROPOSAL_FILE_BYTES,
    MAX_PROPOSAL_PACKAGE_BYTES,
    UploadedFile,
    create_proposal_with_files,
    delete_proposal,
    find_duplicate_rfp_documents,
    reset_for_intake_retry,
)
from app.services.service_line import (
    SERVICE_LINE_PAYMENT_SYSTEMS,
    get_service_line,
)
from app.services.workflow import (
    approve_for_submission,
    archive_proposal,
    mark_submitted,
    sign_off_scope,
)
from app.ui._shared import _empty_state
from app.ui.layout import page_frame
from app.ui.tabs.amendments import _render_amendments_tab
from app.ui.tabs.completed_draft import _render_completed_draft_tab
from app.ui.tabs.compliance import _render_compliance_tab
from app.ui.tabs.cost import (
    _COST_BID_REC_VISUAL,
    _COST_POSITION_VISUAL,
    _COST_SCENARIO_VISUAL,
    _load_cost_deferred_sections,
    _render_cost_tab,
)
from app.ui.tabs.cost_review import (
    _render_cost_review_tab,
    _split_subject_from_finding_text,
)
from app.ui.tabs.draft import _render_draft_tab
from app.ui.tabs.evaluation_criteria import _render_evaluation_criteria_tab
from app.ui.tabs.final_polish import (
    _render_final_polish_tab,
)
from app.ui.tabs.findings import (
    _FINDING_CATEGORY_LABELS,
    _render_findings_tab,
)
from app.ui.tabs.gaps import (
    _render_gaps_tab,
)
from app.ui.tabs.outline import (
    _approve_outline,
    _generate_outline,
    _render_outline_tab,
)
from app.ui.tabs.spend import _render_spend_tab
from app.ui.tabs.submission_checklist import (
    _render_submission_checklist_tab,
)
from app.ui.tabs.team import (
    _render_team_tab,
)
from app.ui.tabs.timeline import _render_timeline_tab

log = logging.getLogger(__name__)


# Outcome → Quasar palette color for chips on the proposal list rows
# and Outcome panel. Mirrors `_OUTCOME_CHIP_COLOR` in
# `app/ui/tabs/outcome.py`.
_OUTCOME_CHIP_COLOR = {
    "won": "green-3",
    "lost": "red-3",
    "no_award": "blue-grey-3",
    "withdrawn": "amber-3",
    "pending": "blue-3",
}


# ----- Pipeline (Home) ----------------------------------------------------------


@ui.page("/")
def home() -> None:
    with page_frame("Pipeline"):
        ui.label("Proposals in flight").classes("text-xl font-semibold")

        # Lazy import — keep the page-render path import cost light, and
        # avoid pulling proposal_outcomes into module-load before the
        # alembic upgrade has happened.
        from app.services.proposal_outcomes import get_win_rate_summary

        # ── Win-rate summary block (hidden when no outcome data) ─────
        filter_state: dict = {"service_line": None}
        summary_container = ui.column().classes("w-full")

        def render_summary() -> None:
            summary_container.clear()
            summary = get_win_rate_summary(service_line=filter_state["service_line"])
            if summary["total"] == 0:
                return  # hide when no decided outcomes
            with summary_container:
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-3 w-full"):
                        wr = summary["win_rate_pct"]
                        wr_str = f"{wr:.0f}%" if wr is not None else "—"
                        ui.label(
                            f"Win rate: {wr_str} ({summary['won']} of {summary['won'] + summary['lost']})"
                        ).classes("text-base font-medium")
                        ui.chip(f"won: {summary['won']}").props("color=green-3 text-color=black dense")
                        ui.chip(f"lost: {summary['lost']}").props("color=red-3 text-color=black dense")
                        ui.chip(f"no_award: {summary['no_award']}").props(
                            "color=blue-grey-3 text-color=black dense"
                        )
                        ui.chip(f"withdrawn: {summary['withdrawn']}").props(
                            "color=amber-3 text-color=black dense"
                        )
                        ui.select(
                            options={
                                None: "All service lines",
                                "it_services": "IT services",
                                "payment_systems": "Payment systems",
                            },
                            value=filter_state["service_line"],
                            on_change=lambda e: (
                                filter_state.update(service_line=e.value),
                                render_summary(),
                            ),
                        ).classes("w-48 ml-auto")

        render_summary()

        with SessionLocal() as db:
            proposals = (
                db.execute(select(Proposal).order_by(Proposal.updated_at.desc()).limit(50)).scalars().all()
            )

        if not proposals:
            _empty_state("No proposals yet. Start one from the New Proposal page.")
            return

        # Container so we can re-render after a delete without reloading.
        list_container = ui.column().classes("w-full")

        def _confirm_delete(pid: int, title: str) -> None:
            with ui.dialog() as dlg, ui.card():
                ui.label(f"Delete proposal #{pid}?").classes("text-base font-medium")
                ui.label(title).classes("text-sm opacity-70")
                ui.label(
                    "This removes compliance items, agent runs, the RFP package, "
                    "and the on-disk files. Cannot be undone."
                ).classes("text-xs opacity-60 pt-2")
                with ui.row().classes("w-full justify-end gap-2 pt-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")

                    def do_delete() -> None:
                        with session_scope() as db:
                            result = delete_proposal(db, pid)
                        dlg.close()
                        if result.get("deleted"):
                            ui.notify(f"Deleted proposal #{pid}.", type="positive")
                            render_list()
                            # Refresh the win-rate summary chip — the
                            # cascade removed the deleted proposal's
                            # outcome row, so the totals shift.
                            render_summary()
                        else:
                            ui.notify(
                                f"Could not delete proposal #{pid} ({result.get('reason')}).",
                                type="negative",
                            )

                    ui.button("Delete", on_click=do_delete, icon="delete").props("color=negative")
            dlg.open()

        def render_list() -> None:
            list_container.clear()
            with SessionLocal() as db:
                # joinedload avoids the per-row N+1 outcome lookup we'd
                # otherwise trigger when reading p.outcome below.
                proposals = (
                    db.execute(
                        select(Proposal)
                        .options(joinedload(Proposal.outcome))
                        .order_by(Proposal.updated_at.desc())
                        .limit(50)
                    )
                    .scalars()
                    .all()
                )
                snapshot = [
                    {
                        "id": p.id,
                        "title": p.title,
                        "agency": p.agency,
                        "due": p.due_date.isoformat() if p.due_date else "—",
                        "status": p.status.value if hasattr(p.status, "value") else str(p.status),
                        "updated": p.updated_at.strftime("%Y-%m-%d %H:%M"),
                        "outcome": (
                            (
                                p.outcome.outcome.value
                                if hasattr(p.outcome.outcome, "value")
                                else str(p.outcome.outcome)
                            )
                            if p.outcome is not None
                            else None
                        ),
                    }
                    for p in proposals
                ]
            with list_container:
                if not snapshot:
                    _empty_state("No proposals yet. Start one from the New Proposal page.")
                    return
                # Render as a column of clickable cards instead of a table so each
                # row has its own delete button.
                with ui.column().classes("w-full gap-2"):
                    for s in snapshot:
                        with (
                            ui.card()
                            .classes("w-full hover:bg-slate-50 cursor-pointer")
                            .on(
                                "click",
                                lambda pid=s["id"]: ui.navigate.to(f"/proposals/{pid}"),
                            )
                        ):
                            with ui.row().classes("items-center w-full gap-3"):
                                with ui.column().classes("gap-0 flex-1"):
                                    ui.label(s["title"]).classes("font-medium")
                                    ui.label(
                                        f"{s['agency'] or '—'} · due {s['due']} · "
                                        f"status: {_status_label(s['status'])} · updated {s['updated']}"
                                    ).classes("text-xs opacity-60")
                                # Outcome badge — only when a non-PENDING
                                # outcome row exists.
                                if s.get("outcome") and s["outcome"] != "pending":
                                    color = _OUTCOME_CHIP_COLOR.get(
                                        s["outcome"],
                                        "blue-grey-3",
                                    )
                                    ui.chip(s["outcome"]).props(f"color={color} text-color=black dense")
                                ui.label(f"#{s['id']}").classes("text-xs opacity-50 font-mono")
                                # Archived proposals are immutable audit records;
                                # deletion is unavailable even from the list page.
                                if s["status"] != ProposalStatus.ARCHIVED.value:
                                    # stop_propagation so click doesn't navigate the row.
                                    ui.button(
                                        icon="delete_outline",
                                        on_click=lambda e, pid=s["id"], t=s["title"]: _confirm_delete(pid, t),
                                    ).props("flat dense color=negative").on(
                                        "click.stop", lambda: None
                                    )

        render_list()


# ----- New Proposal --------------------------------------------------------------

_METADATA_EXTRACT_SUFFIXES = {".pdf", ".docx", ".xlsx"}


def _run_metadata_extraction_sync(name: str, data: bytes) -> ExtractedMetadata:
    """Save bytes to a tempfile, extract text, run Haiku metadata pass.
    Sync — caller wraps in asyncio.to_thread to avoid blocking the event loop.
    Dispatches by file suffix; PDFs are capped at 15 pages for cost control.
    """
    suffix = Path(name).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(data)
        tmp_path = Path(tf.name)
    try:
        if suffix == ".pdf":
            text, _ = extract_pdf_text(tmp_path, max_pages=15)
        else:
            text, _ = extract_text_for_path(tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            log.warning("failed to delete tempfile %s", tmp_path)
    return extract_metadata_from_text(text)


def _open_cost_estimate_dialog(staged: dict[str, bytes]) -> None:
    """Show a per-phase USD estimate for the staged RFP package. Pulls
    the heuristic from app.services.cost_estimate; the New Proposal
    page's Estimate cost button calls this."""
    from app.services.cost_estimate import estimate_pipeline_cost

    est = estimate_pipeline_cost(staged)

    def _row(label: str, cost: float, *, bold: bool = False) -> None:
        weight = "font-medium" if bold else ""
        with ui.row().classes("w-full justify-between items-center"):
            ui.label(label).classes(f"text-sm {weight}")
            ui.label(f"${cost:.2f}").classes(f"text-sm font-mono {weight}")

    with ui.dialog() as dlg, ui.card().classes("min-w-[36rem] max-w-[44rem]"):
        ui.label("Estimated API cost").classes("text-base font-semibold")
        ui.label(
            f"~{est.pages_estimated} page(s), ~{est.tokens_estimated:,} "
            f"tokens, ~{est.requirements_estimated} compliance items "
            f"expected, ~{est.sections_estimated} outline sections. "
            f"Heuristic — actual cost varies ±50%."
        ).classes("text-xs opacity-70 pt-1")

        # Phase 1 — runs immediately on Run-click
        with ui.card().classes("w-full bg-blue-50 border-l-4 border-blue-400 shadow-none mt-2"):
            ui.label("INTAKE — runs when you click Run").classes(
                "text-[10px] font-semibold tracking-wider text-blue-800 uppercase"
            )
            with ui.column().classes("gap-1 pt-1 w-full"):
                _row("Metadata extraction (lightweight model)", est.intake_metadata)
                _row(
                    f"Requirement extraction ({est.compliance_extraction_model})",
                    est.compliance_matrix,
                )
                _row(
                    f"Independent + source review ({est.compliance_review_model})",
                    est.compliance_review,
                )
                _row(
                    f"Fallback contingency ({est.compliance_fallback_model})",
                    est.compliance_fallback_contingency,
                )
                _row("Shortfall Strategist", est.shortfall)
                ui.separator()
                _row("Intake subtotal", est.intake_total, bold=True)

        # Phase 2 — user-driven, runs later
        with ui.card().classes("w-full bg-slate-50 border-l-4 border-slate-400 shadow-none mt-2"):
            ui.label("FULL PROPOSAL — when you go all the way").classes(
                "text-[10px] font-semibold tracking-wider text-slate-800 uppercase"
            )
            with ui.column().classes("gap-1 pt-1 w-full"):
                _row("Outline Agent", est.outline)
                _row(
                    f"Writer Team — initial draft ({est.sections_estimated} sections)",
                    est.writer_initial,
                )
                _row(
                    f"Reviewer auto-loop (A+B, ~{3} passes/section)",
                    est.reviewer_loop,
                )
                _row(
                    "Writer revisions (scheduled models)",
                    est.writer_revisions,
                )
                ui.separator()
                _row(
                    "Estimated total (intake + draft + review)",
                    est.pipeline_total,
                    bold=True,
                )

        ui.label(
            "Pipeline phases past intake are run on demand: you click "
            "Approve outline / Run Writer Team / Run Auto-loop manually. "
            "You can stop and inspect at any step."
        ).classes("text-xs opacity-70 pt-2")

        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


@ui.page("/proposals/new")
def new_proposal() -> None:
    # Per-page session state.
    staged: dict[str, bytes] = {}
    staged_cost_matrices: set[str] = set()
    staged_possible_cost_matrices: set[str] = set()
    extraction_done = {"value": False}  # mutable flag — only run once per page

    with page_frame("New Proposal"):
        ui.label("Upload an RFP package").classes("text-xl font-semibold")
        ui.label(
            "Drag-drop the RFP package (PDF / DOCX / XLSX). "
            "Title, agency, NAICS, and due date will be auto-extracted from the "
            "first PDF / DOCX / XLSX — review and edit as needed before clicking Run."
        ).classes("text-sm opacity-70")

        with ui.card().classes("w-full"):
            staged_list = ui.column().classes("w-full")
            staged_list.props["role"] = "list"
            staged_list.props["aria-label"] = "Staged RFP files"

            def render_staged() -> None:
                staged_list.clear()
                with staged_list:
                    if not staged:
                        ui.label("No files staged.").classes("text-sm opacity-50")
                        return
                    for name, data in staged.items():
                        staged_row = ui.row().classes("items-center gap-2 w-full")
                        staged_row.props["role"] = "listitem"
                        staged_row.props["aria-label"] = f"Staged RFP file {name}"
                        with staged_row:
                            ui.icon("description")
                            ui.label(name).classes("flex-1")
                            if name in staged_cost_matrices:
                                ui.badge(
                                    "Cost matrix — held for pricing",
                                    color="amber",
                                ).props("outline").tooltip(
                                    "Detected now; the original workbook stays unchanged "
                                    "until cost mapping and review are complete."
                                )
                            elif name in staged_possible_cost_matrices:
                                ui.badge(
                                    "Possible cost matrix — review needed",
                                    color="amber",
                                ).props("outline").tooltip(
                                    "The workbook stays in ordinary intake until you "
                                    "confirm whether it is a fillable cost matrix."
                                )
                            ui.label(f"{len(data) / 1024:.1f} KB").classes("text-xs opacity-60")
                            def remove_staged(n: str = name) -> None:
                                staged.pop(n, None)
                                staged_cost_matrices.discard(n)
                                staged_possible_cost_matrices.discard(n)
                                render_staged()
                            remove_button = ui.button(
                                icon="close",
                                on_click=remove_staged,
                            ).props("flat dense")
                            remove_button.props["aria-label"] = (
                                f"Remove staged RFP file {name}"
                            )

            render_staged()

            extraction_status = ui.label("").classes("text-sm italic")

            async def maybe_extract(name: str, data: bytes) -> None:
                """Trigger metadata extraction once on the first supported upload.
                Supports PDF / DOCX / XLSX; shows a visible 'skipped' message for
                anything else (e.g. .zip) so it's never a silent no-op."""
                if extraction_done["value"]:
                    return
                suffix = Path(name).suffix.lower()
                if suffix not in _METADATA_EXTRACT_SUFFIXES:
                    extraction_status.set_text(
                        f"⚠ Auto-extract skipped — {suffix or 'this file type'} not supported. "
                        "Fill in fields manually below, or upload a PDF / DOCX / XLSX."
                    )
                    extraction_status.classes(replace="text-sm italic text-amber-700")
                    return
                extraction_done["value"] = True
                extraction_status.set_text(
                    f"Extracting metadata from {suffix.lstrip('.').upper()}…"
                )
                extraction_status.classes(replace="text-sm italic text-blue-700")
                try:
                    meta = await asyncio.to_thread(_run_metadata_extraction_sync, name, data)
                except Exception as exc:
                    log.exception("metadata extraction failed")
                    extraction_status.set_text(f"⚠ Extraction failed: {exc}")
                    extraction_status.classes(replace="text-sm italic text-amber-700")
                    extraction_done["value"] = False  # allow retry on next upload
                    return

                if not meta.has_anything:
                    extraction_status.set_text("⚠ Could not extract metadata — fill in manually below.")
                    extraction_status.classes(replace="text-sm italic text-amber-700")
                    return

                # Only fill empty fields — don't clobber anything the user already typed.
                filled: list[str] = []
                if meta.title and not (title_in.value or "").strip():
                    title_in.value = meta.title
                    filled.append("title")
                if meta.agency and not (agency_in.value or "").strip():
                    agency_in.value = meta.agency
                    filled.append("agency")
                if meta.naics and not (naics_in.value or "").strip():
                    naics_in.value = meta.naics
                    filled.append("NAICS")
                if meta.due_date and not (due_in.value or "").strip():
                    due_in.value = meta.due_date
                    filled.append("due date")
                if meta.solicitation_number:
                    existing = (notes_in.value or "").strip()
                    sol_line = f"Solicitation #: {meta.solicitation_number}"
                    if sol_line not in existing:
                        notes_in.value = (sol_line + "\n" + existing).strip()
                        filled.append("solicitation #")
                # Service-line dropdown — Haiku always picks one of the
                # registered IDs (or returns None if it can't classify).
                # We update only when the detection differs from the
                # current dropdown value, so the toast doesn't clutter
                # the common 'detected the default' case AND the user's
                # in-flight manual change isn't clobbered when the
                # detection happens to match what they already chose.
                if meta.service_line and service_line_in.value != meta.service_line:
                    service_line_in.value = meta.service_line
                    label = _service_line_options.get(
                        meta.service_line,
                        meta.service_line,
                    )
                    filled.append(f"service line ({label})")

                if filled:
                    extraction_status.set_text(f"✓ Auto-filled: {', '.join(filled)}")
                    extraction_status.classes(replace="text-sm italic text-green-700")
                else:
                    extraction_status.set_text("✓ Extraction ran — no new fields to fill.")
                    extraction_status.classes(replace="text-sm italic text-slate-500")

            async def on_upload(e) -> None:
                try:
                    data = await e.file.read()
                    name = e.file.name
                    staged[name] = data
                    staged_cost_matrices.discard(name)
                    staged_possible_cost_matrices.discard(name)
                    if Path(name).suffix.lower() == ".xlsx":
                        from app.services.cost_matrix import try_inspect_cost_matrix
                        analysis = await asyncio.to_thread(
                            try_inspect_cost_matrix,
                            name,
                            data,
                        )
                        if analysis is not None:
                            classification = analysis.get("classification") or {}
                            if classification.get("is_cost_matrix"):
                                staged_cost_matrices.add(name)
                            elif classification.get("possible_cost_matrix"):
                                staged_possible_cost_matrices.add(name)
                    ui.notify(
                        f"Staged {name} ({len(data) / 1024:.1f} KB)",
                        type="positive",
                    )
                    render_staged()
                    if name in staged_cost_matrices:
                        extraction_status.set_text(
                            "✓ Cost matrix detected and preserved. It will be mapped "
                            "after requirements and pricing review; upload the "
                            "solicitation document for metadata extraction."
                        )
                        extraction_status.classes(
                            replace="text-sm italic text-amber-700"
                        )
                    else:
                        await maybe_extract(name, data)
                        if name in staged_possible_cost_matrices:
                            extraction_status.set_text(
                                "Possible cost matrix detected. It will remain in normal "
                                "requirements intake until you confirm or dismiss it."
                            )
                            extraction_status.classes(
                                replace="text-sm italic text-amber-700"
                            )
                except Exception as exc:
                    log.exception("upload handler failed")
                    ui.notify(f"Upload failed: {exc}", type="negative")

            ui.upload(
                multiple=True,
                max_file_size=MAX_PROPOSAL_FILE_BYTES,
                max_total_size=MAX_PROPOSAL_PACKAGE_BYTES,
                auto_upload=True,
                on_upload=on_upload,
                on_rejected=lambda: ui.notify(
                    "Upload rejected: files must be 50 MB or less each and 200 MB or less total.",
                    type="negative",
                ),
                label="Drop RFP files here",
            ).props("accept=.pdf,.docx,.xlsx").classes("w-full")

            with ui.row().classes("w-full gap-3 pt-2"):
                title_in = ui.input("Proposal title").classes("flex-1")
                agency_in = ui.input("Agency").classes("flex-1")
            with ui.row().classes("w-full gap-3"):
                naics_in = ui.input("NAICS").classes("w-40")
                due_in = ui.input("Due date", placeholder="YYYY-MM-DD").classes("w-40")
                role_in = ui.select(["prime", "sub"], value="prime", label="Role").classes("w-40")
            with ui.row().classes("w-full gap-3"):
                from app.services.service_line import (
                    DEFAULT_SERVICE_LINE,
                    list_service_lines,
                )

                _service_line_options = {sl["id"]: sl["label"] for sl in list_service_lines()}
                service_line_in = (
                    ui.select(
                        options=_service_line_options,
                        value=DEFAULT_SERVICE_LINE,
                        label="Service line",
                    )
                    .classes("flex-1")
                    .tooltip(
                        "IT Services = labor-catalog cost flow (Cost Analyst → "
                        "H/M/L scenarios). Payment Systems = fee-schedule flow "
                        "(skips Cost Analyst; Cost Writer renders directly from "
                        "data/pricing/payment_systems.json). Add new categories "
                        "via app/services/service_line.py SERVICE_LINES + "
                        "data/pricing/<id>.json."
                    )
                )
            notes_in = ui.textarea("Notes (optional)").classes("w-full")

            def _do_run(skip_dup_filenames: set[str] | None = None) -> None:
                """Inner implementation. Builds the file list (filtering out skipped
                duplicates), creates the proposal, and navigates to the progress page."""
                files = [
                    UploadedFile(filename=n, content=d)
                    for n, d in staged.items()
                    if skip_dup_filenames is None or n not in skip_dup_filenames
                ]
                if not files:
                    ui.notify(
                        "All files were duplicates and skipped. Nothing to upload.",
                        type="warning",
                    )
                    return
                title = (title_in.value or "").strip()
                try:
                    with SessionLocal() as db:
                        proposal = create_proposal_with_files(
                            db,
                            title=title,
                            files=files,
                            agency=agency_in.value,
                            naics=naics_in.value,
                            due_date_str=due_in.value,
                            role=role_in.value or "prime",
                            notes=notes_in.value,
                        )
                        db.commit()
                        proposal_id = proposal.id
                except Exception as exc:
                    log.exception("create proposal failed")
                    ui.notify(f"Failed to create proposal: {exc}", type="negative")
                    return
                # Persist the service-line tag so downstream agents
                # (Cost Writer, Cost Market Researcher, format_cost_
                # build_block_for_writer) see the right value before
                # intake starts.
                try:
                    from app.services.service_line import set_service_line

                    set_service_line(proposal_id, service_line_in.value or DEFAULT_SERVICE_LINE)
                except Exception:
                    log.exception(
                        "failed to set service_line=%s for proposal %d",
                        service_line_in.value,
                        proposal_id,
                    )
                # Kick off the background intake pipeline (PDF parse + Compliance Matrix).
                spawn_intake(proposal_id)
                ui.notify(f"Proposal #{proposal_id} created — pipeline running.", type="positive")
                staged.clear()
                staged_cost_matrices.clear()
                staged_possible_cost_matrices.clear()
                ui.navigate.to(f"/proposals/{proposal_id}/progress")

            def on_run() -> None:
                """Public run handler. Checks for duplicate RFP files first; if any
                found, opens a skip / upload-anyway dialog. Otherwise delegates
                straight to _do_run."""
                if not staged:
                    ui.notify("Add at least one file before running.", type="warning")
                    return
                title = (title_in.value or "").strip()
                if not title:
                    ui.notify("Proposal title is required — auto-fill missed it; type one.", type="warning")
                    return

                # Build candidate files just for the duplicate check.
                candidates = [UploadedFile(filename=n, content=d) for n, d in staged.items()]
                with SessionLocal() as db:
                    dups = find_duplicate_rfp_documents(db, candidates)
                    dup_info: dict[str, dict] = {}
                    for n, doc in dups.items():
                        try:
                            proposal_title = (
                                doc.rfp_package.proposal.title
                                if (doc.rfp_package and doc.rfp_package.proposal)
                                else None
                            )
                        except Exception:
                            proposal_title = None
                        dup_info[n] = {
                            "id": doc.id,
                            "filename": doc.filename,
                            "title": proposal_title or doc.filename,
                        }

                if not dup_info:
                    _do_run()
                    return

                # Duplicate(s) detected — ask the user.
                with ui.dialog() as dlg, ui.card().classes("max-w-2xl"):
                    n_dups = len(dup_info)
                    ui.label(
                        f"{n_dups} of these file(s) match RFP files in existing proposals (same content):"
                    ).classes("text-base font-medium")
                    with ui.column().classes("gap-1 pt-2"):
                        for new_name, existing in dup_info.items():
                            ui.label(
                                f"• {new_name}  →  matches RFP file #{existing['id']} "
                                f"from proposal '{existing['title']}'"
                            ).classes("text-sm")
                    ui.label(
                        "Recommended: skip the duplicates so your proposals stay clean. "
                        "If the documents have changed and you want both versions, "
                        "upload anyway."
                    ).classes("text-xs opacity-60 pt-2")

                    with ui.row().classes("w-full justify-end gap-2 pt-3"):
                        ui.button("Cancel", on_click=dlg.close).props("flat")

                        def skip_dups() -> None:
                            dlg.close()
                            _do_run(skip_dup_filenames=set(dup_info.keys()))

                        def upload_anyway() -> None:
                            dlg.close()
                            _do_run()

                        ui.button("Skip duplicates", icon="skip_next", on_click=skip_dups).props(
                            "color=primary"
                        )
                        ui.button("Upload anyway", icon="warning_amber", on_click=upload_anyway).props(
                            "flat color=amber-9"
                        )
                dlg.open()

            def on_estimate_cost() -> None:
                if not staged:
                    ui.notify(
                        "Add at least one file first — the estimate is based on uploaded file size.",
                        type="warning",
                    )
                    return
                _open_cost_estimate_dialog(staged)

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(
                    "Estimate cost",
                    icon="calculate",
                    on_click=on_estimate_cost,
                ).props("flat").tooltip("Preview LLM API spend for the intake + full pipeline.")
                ui.button("Run", icon="play_arrow", on_click=on_run).props("color=primary")


# ----- Run Progress --------------------------------------------------------------

# Maps proposal.status → (icon, color class, "are we still working" flag, label).
_STATUS_VISUAL = {
    "intaking": ("autorenew", "text-blue-700", True, "Intaking"),
    "awaiting_scope_signoff": ("how_to_reg", "text-amber-700", False, "Awaiting scope sign-off"),
    "drafting": ("edit_note", "text-blue-700", False, "Drafting"),
    "awaiting_outline_approval": ("rule_folder", "text-amber-700", False, "Awaiting outline approval"),
    # Phase 2B gates between outline approval and writer team.
    "awaiting_team_approval": ("groups", "text-amber-700", False, "Awaiting team approval"),
    "awaiting_cost_build": ("payments", "text-amber-700", False, "Awaiting cost build"),
    "awaiting_draft": ("auto_stories", "text-amber-700", False, "Ready to draft"),
    "draft_in_progress": ("auto_stories", "text-blue-700", True, "Draft in progress"),
    "draft_ready": ("article", "text-green-700", False, "Draft ready"),
    "reviewing": ("rate_review", "text-blue-700", True, "Reviewing"),
    "pricing": ("payments", "text-blue-700", True, "Pricing"),
    "awaiting_approval": ("verified_user", "text-amber-700", False, "Awaiting approval"),
    "approved": ("check_circle", "text-green-700", False, "Approved"),
    "submitted": ("send", "text-green-700", False, "Submitted"),
    "archived": ("inventory_2", "text-slate-500", False, "Archived"),
}


def _status_label(value: str) -> str:
    visual = _STATUS_VISUAL.get(value)
    return visual[3] if visual else value.replace("_", " ").title()


def _agent_run_status_value(run: Any) -> str:
    status = getattr(run, "status", "")
    return status.value if hasattr(status, "value") else str(status)


def _progress_run_visual(run: Any, index: int) -> tuple[str, str]:
    """Return the Progress-page icon/color for an AgentRun row.

    Persisted status is authoritative. In particular, a failed `_stage` row
    must render red even though ordinary stage rows use their trailing ellipsis
    only to distinguish active progress from completed progress.
    """
    status = _agent_run_status_value(run)
    if status == "failed":
        return "error", "text-red-700"
    if status == "cancelled":
        return "cancel", "text-amber-700"
    if getattr(run, "agent_name", "") == "_stage":
        message = (getattr(run, "error_text", "") or "").rstrip()
        if message.startswith("⚠"):
            return "warning", "text-amber-700"
        if message.endswith("…") and index == 0:
            return "info", "text-blue-600"
        return "check_circle", "text-green-700"
    if status == "completed":
        return "check_circle", "text-green-700"
    return "autorenew", "text-blue-600"


def _model_provider_label(model: str | None) -> str:
    """Human-readable provider derived from the persisted exact model ID."""

    value = (model or "").strip()
    if value.startswith("claude-"):
        return "Anthropic"
    if value.startswith("gemini-"):
        return "Google"
    if value.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return "OpenAI"
    return "Model"


def _requirements_review_visual(status: str) -> tuple[str, str, str]:
    return {
        "pending": ("schedule", "text-blue-700", "Queued for review"),
        "extracting": ("description", "text-blue-700", "Extracting"),
        "reviewing": ("fact_check", "text-blue-700", "Reviewing"),
        "complete": ("verified", "text-green-700", "Complete"),
        "review_required": (
            "warning",
            "text-amber-700",
            "Complete — human review needed",
        ),
        "degraded": (
            "warning",
            "text-amber-700",
            "Fallback used — human review needed",
        ),
        "not_applicable": (
            "grid_on",
            "text-slate-600",
            "Not a requirements source",
        ),
        "partial": ("error", "text-red-700", "Partial — review incomplete"),
        "failed": ("error", "text-red-700", "Failed — manual review required"),
        "unknown": ("error", "text-red-700", "Invalid review state — retry required"),
    }.get(status, ("schedule", "text-slate-500", "Waiting"))


def _requirements_review_from_structure(structure: object) -> dict:
    """Safely normalize durable review metadata for user-facing screens."""
    from app.services.requirements_review import normalize_requirements_review

    return normalize_requirements_review(structure)


@ui.page("/proposals/{proposal_id}/progress")
def run_progress(proposal_id: int) -> None:
    with page_frame(f"Run Progress · #{proposal_id}"):
        # Header card — proposal metadata. Snapshot at page load.
        with SessionLocal() as db:
            proposal = db.get(Proposal, proposal_id)
            if proposal is None:
                _empty_state(f"Proposal #{proposal_id} not found.", icon="error")
                return
            title = proposal.title
            agency = proposal.agency
            naics = proposal.naics
            due = proposal.due_date
            role = proposal.role
            pkg = proposal.rfp_package
            doc_summaries = [(d.filename, d.page_count) for d in pkg.documents] if pkg else []

        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.column().classes("gap-0"):
                    ui.label(title).classes("text-xl font-semibold")
                    with ui.row().classes("gap-4 text-sm opacity-70 flex-wrap"):
                        ui.label(f"Agency: {agency or '—'}")
                        ui.label(f"NAICS: {naics or '—'}")
                        ui.label(f"Due: {due.isoformat() if due else '—'}")
                        role_val = role.value if hasattr(role, "value") else str(role)
                        ui.label(f"Role: {role_val}")

                def on_retry() -> None:
                    result = reset_for_intake_retry(proposal_id)
                    if not result.get("ok"):
                        reason = result.get("reason")
                        if reason == "pipeline_active":
                            ui.notify(
                                "Intake still has recent activity. Retry is "
                                "blocked so a live pipeline is not erased.",
                                type="warning",
                                multi_line=True,
                                timeout=6000,
                            )
                        elif reason == "invalid_status":
                            ui.notify(
                                "Retry is only available for a stuck or failed "
                                "intake, or an incomplete requirements review.",
                                type="warning",
                            )
                        else:
                            ui.notify("Proposal not found.", type="negative")
                        return
                    spawn_intake(proposal_id)
                    ui.notify("Retry started — pipeline re-running.", type="positive")

                with ui.row().classes("gap-2"):
                    ui.button(
                        "Retry pipeline",
                        icon="refresh",
                        on_click=on_retry,
                    ).props("flat")
                    # Cancel only when the auto review-revise loop is active.
                    # The button shows for any proposal but only does something
                    # when there's a registered cancel event.
                    ui.button(
                        "Cancel auto-review",
                        icon="stop_circle",
                        on_click=lambda: _cancel_auto_review(proposal_id),
                    ).props("flat color=red-7")
            # Live status badge
            status_row = ui.row().classes("items-center gap-2 pt-1")

        # Busy banner — visible only while a stage is in flight. Created once,
        # toggled and updated by refresh() to avoid re-render flicker.
        busy_banner = ui.card().classes("w-full bg-blue-50 border-l-4 border-blue-500")
        with busy_banner:
            with ui.row().classes("items-center gap-4 w-full"):
                ui.spinner("dots", size="lg", color="primary")
                with ui.column().classes("gap-0 flex-1"):
                    busy_msg = ui.label("Working…").classes("text-base font-medium text-blue-900")
                    busy_elapsed = ui.label("elapsed 0:00").classes("text-xs opacity-70 font-mono")
                ui.label("Polling every 2s").classes("text-xs opacity-50")
        busy_banner.set_visibility(False)

        # File list card — also snapshot.
        with ui.card().classes("w-full"):
            ui.label(f"RFP package — {len(doc_summaries)} file(s)").classes("text-base font-medium")
            if doc_summaries:
                rows = [{"filename": fn, "pages": pc or "—"} for fn, pc in doc_summaries]
                cols = [
                    {"name": "filename", "label": "File", "field": "filename", "align": "left"},
                    {"name": "pages", "label": "Pages", "field": "pages"},
                ]
                ui.table(columns=cols, rows=rows, row_key="filename").classes("w-full")
            else:
                ui.label("No files uploaded.").classes("text-sm opacity-60")

        # Durable per-document requirements-review state. Unlike transient
        # stage text, this is keyed to each package document and remains
        # accurate when parallel files finish out of order.
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Requirements review").classes("text-base font-medium")
                requirements_summary = ui.label("Waiting to start").classes(
                    "text-sm opacity-70"
                )
            ui.label(
                "Configured source extraction, independent review, source-completeness "
                "coverage, and any fallback are tracked separately for each file."
            ).classes("text-xs opacity-60")
            ui.label(
                "Per-file cost is the reported review estimate; pipeline activity "
                "also shows extraction and failed-call usage when available."
            ).classes("text-xs opacity-50")
            requirements_log = ui.column().classes("w-full gap-1 pt-2")

        # Live cards — refreshed by polling.
        with ui.card().classes("w-full"):
            ui.label("Pipeline activity").classes("text-base font-medium")
            stage_log = ui.column().classes("w-full")

        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Extracted requirements").classes("text-base font-medium")
                matrix_count_label = ui.label("0 items").classes("text-sm opacity-70")
            ui.label(
                "Package-wide total across all source files. "
                "Full matrix lands on the Proposal Review page when intake completes."
            ).classes("text-xs opacity-60")
            review_link = ui.row().classes("pt-2")

        def refresh() -> None:
            # Re-read status, recent agent_runs, and compliance matrix count.
            with SessionLocal() as db:
                p = db.get(Proposal, proposal_id)
                if p is None:
                    return
                status_str = p.status.value if hasattr(p.status, "value") else str(p.status)

                # Recent agent runs / stage messages, newest first.
                recent_runs = (
                    db.execute(
                        select(AgentRun)
                        .where(AgentRun.proposal_id == proposal_id)
                        .order_by(AgentRun.id.desc())
                        .limit(40)
                    )
                    .scalars()
                    .all()
                )
                document_reviews = []
                if p.rfp_package is not None:
                    for document in sorted(
                        p.rfp_package.documents, key=lambda value: value.id
                    ):
                        review = _requirements_review_from_structure(
                            document.structure_json
                        )
                        document_reviews.append(
                            {
                                "id": document.id,
                                "filename": document.filename,
                                "review": review,
                            }
                        )
                matrix_count = (
                    db.query(ComplianceMatrixItem)
                    .filter(
                        ComplianceMatrixItem.proposal_id == proposal_id,
                        ComplianceMatrixItem.status == "active",
                    )
                    .count()
                )

            # Durable source-by-source review state.
            active_review_docs = [
                row
                for row in document_reviews
                if row["review"].get("status")
                in {"pending", "extracting", "reviewing"}
            ]
            terminal_review_docs = [
                row
                for row in document_reviews
                if row["review"].get("status")
                in {
                    "complete",
                    "review_required",
                    "degraded",
                    "not_applicable",
                    "partial",
                    "failed",
                    "unknown",
                }
            ]
            warning_review_docs = [
                row
                for row in terminal_review_docs
                if row["review"].get("status")
                in {"review_required", "degraded", "partial", "failed", "unknown"}
            ]
            if active_review_docs:
                requirements_summary.set_text(
                    f"{len(active_review_docs)} queued/in progress · "
                    f"{len(terminal_review_docs)}/{len(document_reviews)} finished"
                )
            elif warning_review_docs:
                requirements_summary.set_text(
                    f"{len(terminal_review_docs)}/{len(document_reviews)} finished · "
                    f"{len(warning_review_docs)} need attention"
                )
            elif terminal_review_docs:
                requirements_summary.set_text(
                    f"{len(terminal_review_docs)}/{len(document_reviews)} complete"
                )
            else:
                requirements_summary.set_text("Waiting to start")

            requirements_log.clear()
            with requirements_log:
                for row in document_reviews:
                    review = row["review"]
                    status = str(review.get("status") or "waiting")
                    icon_name, color_cls, status_label = _requirements_review_visual(
                        status
                    )
                    extraction = dict(review.get("extraction") or {})
                    classification = dict(review.get("classification") or {})
                    completeness = dict(review.get("completeness") or {})
                    with ui.row().classes("items-start gap-3 w-full py-1"):
                        ui.icon(icon_name).classes(color_cls)
                        with ui.column().classes("gap-0 flex-1"):
                            ui.label(
                                f"{row['filename']} — {status_label}"
                            ).classes(f"text-sm font-medium {color_cls}")
                            detail_parts: list[str] = []
                            extraction_model = extraction.get("model")
                            if extraction_model:
                                detail_parts.append(
                                    f"extractor {_model_provider_label(extraction_model)} · "
                                    f"{extraction_model}"
                                )
                            primary_model = classification.get("primary_model")
                            if primary_model:
                                detail_parts.append(
                                    f"reviewer {_model_provider_label(primary_model)} · "
                                    f"{primary_model}"
                                )
                            if classification.get("total_count") is not None:
                                detail_parts.append(
                                    f"items {classification.get('reviewed_count', 0)}/"
                                    f"{classification.get('total_count', 0)}"
                                )
                            extraction_coverage = dict(
                                extraction.get("coverage") or {}
                            )
                            if extraction_coverage.get("source_chunks_total"):
                                detail_parts.append(
                                    "extraction chunks "
                                    f"{extraction_coverage.get('source_chunks_completed', 0)}/"
                                    f"{extraction_coverage.get('source_chunks_total', 0)}"
                                )
                            if extraction_coverage.get("state") not in {
                                None,
                                "complete",
                            }:
                                detail_parts.append(
                                    "extraction coverage "
                                    + str(extraction_coverage.get("state"))
                                )
                            auto_applied_count = int(
                                classification.get("auto_applied_count") or 0
                            )
                            if auto_applied_count:
                                detail_parts.append(
                                    f"{auto_applied_count} auto-corrected"
                                )
                            human_review_count = (
                                int(classification.get("manual_review_count") or 0)
                                + int(
                                    completeness.get(
                                        "manual_review_candidate_count"
                                    )
                                    or 0
                                )
                                + int(
                                    completeness.get("uncertain_passage_count") or 0
                                )
                            )
                            if human_review_count:
                                detail_parts.append(
                                    f"{human_review_count} need human review"
                                )
                            if completeness.get("source_units_total") is not None:
                                detail_parts.append(
                                    f"source units {completeness.get('reviewed_units', 0)}/"
                                    f"{completeness.get('source_units_total', 0)}"
                                )
                            if classification.get("fallback_used") or completeness.get(
                                "fallback_used"
                            ):
                                fallback_model = classification.get(
                                    "fallback_model"
                                ) or completeness.get("fallback_model")
                                detail_parts.append(f"fallback {fallback_model}")
                            if detail_parts:
                                ui.label(" · ".join(detail_parts)).classes(
                                    "text-xs opacity-65"
                                )
                            reason = str(review.get("reason") or "").strip()
                            if reason:
                                ui.label(reason).classes("text-xs opacity-65")
                        review_cost = float(
                            review.get("known_review_cost_usd")
                            or review.get("estimated_cost_usd")
                            or 0
                        )
                        if review_cost > 0:
                            ui.label(f"review est. ${review_cost:.4f}").classes(
                                "text-xs opacity-60 font-mono"
                            )

            # Status badge — clickable, navigates to Proposal Review.
            status_row.clear()
            visual = _STATUS_VISUAL.get(status_str, ("help_outline", "text-slate-500", False, status_str))
            icon, color_cls, status_busy, label = visual
            with status_row:
                with (
                    ui.row()
                    .classes("items-center gap-2 cursor-pointer hover:underline")
                    .on(
                        "click",
                        lambda: ui.navigate.to(f"/proposals/{proposal_id}"),
                    )
                ):
                    ui.icon(icon).classes(color_cls)
                    ui.label(f"Status: {label}").classes(f"font-medium {color_cls}")
                    ui.icon("open_in_new").classes(f"text-sm opacity-50 {color_cls}")

            # Busy banner — show whenever a long-running job is in flight.
            # Four independent signals OR'd together:
            #   1. status_busy from _STATUS_VISUAL (intaking /
            #      draft_in_progress / reviewing / pricing).
            #   2. loop_running from the cancellation registry (the
            #      auto-review-revise loop registers itself before
            #      flipping status, so the banner stays accurate during
            #      the transition window).
            #   3. active_sections from the cancellation registry — set
            #      whenever run_writer_for_section() is processing a
            #      section. The canonical "regen in flight" signal; works
            #      even when status=draft_ready and parallel regens make
            #      the latest _stage line a completion message.
            #   4. fresh_stage_in_flight: any recent _stage message
            #      (<60s) ends with "…". Catches agents that don't
            #      register active-section markers — most importantly
            #      the Outline Agent, which runs while status=drafting
            #      (busy=False). The freshness window guards against
            #      stale "…" lines left over from a crashed pipeline.
            from app.services.cancellation import (
                JOB_AUTO_REVIEW,
                get_active_sections,
            )
            from app.services.cancellation import (
                is_running as _job_is_running,
            )

            stages_recent = [r for r in recent_runs if r.agent_name == "_stage"]
            loop_running = _job_is_running(JOB_AUTO_REVIEW, proposal_id)
            active_sections = get_active_sections(proposal_id)

            # Iterate ALL recent stages, not just the latest — when
            # multiple sections regen in parallel, one finishing
            # (completion message, no "…") shouldn't hide the in-flight
            # banner for the others still running.
            fresh_stage_in_flight = False
            for stage in stages_recent:
                if _agent_run_status_value(stage) == "failed":
                    continue
                msg = (stage.error_text or "").rstrip()
                if not msg.endswith("…"):
                    continue
                when = stage.completed_at or stage.started_at or stage.created_at
                if when is None:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - when).total_seconds()
                if age <= 60:
                    fresh_stage_in_flight = True
                    break

            is_busy = (
                bool(status_busy)
                or bool(loop_running)
                or bool(active_sections)
                or bool(active_review_docs)
                or fresh_stage_in_flight
            )
            busy_stage_obj = None
            in_flight_stage_obj = None

            if is_busy:
                # Prefer an in-flight ("…") stage so the banner reflects
                # what's actually running, not what most recently
                # completed. When active_sections is non-empty but every
                # recent stage is a completion message (parallel regens
                # racing), the in-flight start-stage is what matters.
                for stage in stages_recent:
                    if _agent_run_status_value(stage) == "failed":
                        continue
                    msg = (stage.error_text or "").rstrip()
                    if msg.endswith("…"):
                        in_flight_stage_obj = stage
                        break
                busy_stage_obj = in_flight_stage_obj
                if busy_stage_obj is None and stages_recent:
                    busy_stage_obj = stages_recent[0]

            # Synthesize a message when active_sections is the busy
            # signal but no in-flight stage is visible in the last 20
            # rows (start-stage aged out, or completion-stage races).
            n_active = len(active_sections)
            if active_review_docs and in_flight_stage_obj is None:
                active_row = active_review_docs[0]
                active_review = active_row["review"]
                active_status = active_review.get("status")
                if active_status == "extracting":
                    model = (
                        (active_review.get("extraction") or {}).get("model")
                        or "configured extraction model"
                    )
                    latest_msg = (
                        f"{active_row['filename']}: extracting requirements "
                        f"with {model}…"
                    )
                else:
                    model = (
                        (active_review.get("classification") or {}).get(
                            "primary_model"
                        )
                        or "configured review model"
                    )
                    latest_msg = (
                        f"{active_row['filename']}: reviewing requirements "
                        f"with {model}…"
                    )
            elif active_sections and in_flight_stage_obj is None:
                latest_msg = (
                    f"Regenerating {n_active} section"
                    f"{'s' if n_active != 1 else ''}…"
                )
            elif busy_stage_obj is not None:
                latest_msg = busy_stage_obj.error_text or "Auto review-revise loop running…"
            else:
                latest_msg = "Auto review-revise loop running…"

            busy_banner.set_visibility(is_busy)
            if is_busy:
                busy_msg.set_text(latest_msg)
                # Elapsed time anchors to the most recent in-flight
                # stage start, since that's the work the message
                # describes. When no such stage exists, leave the
                # elapsed counter alone (poll cycle will refresh it
                # when a stage comes through).
                if in_flight_stage_obj is not None:
                    stage_start = (
                        in_flight_stage_obj.completed_at
                        or in_flight_stage_obj.started_at
                        or in_flight_stage_obj.created_at
                    )
                    if stage_start is not None:
                        if stage_start.tzinfo is None:
                            stage_start = stage_start.replace(tzinfo=UTC)
                        sec = int((datetime.now(UTC) - stage_start).total_seconds())
                        mm, ss = divmod(max(sec, 0), 60)
                        busy_elapsed.set_text(f"elapsed {mm}:{ss:02d}")

            # Stage log — newest first
            stage_log.clear()
            with stage_log:
                if not recent_runs:
                    ui.label("Waiting for the pipeline to start…").classes("text-sm opacity-60")
                for idx, run in enumerate(recent_runs):
                    is_stage = run.agent_name == "_stage"
                    if is_stage:
                        label_text = run.error_text
                    else:
                        agent_label = (run.agent_name or "agent").replace("_", " ")
                        label_text = agent_label
                        if run.model_used:
                            label_text += (
                                f" · {_model_provider_label(run.model_used)} · "
                                f"{run.model_used}"
                            )
                        run_status = _agent_run_status_value(run)
                        if run_status == "failed":
                            label_text += " — failed"
                        elif run_status == "cancelled":
                            label_text += " — cancelled"
                    when = run.completed_at or run.started_at or run.created_at
                    timestamp = when.strftime("%H:%M:%S") if when else "—"

                    with ui.row().classes("items-center gap-3 w-full py-0.5"):
                        icon_name, icon_color = _progress_run_visual(run, idx)
                        ui.icon(icon_name).classes(icon_color)
                        ui.label(timestamp).classes("text-xs opacity-60 w-20 font-mono")
                        ui.label(label_text or "(no message)").classes("flex-1 text-sm")
                        if run.cost_usd and float(run.cost_usd) > 0:
                            ui.label(f"est. ${float(run.cost_usd):.4f}").classes(
                                "text-xs opacity-60 font-mono"
                            )

            matrix_count_label.set_text(
                f"{matrix_count} package item{'s' if matrix_count != 1 else ''}"
            )

            review_link.clear()
            with review_link:
                if matrix_count > 0:
                    ui.button(
                        "Open Proposal Review",
                        icon="open_in_new",
                        on_click=lambda: ui.navigate.to(f"/proposals/{proposal_id}"),
                    ).props("color=primary flat")

        # Initial render + 2s polling. A regular ui.timer belongs to the
        # current parent slot and can race with page teardown: its loop may try
        # to enter a slot just after NiceGUI deletes that slot. Keep this timer
        # outside the element tree and stop it as soon as its page disappears.
        refresh()
        poll_timer: BackgroundTimer | None = None

        def poll_refresh() -> None:
            if status_row.is_deleted:
                if poll_timer is not None:
                    poll_timer.cancel(with_current_invocation=True)
                return
            refresh()

        poll_timer = BackgroundTimer(2.0, poll_refresh, immediate=False)
        ui.context.client.on_delete(
            lambda: poll_timer.cancel(with_current_invocation=True)
        )


# ----- Proposal Review -----------------------------------------------------------


def _tab_with_badge(name: str, icon: str, count: int):
    """Render a tab button with a small red action-count badge floating
    bottom-left of the icon. Returns the badge widget so callers can
    update count via `_update_badge` for live mid-tab refreshes — the
    widget is always mounted (hidden when count == 0) so its identity
    survives refresh cycles.

    Display cap is "99+" — anything more isn't useful at a glance.
    """
    with ui.tab(name, icon=icon):
        label = _badge_label(count)
        # Quasar QBadge with `floating` defaults to top-right; override
        # to bottom-left so it doesn't clash with the active-tab
        # underline. Positive `bottom` keeps the digits inside the
        # tab's clipping box (negative offsets get cropped on some
        # browsers). min-width matches min-height so single-digit
        # counts render as a clean circle, multi-digit as a pill.
        badge = (
            ui.badge(label, color="red")
            .props("floating rounded")
            .style(
                "top: auto; right: auto; bottom: 2px; left: 2px; "
                "font-size: 10px; line-height: 14px; "
                "min-width: 18px; min-height: 18px; "
                "padding: 1px 5px; font-weight: 600; "
                "box-shadow: 0 1px 2px rgba(0,0,0,0.15);"
            )
        )
        if count <= 0:
            badge.set_visibility(False)
        return badge


def _badge_label(count: int) -> str:
    """Render-string for a tab badge. Empty when zero (paired with
    set_visibility=False to hide the widget); '99+' when capped."""
    if count <= 0:
        return ""
    return "99+" if count > 99 else str(count)


def _update_badge(badge, count: int) -> None:
    """Update an existing tab badge's text + visibility in place. Safe
    to call with a None badge (no-op) so callers don't need to guard."""
    if badge is None:
        return
    badge.text = _badge_label(count)
    badge.set_visibility(count > 0)


# Proposal Review tab declarations. Order = display order. Each entry
# is (label, icon, badge_key). A non-None badge_key means the tab gets
# a live-updating red action-count badge — the SAME key MUST appear in
# the dict `_compute_tab_badges` returns. None means no count is shown
# (the tab is informational / agent-work, not human-action).
#
# Single source of truth: adding a new badged tab is now (1) add one
# entry here and (2) add the key + count query to `_compute_tab_badges`.
_PROPOSAL_REVIEW_TABS: list[tuple[str, str, str | None]] = [
    ("Compliance", "rule", None),
    ("Evaluation Criteria", "scoreboard", None),
    ("Amendments & Q&A", "fact_check", None),
    ("Gaps", "warning", "gaps"),
    ("Outline", "rule_folder", None),
    ("Team", "groups", None),
    ("Cost", "payments", None),
    ("Cost Review", "fact_check", "cost_review"),
    ("Draft", "article", "draft"),
    ("Reviewer Findings", "rate_review", "findings"),
    ("Final Polish", "auto_awesome", None),
    ("Completed Draft", "task_alt", None),
    ("Submission Checklist", "checklist", "submission"),
    ("Timeline", "schedule", None),
    ("Spend", "savings", None),
]


def _compute_tab_badges(proposal_id: int) -> dict[str, int]:
    """Return action-item counts for every tab that has an action queue.
    Tabs without a meaningful "needs attention" semantic (Compliance,
    Pricing/Cost/Audit placeholders) get 0 by omission.

    Cheap queries — runs once per page render.
    """
    # Only tabs where the human is ACTIVELY needed appear here. Outline
    # is an agent-work indicator (Outline Agent assigns items); a count
    # there isn't a "you must act" signal, so it's intentionally absent.
    # Draft IS badged, but only counts unresolved [NEEDS_HUMAN]
    # placeholders — the human-action items embedded in the prose.
    # Section-drafting progress is communicated via the header card
    # ("X of Y section(s) drafted"), not the badge.
    badges: dict[str, int] = {
        "gaps": 0,
        "submission": 0,
        "findings": 0,
        "cost_review": 0,
        "draft": 0,
    }
    with SessionLocal() as db:
        # Gaps — count those that need user attention. A gap is "addressed"
        # if the user has either explicitly marked it resolved OR picked
        # a mitigation (selecting commits to a path even if "Mark resolved"
        # wasn't separately clicked). The reverse: a gap needs attention
        # only when neither has happened.
        badges["gaps"] = (
            db.query(GapAnalysis)
            .join(
                ComplianceMatrixItem,
                ComplianceMatrixItem.id == GapAnalysis.requirement_id_fk,
            )
            .filter(
                GapAnalysis.proposal_id == proposal_id,
                GapAnalysis.resolved == False,  # noqa: E712
                GapAnalysis.selected_mitigation_index.is_(None),
                ComplianceMatrixItem.status == "active",
            )
            .count()
        )

        # Submission Checklist — sum of:
        #   1. mandatory_form / certification matrix items not yet obtained
        #   2. drafting commitments not yet obtained (user-tracked
        #      deliverables added from the Provide-value dialog)
        from app.models import SubmissionCommitment

        n_matrix = (
            db.query(ComplianceMatrixItem)
            .filter(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
                ComplianceMatrixItem.submission_obtained == False,  # noqa: E712,
                (
                    (ComplianceMatrixItem.requirement_type == "mandatory_form")
                    | (ComplianceMatrixItem.category == "certification")
                ),
            )
            .count()
        )
        n_commits = (
            db.query(SubmissionCommitment)
            .filter(
                SubmissionCommitment.proposal_id == proposal_id,
                SubmissionCommitment.obtained == False,  # noqa: E712
            )
            .count()
        )
        badges["submission"] = n_matrix + n_commits

        # Pull section rows once for the Needs Human + Reviewer Findings counts.
        sec_rows = (
            db.execute(select(ProposalSection).where(ProposalSection.proposal_id == proposal_id))
            .scalars()
            .all()
        )
        section_pks = [s.id for s in sec_rows]

        # Draft — unresolved [NEEDS_HUMAN] placeholders across all
        # sections. These are the items the user must act on inline in
        # the draft prose. We count what's currently in the JSON; the
        # Draft tab's render() runs reconcile_placeholders before
        # display, which may shift the count slightly — refresh_outer_
        # chrome re-runs this query after any in-tab resolve action.
        n_draft_action = 0
        for s in sec_rows:
            for ph in s.needs_human_placeholders_json or []:
                if not ph.get("resolved"):
                    n_draft_action += 1
        badges["draft"] = n_draft_action

        # Reviewer Findings — every unresolved action. Pending findings
        # still need a triage decision; accepted findings still need to be
        # applied to the section and verified by a follow-up review. Dismissed
        # and resolved findings are audit history, not actions.
        if section_pks:
            badges["findings"] = (
                db.query(ReviewerFinding)
                .filter(
                    ReviewerFinding.proposal_section_id.in_(section_pks),
                    ReviewerFinding.dismissed_at.is_(None),
                    ReviewerFinding.resolved_in_pass_number.is_(None),
                )
                .count()
            )

        # Cost Review — pending findings (CostReviewFinding rows where
        # the user hasn't yet accepted or rejected). Counts logical
        # findings, not per-scenario rows: same finding text means
        # same logical finding, so we DISTINCT on finding_text. The
        # user_action persists at the row level but is identical
        # across rows of the same logical finding, so any row's
        # action represents the whole.
        from app.models import CostReviewFinding, PricingPackage

        pending_pkg_query = (
            db.query(CostReviewFinding.finding_text)
            .join(
                PricingPackage,
                CostReviewFinding.pricing_package_id == PricingPackage.id,
            )
            .filter(
                PricingPackage.proposal_id == proposal_id,
                CostReviewFinding.user_action == "pending",
            )
            .distinct()
        )
        badges["cost_review"] = pending_pkg_query.count()

    return badges


# `_REQ_TYPE_COLOR` moved to app/ui/tabs/compliance.py with the
# Compliance tab renderer (its only consumer).


@ui.page("/proposals/{proposal_id}")
def proposal_review(proposal_id: int) -> None:
    with page_frame(f"Proposal Review · #{proposal_id}"):
        with SessionLocal() as db:
            proposal = db.get(Proposal, proposal_id)
            if proposal is None:
                _empty_state(f"Proposal #{proposal_id} not found.", icon="error")
                return
            title = proposal.title
            agency = proposal.agency
            status_val = proposal.status.value if hasattr(proposal.status, "value") else str(proposal.status)
            evaluation_criteria_json = proposal.evaluation_criteria_json
            items = (
                db.execute(
                    select(ComplianceMatrixItem)
                    .where(ComplianceMatrixItem.proposal_id == proposal_id)
                    .order_by(
                        ComplianceMatrixItem.source_doc,
                        ComplianceMatrixItem.source_page.nulls_last(),
                        ComplianceMatrixItem.id,
                    )
                )
                .scalars()
                .all()
            )
            # Snapshot rows out of the session.
            matrix_rows = [
                {
                    "id": i.id,
                    "requirement_id": i.requirement_id,
                    "requirement_text": i.requirement_text,
                    "source_doc": i.source_doc,
                    "source_document_id": i.source_document_id,
                    "source_section": i.source_section or "",
                    "source_page": i.source_page,
                    "type": i.requirement_type.value
                    if hasattr(i.requirement_type, "value")
                    else str(i.requirement_type),
                    "category": i.category.value if hasattr(i.category, "value") else str(i.category),
                    "weight": float(i.weight) if i.weight is not None else None,
                    "amendment_origin": i.amendment_origin,
                    "status": i.status,
                }
                for i in items
            ]
            requirements_reviews = []
            if proposal.rfp_package is not None:
                for document in sorted(
                    proposal.rfp_package.documents, key=lambda value: value.id
                ):
                    review = _requirements_review_from_structure(
                        document.structure_json
                    )
                    if review:
                        requirements_reviews.append(
                            {
                                "id": document.id,
                                "filename": document.filename,
                                "review": review,
                            }
                        )

        # Snapshot gaps + deal-breaker count for the header banner / Gaps tab.
        with SessionLocal() as db:
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
                .order_by(GapAnalysis.gap_severity, GapAnalysis.id)
            ).all()
            gaps_snapshot = [
                {
                    "id": g.id,
                    "gap_id": g.gap_id,
                    "severity": g.gap_severity.value
                    if hasattr(g.gap_severity, "value")
                    else str(g.gap_severity),
                    "current_state": g.current_state or "",
                    "mitigation_options": g.mitigation_options_json or [],
                    "recommended_index": g.recommended_mitigation_index,
                    "selected_index": g.selected_mitigation_index,
                    "selected_partner": g.selected_partner_name,
                    "resolved": bool(g.resolved),
                    "resolution_notes": g.resolution_notes or "",
                    "req_id": req.requirement_id,
                    "req_text": req.requirement_text,
                    "req_type": req.requirement_type.value
                    if hasattr(req.requirement_type, "value")
                    else str(req.requirement_type),
                    "req_source": (
                        f"{req.source_doc}"
                        + (f" §{req.source_section}" if req.source_section else "")
                        + (f" p.{req.source_page}" if req.source_page else "")
                    ),
                }
                for g, req in gap_rows
            ]
        deal_breakers = [g for g in gaps_snapshot if g["severity"] == "deal_breaker"]

        # Snapshot count of sections with amendment-driven compliance
        # drift so the header can render the "N sections need re-drafting"
        # banner. Cheap query — one COUNT against an indexed proposal_id.
        with SessionLocal() as db:
            n_stale_sections = (
                db.query(ProposalSection)
                .filter(
                    ProposalSection.proposal_id == proposal_id,
                    ProposalSection.compliance_drift_pending == True,  # noqa: E712
                )
                .count()
            )

        # Header
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.column().classes("gap-0"):
                    ui.label(title).classes("text-xl font-semibold")
                    ui.label(f"{agency or '—'} · status: {_status_label(status_val)}").classes(
                        "text-sm opacity-70"
                    )
                with ui.row().classes("gap-2"):

                    def on_run_shortfall(pid=proposal_id) -> None:
                        action = "Re-running" if gaps_snapshot else "Running"
                        spawn_shortfall(pid)
                        ui.notify(
                            f"{action} shortfall analysis. Watch Run Progress for live status.",
                            type="positive",
                        )
                        ui.navigate.to(f"/proposals/{pid}/progress")

                    # Header buttons only matter during the gaps phase —
                    # re-running shortfall after scope sign-off would
                    # invalidate downstream selections, and the next-step
                    # banner exposes Run Progress directly when an agent
                    # is actually running. Hide both once we're past gaps.
                    _PRE_OUTLINE_STATUSES = (
                        "intaking",
                        "awaiting_scope_signoff",
                    )
                    if status_val in _PRE_OUTLINE_STATUSES:
                        if matrix_rows:
                            btn_label = (
                                "Re-run shortfall analysis" if gaps_snapshot else "Run shortfall analysis"
                            )
                            ui.button(btn_label, icon="warning", on_click=on_run_shortfall).props(
                                "flat color=primary"
                            )
                        ui.button(
                            "Run progress",
                            icon="autorenew",
                            on_click=lambda: ui.navigate.to(f"/proposals/{proposal_id}/progress"),
                        ).props("flat")

        # Tab-switch callback so the next-step banner can flip tabs
        # in-place instead of using `#cost`-style URL hash navigation,
        # which is a no-op when the user is already on this page. The
        # `tabs` widget is created further down; the callback closes
        # over a mutable handle that's populated post-creation.
        _tab_handle: dict = {"widget": None}

        def _switch_tab(name: str) -> None:
            w = _tab_handle["widget"]
            if w is not None:
                w.set_value(name)
            else:
                # Fallback: pre-tabs (shouldn't happen during normal use,
                # but defensive in case of race on initial render).
                ui.navigate.to(f"/proposals/{proposal_id}")

        # Status-aware next-step banner — always visible, content varies by status.
        # Refreshable: re-queries gap counts AND the current proposal status
        # on every refresh so the banner advances when in-tab actions
        # (e.g. Approve Team → AWAITING_COST_BUILD) flip the status mid-page.
        _render_next_step_banner(proposal_id, switch_tab=_switch_tab)

        # Deal-breaker banner — only when shortfall has flagged at least one
        if deal_breakers:
            with ui.card().classes("w-full bg-red-50 border-l-4 border-red-700"):
                with ui.row().classes("items-center gap-3 w-full"):
                    ui.icon("error").classes("text-red-700 text-3xl")
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(
                            f"⚠ {len(deal_breakers)} deal-breaker gap"
                            f"{'s' if len(deal_breakers) != 1 else ''} detected"
                        ).classes("text-base font-semibold text-red-900")
                        ui.label(
                            "Shortfall Strategist found requirements with no honest "
                            "mitigation. Review the Gaps tab — no-bid may be the right call."
                        ).classes("text-sm text-red-800")

        # Amendment drift banner — non-zero when any section's
        # compliance_items_addressed overlaps a requirement an amendment
        # modified or removed and the writer hasn't re-drafted it yet.
        if n_stale_sections > 0:
            with ui.card().classes("w-full bg-amber-50 border-l-4 border-amber-700"):
                with ui.row().classes("items-center gap-3 w-full"):
                    ui.icon("update").classes("text-amber-800 text-3xl")
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(
                            f"{n_stale_sections} section"
                            f"{'s' if n_stale_sections != 1 else ''} "
                            f"need re-drafting after recent amendment activity."
                        ).classes("text-base font-semibold text-amber-900")
                        ui.label(
                            "Open the Draft tab — sections flagged 'Stale' "
                            "need a regenerate to reflect the latest "
                            "compliance matrix."
                        ).classes("text-sm text-amber-800")

        # Compute action-item counts for each tab so we can render a small
        # red badge over the icon. Badge widgets are captured so we can
        # update them in place when in-tab actions change the underlying
        # state (see refresh_outer_chrome below).
        badges = _compute_tab_badges(proposal_id)
        badge_widgets: dict[str, object] = {}

        with ui.tabs().classes("w-full") as tabs:
            for label, icon, badge_key in _PROPOSAL_REVIEW_TABS:
                if badge_key is None:
                    _tab_with_badge(label, icon, 0)
                else:
                    badge_widgets[badge_key] = _tab_with_badge(
                        label,
                        icon,
                        badges[badge_key],
                    )

        # Now that the tabs widget exists, wire it up to the switch_tab
        # callback the banner already captured.
        _tab_handle["widget"] = tabs

        _submission_checklist_refresh: dict = {"callback": None}

        def refresh_outer_chrome(*, refresh_submission: bool = True) -> None:
            """Refresh shared proposal state after an in-tab mutation.

            Recomputes tab badge counts and the next-step banner, then
            refreshes the Submission Checklist's deterministic readiness
            snapshot so cross-tab changes are visible without a page reload.
            Called from in-tab handlers after they mutate state, so the
            page chrome stays in sync without a full page reload."""
            new_counts = _compute_tab_badges(proposal_id)
            for key, widget in badge_widgets.items():
                _update_badge(widget, new_counts.get(key, 0))
            _render_next_step_banner.refresh()
            callback = _submission_checklist_refresh["callback"]
            if refresh_submission and callback is not None:
                callback()

        def refresh_chrome_from_submission() -> None:
            """Refresh shared chrome after the checklist refreshed itself."""
            refresh_outer_chrome(refresh_submission=False)

        # Outcome panel — only for terminal statuses where an outcome
        # actually makes sense (submitted / approved / archived).
        if status_val in ("submitted", "approved", "archived"):
            from app.ui.tabs.outcome import _render_outcome_panel

            _render_outcome_panel(
                proposal_id,
                on_state_change=refresh_outer_chrome,
                read_only=status_val == ProposalStatus.ARCHIVED.value,
            )

        # Default tab logic — give the user the most relevant landing tab
        # for the current phase.
        if deal_breakers:
            default_tab = "Gaps"
        elif status_val == "awaiting_outline_approval":
            default_tab = "Outline"
        elif status_val == "awaiting_team_approval":
            default_tab = "Team"
        elif status_val in ("awaiting_cost_build", "awaiting_draft"):
            default_tab = "Cost"
        elif status_val in ("draft_in_progress", "draft_ready"):
            default_tab = "Draft"
        else:
            default_tab = "Compliance"

        with ui.tab_panels(tabs, value=default_tab).classes("w-full") as tab_panels:
            with ui.tab_panel("Compliance"):
                _render_compliance_tab(matrix_rows, requirements_reviews)
            with ui.tab_panel("Evaluation Criteria"):

                def _rerun_section_m(pid=proposal_id):
                    spawn_section_m_only(pid)
                    ui.notify(
                        "Re-extracting evaluation criteria — refresh in a moment.",
                        type="positive",
                    )

                _render_evaluation_criteria_tab(
                    evaluation_criteria_json,
                    on_rerun=_rerun_section_m,
                )
            with ui.tab_panel("Amendments & Q&A"):
                _render_amendments_tab(
                    proposal_id,
                    on_state_change=refresh_outer_chrome,
                )
            with ui.tab_panel("Gaps"):
                _render_gaps_tab(
                    proposal_id,
                    gaps_snapshot,
                    has_matrix=bool(matrix_rows),
                    on_state_change=refresh_outer_chrome,
                )
            with ui.tab_panel("Outline"):
                _render_outline_tab(
                    proposal_id,
                    status_val,
                    on_state_change=refresh_outer_chrome,
                )
            with ui.tab_panel("Team"):
                _render_team_tab(
                    proposal_id,
                    on_state_change=refresh_outer_chrome,
                )
            with ui.tab_panel("Cost"):
                _render_cost_tab(proposal_id)
            with ui.tab_panel("Cost Review"):
                _render_cost_review_tab(
                    proposal_id,
                    on_state_change=refresh_outer_chrome,
                    switch_tab=_switch_tab,
                )
            with ui.tab_panel("Draft"):
                _render_draft_tab(
                    proposal_id,
                    status_val,
                    on_state_change=refresh_outer_chrome,
                )
            with ui.tab_panel("Reviewer Findings"):
                _render_findings_tab(
                    proposal_id,
                    status_val,
                    on_state_change=refresh_outer_chrome,
                )
            with ui.tab_panel("Final Polish"):
                _render_final_polish_tab(proposal_id)
            with ui.tab_panel("Completed Draft"):
                _render_completed_draft_tab(proposal_id)
            with ui.tab_panel("Submission Checklist"):
                _submission_checklist_refresh["callback"] = (
                    _render_submission_checklist_tab(
                        proposal_id,
                        on_state_change=refresh_chrome_from_submission,
                    )
                )
            with ui.tab_panel("Timeline"):
                _render_timeline_tab(
                    proposal_id,
                    on_state_change=refresh_outer_chrome,
                )
            with ui.tab_panel("Spend"):
                _render_spend_tab(proposal_id)

        # Archived proposals are an audit record. Keep every tab readable and
        # navigable, but disable all controls inside the panels so the
        # "Archived (read-only)" contract is real rather than cosmetic. The
        # top-level tab selectors live outside ``tab_panels``. Nested tabs and
        # expansion panels are also view navigation, not mutations, so leave
        # those enabled or archived detail would become inaccessible.
        if status_val == ProposalStatus.ARCHIVED.value:
            from nicegui.elements.expansion import Expansion
            from nicegui.elements.mixins.disableable_element import DisableableElement
            from nicegui.elements.tabs import Tab, TabPanel

            for element in tab_panels.descendants():
                if isinstance(element, (Tab, TabPanel, Expansion)):
                    continue
                if isinstance(element, DisableableElement):
                    element.disable()


# `_SEVERITY_VISUAL` and `_SEVERITY_ORDER` moved to
# app/ui/tabs/gaps.py (their only consumer).


@ui.refreshable
def _render_next_step_banner(
    proposal_id: int,
    *,
    switch_tab=None,
) -> None:
    """Status-aware 'what's next' banner pinned to the top of Proposal Review.

    Color scheme:
      amber = action needed from the user
      blue  = work in progress (agent or future agent)
      green = done / approved / submitted
      slate = inactive / archived

    Each variant shows a title, one-line description, and (where applicable)
    an action button. Variants for statuses whose agent isn't built yet
    explicitly say so — the user shouldn't think they did something wrong
    when there's nothing happening.

    Refreshable so the unresolved-gap count AND the current status stay
    in sync with mid-tab mutations (mitigation picks / mark-resolved
    clicks / Approve Team / Begin Drafting). Both are queried fresh on
    every refresh so the banner moves to the next step the moment the
    underlying action lands — no full page reload required.
    """
    with SessionLocal() as db:
        prop = db.get(Proposal, proposal_id)
        status_val = (
            prop.status.value
            if prop and hasattr(prop.status, "value")
            else (str(prop.status) if prop else "")
        )
        n_total = (
            db.query(GapAnalysis)
            .join(
                ComplianceMatrixItem,
                ComplianceMatrixItem.id == GapAnalysis.requirement_id_fk,
            )
            .filter(
                GapAnalysis.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
            .count()
        )
        n_unresolved = (
            db.query(GapAnalysis)
            .join(
                ComplianceMatrixItem,
                ComplianceMatrixItem.id == GapAnalysis.requirement_id_fk,
            )
            .filter(
                GapAnalysis.proposal_id == proposal_id,
                GapAnalysis.resolved == False,  # noqa: E712
                GapAnalysis.selected_mitigation_index.is_(None),
                ComplianceMatrixItem.status == "active",
            )
            .count()
        )

    # Each spec: (icon, accent, bg, border, title, body, action_button | None)
    # action_button: dict {label, icon, on_click} or None.
    spec: dict[str, dict] = {}

    spec["intaking"] = {
        "icon": "autorenew",
        "accent": "blue-700",
        "bg": "bg-blue-50",
        "border": "border-blue-500",
        "title": "Pipeline running",
        "body": "Compliance Matrix and Shortfall Strategist are working on this proposal.",
        "btn": {
            "label": "Watch Run Progress",
            "icon": "monitor_heart",
            "on_click": lambda: ui.navigate.to(f"/proposals/{proposal_id}/progress"),
        },
    }
    spec["awaiting_scope_signoff"] = {
        "icon": "how_to_reg",
        "accent": "amber-700",
        "bg": "bg-amber-50",
        "border": "border-amber-500",
        "title": "Action needed: review gaps and sign off scope",
        "body": (
            f"{n_unresolved} unresolved gap{'s' if n_unresolved != 1 else ''} of {n_total}. "
            "Open the Gaps tab, choose a mitigation for each, mark resolved as you go, "
            "then sign off scope. (Outline Agent runs next — proposes section structure for your review.)"
        ),
        "btn": {
            "label": "Sign off scope",
            "icon": "task_alt",
            "on_click": lambda: _sign_off_scope(proposal_id),
        },
    }
    spec["drafting"] = {
        "icon": "edit_note",
        "accent": "blue-700",
        "bg": "bg-blue-50",
        "border": "border-blue-500",
        "title": "Scope signed off — generate the section outline",
        "body": (
            "Click 'Generate Draft Outline' to run the Outline Agent. It reads the "
            "compliance matrix, your gap mitigation choices, and the RFP itself, and "
            "proposes a section structure (titles, order, what each section addresses). "
            "You'll review and approve the outline before the Writer Team drafts each "
            "section."
        ),
        "btn": {
            "label": "Generate Draft Outline",
            "icon": "rule_folder",
            "on_click": lambda: _generate_outline(proposal_id),
        },
    }
    spec["awaiting_outline_approval"] = {
        "icon": "rule_folder",
        "accent": "amber-700",
        "bg": "bg-amber-50",
        "border": "border-amber-500",
        "title": "Action needed: review the outline and approve",
        "body": (
            "The Outline Agent has proposed a section structure. Open the Outline tab "
            "to review section titles, briefs, and which compliance items each section "
            "addresses. Approving the outline moves you to the Team tab next — you'll "
            "name the personnel and time allocations BEFORE the cost build and the "
            "draft, so the writer has real names + numbers from the start."
        ),
        "btn": {
            "label": "Approve outline",
            "icon": "rule_folder",
            "on_click": lambda: _approve_outline(proposal_id),
        },
    }

    def _go_to_tab(name: str):
        # Prefer the in-page tab switcher (no reload); fall back to a
        # full navigate if no switcher was wired (defensive — should
        # always be present from the proposal_review page).
        if switch_tab is not None:
            return lambda: switch_tab(name)
        return lambda: ui.navigate.to(f"/proposals/{proposal_id}")

    spec["awaiting_team_approval"] = {
        "icon": "groups",
        "accent": "amber-700",
        "bg": "bg-amber-50",
        "border": "border-amber-500",
        "title": "Action needed: build and approve the team",
        "body": (
            "Outline is approved. Open the Team tab next: click 'Propose Team (AI)' "
            "to seed roles + labor categories + time allocations from the RFP scope, "
            "then assign specific people to each role and click Approve Team. The "
            "approved roster drives both the Cost Analyst (which uses your salaries "
            "+ hours verbatim) and the Writer Team (which references named personnel "
            "in prose instead of emitting [NEEDS_HUMAN] placeholders)."
        ),
        "btn": {
            "label": "Open Team tab",
            "icon": "groups",
            "on_click": _go_to_tab("Team"),
        },
    }
    spec["awaiting_cost_build"] = {
        "icon": "payments",
        "accent": "amber-700",
        "bg": "bg-amber-50",
        "border": "border-amber-500",
        "title": "Action needed: run the Cost Analyst",
        "body": (
            "Team is approved. Open the Cost tab next and click 'Run Cost Analyst' "
            "(or 'Run Market Research' first if you haven't already). The Cost "
            "Analyst will use your approved roster's labor categories, salaries, "
            "and hours verbatim, and decide ODCs / phases / risks / executive "
            "summary on top. Drafting unlocks once the cost build is in place."
        ),
        "btn": {
            "label": "Open Cost tab",
            "icon": "payments",
            "on_click": _go_to_tab("Cost"),
        },
    }
    # Service-line override: payment_systems doesn't run Cost Analyst.
    # Banner points at Payment Market Research → Cost Volume Writer
    # depending on which has run already. Status advance from awaiting_
    # cost_build → awaiting_draft happens automatically once the Cost
    # Volume Writer drafts at least one cost-deferred section (see
    # jobs/cost_writer.py); if the user lands here after both have
    # run, that flip is the recovery — refresh to see the awaiting_
    # draft banner. This conditional only fires while status is
    # genuinely stuck pre-flip.
    if get_service_line(proposal_id) == SERVICE_LINE_PAYMENT_SYSTEMS:
        with SessionLocal() as _db:
            _p = _db.get(Proposal, proposal_id)
            _has_payment_scan = bool(_p and (_p.payment_market_scan_json or "").strip())
        if not _has_payment_scan:
            spec["awaiting_cost_build"] = {
                "icon": "payments",
                "accent": "amber-700",
                "bg": "bg-amber-50",
                "border": "border-amber-500",
                "title": ("Action needed: run Payment Market Research"),
                "body": (
                    "Team is approved. Open the Cost tab and click "
                    "'Run Payment Market Research' — Gemini + Claude "
                    "will research comparable processor rate "
                    "disclosures, estimate annual processed volume, "
                    "and recommend a competitive bid posture. The "
                    "Cost Volume Writer drafts the fee narrative "
                    "from that recommendation. The labor-based Cost "
                    "Analyst is skipped; the payment-specific Cost Reviewer "
                    "then fact-checks the narrative against the scan."
                ),
                "btn": {
                    "label": "Open Cost tab",
                    "icon": "payments",
                    "on_click": _go_to_tab("Cost"),
                },
            }
        else:
            spec["awaiting_cost_build"] = {
                "icon": "article",
                "accent": "amber-700",
                "bg": "bg-amber-50",
                "border": "border-amber-500",
                "title": ("Payment market scan complete — draft the fee narrative"),
                "body": (
                    "The dual-pipeline scan persisted recommended "
                    "rates, comparable awards, and a profit "
                    "projection. Click 'Run Cost Volume Writer' on "
                    "the Cost tab to draft the fee section (SEC-005 "
                    "or equivalent) directly from the recommendation. "
                    "Status advances to 'Awaiting draft' as soon as "
                    "the writer drafts cleanly."
                ),
                "btn": {
                    "label": "Open Cost tab",
                    "icon": "payments",
                    "on_click": _go_to_tab("Cost"),
                },
            }
    # Two-stage flow at AWAITING_DRAFT: the user reviews the cost-
    # narrative draft (Cost Volume Writer) BEFORE the full Writer Team
    # fans out. We branch the spec here so the banner reflects whether
    # the cost section has been drafted yet — surfacing the right
    # next-step button without forcing a status enum split.
    cost_sections_drafted = False
    try:
        cost_sections = _load_cost_deferred_sections(proposal_id)
        cost_sections_drafted = bool(cost_sections) and all(s.get("has_draft") for s in cost_sections)
    except Exception:
        log.exception(
            "next-step banner: failed to read cost-deferred sections; defaulting to Begin Drafting prompt.",
        )

    def _kick_off_cost_writer_from_banner() -> None:
        spawn_cost_writer(proposal_id)
        ui.notify(
            "Cost Volume Writer started — drafting the cost-narrative section. Watch Run Progress.",
            type="positive",
            multi_line=True,
            timeout=5000,
        )
        ui.navigate.to(f"/proposals/{proposal_id}/progress")

    if not cost_sections_drafted:
        spec["awaiting_draft"] = {
            "icon": "article",
            "accent": "amber-700",
            "bg": "bg-amber-50",
            "border": "border-amber-500",
            "title": "Cost build is in place — draft the cost narrative next",
            "body": (
                "Team and cost are both approved. Run the Cost Volume "
                "Writer next: it drafts the cost-narrative section "
                "(SEC-009 / cost proposal) using the actual labor "
                "lines, ODCs, and proposed price as anchors. Review "
                "that draft before the full Writer Team fans out — "
                "easier to fix the pricing story once than across "
                "every section."
            ),
            "btn": {
                "label": "Run Cost Volume Writer",
                "icon": "article",
                "on_click": _kick_off_cost_writer_from_banner,
            },
        }
    else:
        spec["awaiting_draft"] = {
            "icon": "auto_stories",
            "accent": "amber-700",
            "bg": "bg-amber-50",
            "border": "border-amber-500",
            "title": "Ready to draft — kick off the Writer Team",
            "body": (
                "Cost narrative is drafted. The Writer Team's cached "
                "prefix contains the approved roster + cost build + "
                "the cost narrative the writer can reference, so the "
                "first draft will name real personnel, quote real "
                "allocations, and reference the actual proposed "
                "price — no [NEEDS_HUMAN] markers for things already "
                "decided. Click below to start."
            ),
            "btn": {
                "label": "Begin Drafting",
                "icon": "auto_stories",
                "on_click": lambda: _begin_drafting(proposal_id),
            },
        }
    spec["draft_in_progress"] = {
        "icon": "auto_stories",
        "accent": "blue-700",
        "bg": "bg-blue-50",
        "border": "border-blue-500",
        "title": "Writer Team drafting sections",
        "body": (
            "Per-section drafts are being written. Watch live progress on Run Progress, "
            "or check the Draft tab — completed sections render as they land."
        ),
        "btn": {
            "label": "Watch Run Progress",
            "icon": "monitor_heart",
            "on_click": lambda: ui.navigate.to(f"/proposals/{proposal_id}/progress"),
        },
    }
    spec["draft_ready"] = {
        "icon": "article",
        "accent": "green-700",
        "bg": "bg-green-50",
        "border": "border-green-500",
        "title": "Draft ready — run the Auto Review-Revise Loop",
        "body": (
            "Each section iterates Reviewer A (compliance & risk) + "
            "Reviewer B (persuasion) and Writer Team for up to 6 "
            "passes. ALL findings auto-revise. Pass 3+ escalates: writer "
            "is told to delete or [NEEDS_HUMAN]-wrap content reviewers "
            "keep flagging. Stuck-detection bails after 2 consecutive "
            "no-progress passes. Un-resolvable findings + remaining "
            "[NEEDS_HUMAN] markers surface for you to triage. Cost: "
            "~$30-100 per full proposal depending on how many sections "
            "need escalation."
        ),
        "btn": None,
        "btns": [
            {
                "label": "Run Auto Review-Revise Loop",
                "icon": "all_inclusive",
                "on_click": lambda: _run_reviewer_loop(proposal_id),
                "props": "color=primary",
            },
            {
                "label": "Approve for submission",
                "icon": "check_circle",
                "on_click": lambda: _approve_proposal(proposal_id),
                "props": "flat color=positive",
            },
        ],
    }
    spec["reviewing"] = {
        "icon": "all_inclusive",
        "accent": "blue-700",
        "bg": "bg-blue-50",
        "border": "border-blue-500",
        "title": "Auto Review-Revise Loop running",
        "body": (
            "Reviewer A + Reviewer B and Writer Team are "
            "passing each section back and forth until clean, capped, or "
            "stuck. Watch live progress on Run Progress. Cancel will stop "
            "at the next checkpoint (~30-90s after the current LLM call)."
        ),
        # Two buttons: Watch + Cancel. The banner renderer below knows how
        # to render either a single `btn` or a list of buttons via `btns`.
        "btn": None,
        "btns": [
            {
                "label": "Watch Run Progress",
                "icon": "monitor_heart",
                "on_click": lambda: ui.navigate.to(f"/proposals/{proposal_id}/progress"),
                "props": "color=primary",
            },
            {
                "label": "Cancel",
                "icon": "stop_circle",
                "on_click": lambda: _cancel_auto_review(proposal_id),
                "props": "flat color=red-7",
            },
        ],
    }
    spec["pricing"] = {
        "icon": "payments",
        "accent": "blue-700",
        "bg": "bg-blue-50",
        "border": "border-blue-500",
        "title": "Cost Analysis running",
        "body": (
            "Cost Analysis Agent is building the pricing breakdown and P&L. "
            "Cost Reviewer will validate it next. Follow live progress on "
            "Run Progress."
        ),
        "btn": None,
    }
    spec["awaiting_approval"] = {
        "icon": "verified_user",
        "accent": "amber-700",
        "bg": "bg-amber-50",
        "border": "border-amber-500",
        "title": "Action needed: final review before submission",
        "body": (
            "Pricing and review are done. Walk through every tab one more time. "
            "When you're satisfied, approve for submission."
        ),
        "btn": {
            "label": "Approve for submission",
            "icon": "check_circle",
            "on_click": lambda: _approve_proposal(proposal_id),
        },
    }
    spec["approved"] = {
        "icon": "check_circle",
        "accent": "green-700",
        "bg": "bg-green-50",
        "border": "border-green-500",
        "title": "Approved — submit through the agency's system",
        "body": (
            "This proposal is human-approved. Download the package and submit "
            "through the agency's required channel (NEVER auto-submitted by this tool). "
            "Mark as Submitted once delivered."
        ),
        "btn": {
            "label": "Mark as submitted",
            "icon": "send",
            "on_click": lambda: _mark_submitted(proposal_id),
        },
    }
    spec["submitted"] = {
        "icon": "send",
        "accent": "green-700",
        "bg": "bg-green-50",
        "border": "border-green-500",
        "title": "Submitted",
        "body": (
            "Keep this record active while awaiting the agency response. "
            "After recording the award outcome and any debrief, archive it "
            "to make the complete record read-only."
        ),
        "btn": {
            "label": "Archive proposal", "icon": "inventory_2",
            "on_click": lambda: _archive_proposal(proposal_id),
        },
    }
    spec["archived"] = {
        "icon": "inventory_2",
        "accent": "slate-500",
        "bg": "bg-slate-50",
        "border": "border-slate-400",
        "title": "Archived (read-only)",
        "body": "This proposal is archived. No further actions available.",
        "btn": None,
    }

    s = spec.get(
        status_val,
        {
            "icon": "help_outline",
            "accent": "slate-500",
            "bg": "bg-slate-50",
            "border": "border-slate-400",
            "title": f"Status: {_status_label(status_val)}",
            "body": "No specific next-step guidance for this status.",
            "btn": None,
        },
    )

    with ui.card().classes(f"w-full {s['bg']} border-l-4 {s['border']}"):
        with ui.row().classes("items-start gap-3 w-full"):
            ui.icon(s["icon"]).classes(f"text-{s['accent']} text-2xl")
            with ui.column().classes("gap-0 flex-1"):
                ui.label(s["title"]).classes(
                    f"text-base font-semibold text-{s['accent'].replace('-700', '-900').replace('-500', '-800')}"
                )
                ui.label(s["body"]).classes(
                    f"text-sm text-{s['accent'].replace('-700', '-800').replace('-500', '-700')}"
                )
            # Banner button(s). A spec entry can supply EITHER `btn` (single
            # primary action) OR `btns` (list of buttons with optional props
            # per button — used when we need a Cancel alongside the primary
            # action).
            buttons = s.get("btns") or ([s["btn"]] if s.get("btn") else [])
            if buttons:
                with ui.row().classes("gap-2"):
                    for b in buttons:
                        ui.button(
                            b["label"],
                            icon=b["icon"],
                            on_click=b["on_click"],
                        ).props(b.get("props", "color=primary"))


def _sign_off_scope(proposal_id: int) -> None:
    result = sign_off_scope(proposal_id)
    if not result["ok"]:
        ui.notify(
            "Scope cannot be signed off yet:\n" + "\n".join(result["blockers"][:4]),
            type="warning", multi_line=True, timeout=8000,
        )
        return
    ui.notify(
        "Scope signed off — click Generate Draft Outline to propose a section structure.",
        type="positive",
        multi_line=True,
        timeout=6000,
    )
    ui.navigate.reload()


def _force_restart_writer_team(proposal_id: int, *, on_change=None) -> None:
    """Manual override for a stuck DRAFT_IN_PROGRESS status. Use case:
    the writer thread crashed (terminal closed, app killed) but the
    proposal status didn't get reverted because the crash-recovery
    pass only runs at app startup and the user hasn't restarted.
    Flips status back to AWAITING_DRAFT so spawn_writer_team's status
    transition is valid, then respawns. Existing section drafts are
    preserved — the writer's resume logic skips sections with
    has_draft=True."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            ui.notify("Proposal not found.", type="negative")
            return
        if p.status == ProposalStatus.DRAFT_IN_PROGRESS:
            p.status = ProposalStatus.AWAITING_DRAFT
        elif p.status not in (
            ProposalStatus.AWAITING_DRAFT,
            ProposalStatus.DRAFT_READY,
            ProposalStatus.REVIEWING,
        ):
            ui.notify(
                f"Cannot restart from status "
                f"{_status_label(p.status.value)} — the writer team "
                f"only runs once cost is in place.",
                type="warning",
                multi_line=True,
                timeout=6000,
            )
            return
    spawn_writer_team(proposal_id)
    ui.notify(
        "Writer Team respawned in resume mode — already-drafted "
        "sections are skipped. Watch Run Progress for live updates.",
        type="positive",
        multi_line=True,
        timeout=6000,
    )
    if on_change is not None:
        on_change()
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _begin_drafting(proposal_id: int) -> None:
    """Spawn the Writer Team. Phase 2B gate — only valid when the
    proposal is AWAITING_DRAFT (cost build complete) or DRAFT_READY
    (re-running the writer post-draft). Writer team's existing
    status-flip handles DRAFT_IN_PROGRESS → DRAFT_READY."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            ui.notify("Proposal not found.", type="negative")
            return
        valid_states = {
            ProposalStatus.AWAITING_DRAFT,
            ProposalStatus.DRAFT_READY,
            ProposalStatus.REVIEWING,
        }
        if p.status not in valid_states:
            ui.notify(
                f"Drafting is gated until the cost build is in "
                f"place. Current status: {_status_label(p.status.value)}.",
                type="warning",
                multi_line=True,
                timeout=6000,
            )
            return
    spawn_writer_team(proposal_id)
    ui.notify(
        "Writer Team drafting now. Each section appears on the "
        "Draft tab as it completes — your approved team and cost "
        "build are baked into the cached prefix.",
        type="positive",
        multi_line=True,
        timeout=6000,
    )
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _regenerate_section(proposal_id: int, section_pk: int) -> None:
    spawn_writer_for_section(proposal_id, section_pk)
    ui.notify(
        "Section regenerating — refresh the Draft tab in ~30s to see the new draft.",
        type="positive",
        multi_line=True,
        timeout=5000,
    )
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _refine_section_with_ai(proposal_id: int, section_pk: int, directive: str) -> None:
    """Per-section AI refine — same path as Regenerate but with a USER
    DIRECTIVE block forwarded to the Writer Team agent."""
    if not directive or not directive.strip():
        ui.notify("Empty directive — type a change you want first.", type="warning")
        return
    spawn_writer_for_section(proposal_id, section_pk, user_directive=directive.strip())
    ui.notify(
        "Section refining with your directive — refresh the Draft tab in ~30s.",
        type="positive",
        multi_line=True,
        timeout=5000,
    )
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _run_reviewer_loop(proposal_id: int) -> None:
    """Primary reviewer entry point — kicks off the AUTO loop.

    Per-section: review (A+B) → if critical/major → auto-accept those → writer
    regenerates with findings as a directive → re-review. Repeats until the
    section is clean, hits the 4-pass cap, or stops making progress. MINOR
    findings + remaining critical/major after stop conditions surface on the
    Findings tab for user review.
    """
    spawn_auto_review_revise_loop(proposal_id)
    ui.notify(
        "Auto Review-Revise Loop running. Each section iterates Reviewer A+B "
        "and Writer Team until clean, capped, or stuck. Watch Run Progress; "
        "remaining issues surface on the Findings tab when done.",
        type="positive",
        multi_line=True,
        timeout=7000,
    )
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _run_reviewer_only(proposal_id: int) -> None:
    """Diagnostic path — single review pass, no auto-revisions. Useful when
    the user wants to check status without spending on a full loop."""
    spawn_reviewer_loop(proposal_id)
    ui.notify(
        "Single review pass running (no auto-revisions). Findings appear on "
        "the Findings tab when each section completes.",
        type="positive",
        multi_line=True,
        timeout=6000,
    )
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _cancel_auto_review(proposal_id: int) -> None:
    """Stop the auto review-revise loop at the next checkpoint, OR recover
    from a stuck `Reviewing` state if the loop crashed without cleanup.

    - Live loop registered → send cancel; it'll exit at next checkpoint
      (~30-90s after the in-progress LLM call returns).
    - No live loop AND status=REVIEWING → loop probably crashed (Python
      restarted, unhandled exception, etc.). Reset status to DRAFT_READY
      so the user isn't stuck.
    - No live loop AND status≠REVIEWING → nothing to do.
    """
    from datetime import datetime as _dt

    from app.services.cancellation import JOB_AUTO_REVIEW, request_cancel

    if request_cancel(JOB_AUTO_REVIEW, proposal_id):
        ui.notify(
            "Cancel signal sent. The loop will stop at the next checkpoint "
            "— typically within 30-90 seconds after the current LLM call "
            "completes. Status will return to Draft Ready.",
            type="warning",
            multi_line=True,
            timeout=8000,
        )
        return

    # No registered loop. Check if status is stuck on REVIEWING — that means
    # the loop died without running its finally block. Force-reset.
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        current_status = (
            p.status.value if p and hasattr(p.status, "value") else (str(p.status) if p else None)
        )
        if p and current_status == "reviewing":
            p.status = ProposalStatus.DRAFT_READY
            # Also write a terminal stage so the busy banner clears and the
            # Pipeline activity log shows the recovery.
            db.add(
                AgentRun(
                    proposal_id=proposal_id,
                    agent_name="_stage",
                    model_used=None,
                    started_at=_dt.now(UTC),
                    completed_at=_dt.now(UTC),
                    status="completed",
                    error_text=(
                        "🛑 Status reset by user — no active loop was running "
                        "but status was stuck on Reviewing. The loop likely "
                        "crashed or the Python process restarted."
                    ),
                )
            )
            ui.notify(
                "No active loop found, but status was stuck on Reviewing — reset to Draft Ready.",
                type="warning",
                multi_line=True,
                timeout=6000,
            )
            return

    ui.notify(
        "No active auto review-revise loop to cancel.",
        type="info",
    )


def _re_review_section(proposal_id: int, section_pk: int) -> None:
    spawn_reviewer_for_section(proposal_id, section_pk)
    ui.notify(
        "Re-reviewing section — refresh the Findings tab in ~30s.",
        type="positive",
        multi_line=True,
        timeout=5000,
    )
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _apply_accepted_findings(proposal_id: int, section_pk: int) -> None:
    """Build a directive from accepted findings on the section and trigger
    a directive-driven Writer Team regenerate.

    Refuses if the auto-loop is currently processing this section — two
    concurrent writer threads on the same section race on draft_text_markdown
    and would lose intermediate state.
    """
    from app.services.cancellation import get_active_sections

    if section_pk in get_active_sections(proposal_id):
        ui.notify(
            "The auto-loop is currently processing this section — wait until "
            "it moves on, or cancel the loop first. Other sections can be "
            "applied immediately.",
            type="warning",
            multi_line=True,
            timeout=6000,
        )
        return

    findings = get_accepted_findings_for_section(section_pk)
    if not findings:
        ui.notify(
            "No accepted findings on this section — accept some first.",
            type="warning",
        )
        return
    directive = build_directive_from_findings(findings)
    spawn_writer_for_section(proposal_id, section_pk, user_directive=directive)
    ui.notify(
        f"Writer Team revising with {len(findings)} accepted finding(s). "
        f"After it finishes, re-run the reviewer to verify.",
        type="positive",
        multi_line=True,
        timeout=6000,
    )
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _approve_proposal(proposal_id: int) -> None:
    result = approve_for_submission(proposal_id)
    if not result["ok"]:
        ui.notify(
            "Approval blocked:\n" + "\n".join(result["blockers"][:5]),
            type="warning", multi_line=True, timeout=10000,
        )
        return
    ui.notify("Approved for submission.", type="positive")
    ui.navigate.reload()


def _mark_submitted(proposal_id: int) -> None:
    with ui.dialog() as dialog, ui.card().classes("max-w-lg"):
        ui.label("Confirm submission").classes("text-lg font-semibold")
        ui.label(
            "Only mark this proposal submitted after it has been delivered "
            "through the agency's required channel. This app never submits it for you."
        ).classes("text-sm")

        def confirm() -> None:
            result = mark_submitted(proposal_id)
            if not result["ok"]:
                ui.notify(
                    "Cannot mark submitted:\n" + "\n".join(result["blockers"][:5]),
                    type="warning", multi_line=True, timeout=10000,
                )
                return
            dialog.close()
            ui.notify("Marked as submitted.", type="positive")
            ui.navigate.reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Confirm submitted", icon="send", on_click=confirm).props(
                "color=primary"
            )
    dialog.open()


def _archive_proposal(proposal_id: int) -> None:
    with ui.dialog() as dialog, ui.card().classes("max-w-lg"):
        ui.label("Archive proposal?").classes("text-lg font-semibold")
        ui.label(
            "Archiving preserves the proposal, package, drafts, decisions, and "
            "run history, but makes every proposal tab read-only. Record any "
            "award outcome and debrief details first; there is no unarchive flow."
        ).classes("text-sm")

        def confirm() -> None:
            result = archive_proposal(proposal_id)
            if not result["ok"]:
                ui.notify(
                    "Cannot archive:\n" + "\n".join(result["blockers"][:5]),
                    type="warning", multi_line=True, timeout=8000,
                )
                return
            dialog.close()
            ui.notify("Proposal archived as a read-only record.", type="positive")
            ui.navigate.reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Archive", icon="inventory_2", on_click=confirm).props(
                "color=primary"
            )
    dialog.open()


# Sentinel for "do not modify this field" — distinguishes from explicit None
# (which means "clear the value").
# `_render_spend_tab` extracted to app/ui/tabs/spend.py.


# ---- Cost tab ------------------------------------------------------------


def _open_edit_cost_basis_dialog(
    proposal_id: int,
    *,
    on_change=None,
) -> None:
    """Edit our_cost_basis values in data/pricing/payment_systems.json
    without leaving the app. Available on the Cost tab when
    service_line=payment_systems. On save: persists the edits, clears
    the JSON lru_cache, and re-computes profit math on the existing
    payment market scan (no LLM call). The 'industry-typical defaults'
    caveat in the writer's view auto-disappears once the user toggles
    `_confirmed_by_ops_finance` to true."""
    from app.services.service_line import (
        get_payment_cost_basis,
        recompute_payment_profit_math,
        update_payment_cost_basis,
    )

    current = get_payment_cost_basis()

    with ui.dialog() as dialog, ui.card().classes("w-[600px] max-w-full"):
        ui.label("Edit Cost Basis").classes("text-lg font-semibold")
        ui.label(
            "Internal cost-of-service inputs the Payment Market "
            "Researcher uses to compute profit math (revenue − cost = "
            "profit). Update these as Quadratic Financial ops finance "
            "provides accurate per-county numbers; flip the "
            "'Confirmed by ops finance' toggle once you're done."
        ).classes("text-sm opacity-70 pb-2")

        with ui.column().classes("gap-3 w-full"):
            with ui.column().classes("gap-0 w-full"):
                sponsor_in = ui.number(
                    label="Sponsor / acquirer fee (basis points)",
                    value=float(current.get("sponsor_acquirer_fee_bps") or 0),
                    format="%.2f",
                ).classes("w-full")
                ui.label(current.get("_sponsor_acquirer_fee_bps_note") or "").classes("text-xs opacity-60")

            with ui.column().classes("gap-0 w-full"):
                gateway_in = ui.number(
                    label="Gateway / network access per transaction (USD)",
                    value=float(current.get("gateway_per_txn_usd") or 0),
                    format="%.4f",
                ).classes("w-full")
                ui.label(current.get("_gateway_per_txn_usd_note") or "").classes("text-xs opacity-60")

            with ui.column().classes("gap-0 w-full"):
                pci_in = ui.number(
                    label="Annualized PCI compliance cost (USD/year)",
                    value=float(current.get("annualized_pci_compliance_usd") or 0),
                    format="%.0f",
                ).classes("w-full")
                ui.label(current.get("_annualized_pci_compliance_usd_note") or "").classes(
                    "text-xs opacity-60"
                )

            with ui.column().classes("gap-0 w-full"):
                support_in = ui.number(
                    label="Annualized support allocation per client (USD/year)",
                    value=float(current.get("annualized_support_allocation_usd") or 0),
                    format="%.0f",
                ).classes("w-full")
                ui.label(current.get("_annualized_support_allocation_usd_note") or "").classes(
                    "text-xs opacity-60"
                )

            with ui.row().classes(
                "items-start gap-2 w-full pt-2 px-3 py-2 rounded bg-emerald-50 border border-emerald-300"
            ):
                confirmed_in = ui.checkbox(
                    "Confirmed by ops finance",
                    value=bool(current.get("_confirmed_by_ops_finance")),
                )
                ui.label(
                    "Once checked, the writer's narrative stops "
                    "disclaiming these numbers as 'industry-typical "
                    "defaults' and treats them as authoritative."
                ).classes("text-xs opacity-70")

        status = ui.label("").classes("text-sm italic")

        async def _save() -> None:
            try:
                status.set_text("Saving cost basis to data/pricing/payment_systems.json…")
                status.classes(replace="text-sm italic text-blue-700")
                update_payment_cost_basis(
                    proposal_id=proposal_id,
                    sponsor_acquirer_fee_bps=float(sponsor_in.value or 0),
                    gateway_per_txn_usd=float(gateway_in.value or 0),
                    annualized_pci_compliance_usd=float(pci_in.value or 0),
                    annualized_support_allocation_usd=float(support_in.value or 0),
                    confirmed_by_ops_finance=bool(confirmed_in.value),
                )
                status.set_text("Re-computing profit math on the existing market scan…")
                ok = await asyncio.to_thread(
                    recompute_payment_profit_math,
                    proposal_id,
                )
                if ok:
                    ui.notify(
                        "Cost basis saved and this proposal's profit math "
                        "recomputed. Rerun Payment Cost Reviewer; other "
                        "payment proposals must refresh their profit math "
                        "before submission.",
                        type="positive",
                        multi_line=True,
                        timeout=8000,
                    )
                else:
                    ui.notify(
                        "Cost basis saved. (No payment market scan "
                        "exists yet — profit math will compute on the "
                        "next Run Payment Market Research.)",
                        type="info",
                        multi_line=True,
                        timeout=6000,
                    )
                if on_change is not None:
                    on_change()
                dialog.close()
            except Exception as exc:
                log.exception("edit cost basis: save failed")
                status.set_text(f"⚠ Save failed: {exc}")
                status.classes(replace="text-sm italic text-red-700")

        with ui.row().classes("items-center justify-end gap-2 w-full pt-3"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button(
                "Save",
                icon="save",
                on_click=_save,
            ).props("color=primary")

    dialog.open()


def _open_finding_refine_dialog(
    f: dict,
    *,
    proposal_id: int,
    on_change,
) -> None:
    """Refine-with-AI dialog for a cost-review finding. Iteratively
    refines `recommended_change` via Sonnet 4.6, then saves the
    result as user_note (with user_action=accepted).

    Renamed 2026-04-30 from `_open_refine_dialog` because pages.py
    had a second function with the SAME name further down (the
    per-section refine dialog used by the Draft tab) that was
    silently shadowing this one — module-level rebinding meant
    `pages._open_refine_dialog` resolved to the section version,
    so the Cost Review tab's "Refine with AI" finding button
    called the wrong function with the wrong signature.

    Layout:
      - Read-only finding context (subject + description)
      - Editable recommendation textarea (becomes the live working
        copy; Refine replaces, manual edits are preserved)
      - Guidance textarea + Refine button
      - Save & Accept / Cancel
    """
    from app.agents.cost_review_refiner import refine_recommendation
    from app.services.cost_reviewer import (
        update_cost_review_finding_action,
    )

    # Initial recommendation: prefer the user's edited version (if
    # they previously accepted with edits) over the agent's
    # original. Matches the display logic.
    initial_rec = (
        f.get("user_note")
        if (f.get("user_action") == "accepted" and f.get("user_note"))
        else f.get("recommended_change") or ""
    )
    subject, body = _split_subject_from_finding_text(f.get("finding_text") or "")
    severity = f.get("severity") or "MINOR"
    category = f.get("category") or ""
    row_ids = list(f.get("row_ids") or [])

    with ui.dialog() as dialog, ui.card().classes("w-[640px]"):
        ui.label("Refine with AI").classes("text-base font-medium")
        ui.label(
            "Provide context the original reviewer didn't have, "
            "and Sonnet 4.6 will rewrite the recommendation. "
            "Iterate as many times as you want — each refine "
            "replaces the editable text below; manual edits are "
            "preserved between refines."
        ).classes("text-xs opacity-70 pb-2")

        # Finding context (read-only)
        with ui.card().classes("w-full bg-slate-50 border border-slate-200"):
            ui.label(f"{severity} · {category}").classes("text-xs uppercase opacity-70")
            if subject:
                ui.label(subject).classes("text-sm font-medium")
            if body:
                ui.label(body).classes("text-xs opacity-80 whitespace-pre-wrap")

        ui.label("Recommended change").classes("text-xs uppercase opacity-70 pt-3")
        rec_input = (
            ui.textarea(
                value=initial_rec,
            )
            .props("outlined dense autogrow")
            .classes("w-full")
        )

        ui.label("Your guidance").classes("text-xs uppercase opacity-70 pt-3")
        ui.label(
            "Tell the AI what to change. Be specific about context or constraints the agent missed."
        ).classes("text-xs opacity-60")
        guidance_input = (
            ui.textarea(
                placeholder=(
                    "e.g., 'SSP is delivered by a sub via vehicle "
                    "X' — or 'reduce hours instead of changing salary'"
                ),
            )
            .props("outlined dense")
            .classes("w-full")
        )

        # Status label below the Refine button — shows feedback
        # while a refinement is in flight or after it completes.
        status_lbl = ui.label("").classes("text-xs opacity-70 pt-1")

        with ui.row().classes("items-center gap-2 pt-2"):
            refine_btn = ui.button(
                "Refine",
                icon="auto_awesome",
            ).props("color=primary")

            async def _do_refine() -> None:
                guidance = (guidance_input.value or "").strip()
                if not guidance:
                    ui.notify(
                        "Provide guidance to refine.",
                        type="warning",
                        timeout=3000,
                    )
                    return
                # Disable + show progress so the user knows the
                # call is in flight (Sonnet typically takes 3-8s).
                # Run the LLM in a thread so the event loop stays
                # responsive and the websocket doesn't drop.
                refine_btn.props("loading disable")
                status_lbl.set_text("Refining…")
                try:
                    new_text = await asyncio.to_thread(
                        refine_recommendation,
                        proposal_id=proposal_id,
                        severity=severity,
                        category=category,
                        subject=subject,
                        finding_text=body,
                        current_recommendation=(rec_input.value or ""),
                        user_guidance=guidance,
                    )
                    rec_input.set_value(new_text)
                    guidance_input.set_value("")
                    status_lbl.set_text(f"Refined ({len(new_text)} chars). Edit below or save when ready.")
                except Exception as exc:
                    status_lbl.set_text(f"Refine failed: {exc}")
                    ui.notify(
                        f"Refine failed: {type(exc).__name__}",
                        type="negative",
                        timeout=5000,
                    )
                finally:
                    refine_btn.props(remove="loading disable")
                    refine_btn.props("color=primary")

            refine_btn.on("click", _do_refine)

        ui.separator().classes("my-3")

        with ui.row().classes("justify-end gap-2 w-full"):
            ui.button(
                "Cancel",
                on_click=dialog.close,
            ).props("flat")

            def _save() -> None:
                new_text = (rec_input.value or "").strip()
                if not new_text:
                    ui.notify(
                        "Recommendation cannot be empty.",
                        type="warning",
                        timeout=3000,
                    )
                    return
                update_cost_review_finding_action(
                    finding_ids=row_ids,
                    user_action="accepted",
                    user_note=new_text,
                )
                dialog.close()
                ui.notify(
                    "Saved AI-refined recommendation.",
                    type="positive",
                    timeout=3000,
                )
                on_change()

            ui.button(
                "Save & Accept",
                on_click=_save,
            ).props("color=positive")

    dialog.open()


def _render_cost_pricing_headline(
    packages: list[dict],
    *,
    proposal_id: int,
    tab_state: dict,
    on_select,
) -> None:
    """Four cards side-by-side: LOW / MEDIUM / HIGH (each shows its
    persisted price) plus CUSTOM (the editable what-if mode). All
    four are clickable; the selected card's detail renders directly
    below. LOW/MEDIUM/HIGH show read-only detail (metric tiles, labor
    table, indirect, ODCs, phases, recommendation, exec summary).
    CUSTOM shows the slider + editable labor + editable ODCs +
    budget tracker.

    The MEDIUM card carries the star (★) marking it as the default
    proposed scenario. The currently SELECTED card gets a thick
    accent border + ring so it's visually distinct from the proposed
    marker. Clicking the already-selected card is a no-op (avoids a
    redundant refresh that would reset the Custom slider's edits)."""
    by_scenario = {p["scenario"]: p for p in packages}
    selected = tab_state.get("selected_scenario") or "MEDIUM"
    if selected not in ("LOW", "MEDIUM", "HIGH", "CUSTOM"):
        selected = "MEDIUM"
        tab_state["selected_scenario"] = selected

    def _make_select_handler(scenario_name):
        def _select() -> None:
            # No-op when the same card is clicked again — avoids
            # destroying any in-progress edits in the Custom view.
            if tab_state.get("selected_scenario") == scenario_name:
                return
            tab_state["selected_scenario"] = scenario_name
            # Persist LOW / MEDIUM / HIGH so the Cost Writer + Cost
            # Reviewer pick up the user's choice. CUSTOM stays in
            # memory only — there's no PricingPackage row for it,
            # so persisting it would break the downstream lookup.
            if scenario_name in ("LOW", "MEDIUM", "HIGH"):
                from app.services.pricing import set_proposed_scenario

                try:
                    set_proposed_scenario(proposal_id, scenario_name)
                except Exception:
                    log.exception(
                        "failed to persist proposed_scenario=%s for proposal %d",
                        scenario_name,
                        proposal_id,
                    )
            on_select()

        return _select

    with ui.card().classes("w-full"):
        ui.label("Proposed price").classes("text-base font-medium")
        ui.label(
            "Three risk-adjusted policy scenarios plus a Custom "
            "slider for free-form what-if exploration. Click a card "
            "to see its detail below. The selected LOW/MEDIUM/HIGH "
            "scenario is what the Cost Writer will use. The Custom "
            "view lets you edit labor lines and ODCs, then save those "
            "edits back into the policy scenarios before drafting. "
            "Changing the proposed scenario or saved build requires a "
            "fresh Cost Reviewer run before submission."
        ).classes("text-xs opacity-70 pb-2")

        with ui.row().classes("w-full gap-3 flex-wrap"):
            for scenario in ("LOW", "MEDIUM", "HIGH"):
                pkg = by_scenario.get(scenario)
                if pkg is None:
                    continue
                _render_scenario_card(
                    pkg,
                    is_proposed=(scenario == "MEDIUM"),
                    is_selected=(scenario == selected),
                    on_click=_make_select_handler(scenario),
                )
            # Fourth card — Custom Slider. Different render path
            # (no fixed price, action-card style).
            _render_custom_select_card(
                is_selected=(selected == "CUSTOM"),
                on_click=_make_select_handler("CUSTOM"),
            )

        # Detail of selected scenario / mode, rendered directly below
        # the cards. LOW/MEDIUM/HIGH go through the read-only renderer;
        # CUSTOM goes through the editable slider view.
        ui.separator().classes("my-3")
        visual = _COST_SCENARIO_VISUAL.get(selected, {})
        ui.label(f"{visual.get('label', selected)} — detail").classes("text-base font-medium pb-1")
        if selected == "CUSTOM":
            _render_custom_scenario_tab(packages)
        else:
            selected_pkg = by_scenario.get(selected)
            if selected_pkg is not None:
                _render_scenario_detail(selected_pkg)
            else:
                _empty_state(
                    f"{selected} scenario not persisted.",
                    icon="info",
                )


def _render_custom_select_card(
    *,
    is_selected: bool,
    on_click,
) -> None:
    """The 4th selection card — Custom Slider mode. Action-style
    rather than price-style: no $ headline, just a call-to-action
    visual that matches the height/width of the H/M/L cards so the
    row stays balanced.

    Selected state uses the same border-4 + ring + shadow treatment
    as the price cards for consistency."""
    visual = _COST_SCENARIO_VISUAL["CUSTOM"]
    bg_class = visual["card_bg"]
    border_class = visual["border"]
    selection_classes = "border-4 ring-2 ring-violet-300 shadow-md" if is_selected else "border"
    cursor_class = "cursor-pointer"

    column = ui.column().classes(
        f"flex-1 min-w-[260px] p-3 rounded {bg_class} "
        f"{border_class} {selection_classes} {cursor_class} gap-1 "
        "transition-shadow hover:shadow-md"
    )
    column.on("click", lambda _e=None: on_click())

    with column:
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(visual["label"]).classes("text-xs font-medium uppercase opacity-80")
            if is_selected:
                ui.icon("check_circle").classes("text-violet-600").tooltip(
                    "Currently selected — editable detail shown below"
                )
        with ui.row().classes("items-center gap-2 pt-1"):
            ui.icon("tune").classes("text-violet-600 text-2xl")
            ui.label("Custom slider").classes("text-xl font-semibold")
        ui.label(visual["subtitle"]).classes("text-xs opacity-60")
        ui.label(
            "Free-form what-if mode — edit any number, drag the bid "
            "posture slider, watch the budget tracker recompute. "
            "Edits do not persist."
        ).classes("text-xs opacity-70 pt-2")


def _render_scenario_card(
    pkg: dict,
    *,
    is_proposed: bool,
    is_selected: bool = False,
    on_click=None,
) -> None:
    """One H/M/L scenario card. The proposed scenario shows the star;
    the SELECTED card gets a thicker accent border + a ring so the
    user can tell which card's detail is showing below.

    When `on_click` is provided, the card is clickable — the entire
    card surface is hooked up so the user doesn't have to aim for a
    button."""
    visual = _COST_SCENARIO_VISUAL[pkg["scenario"]]
    bg_class = visual["card_bg"]
    border_class = visual["border"]
    # Selected gets thicker border + a ring for stronger visual.
    # When also proposed, the ring color leans on the accent palette.
    selection_classes = "border-4 ring-2 ring-emerald-300 shadow-md" if is_selected else "border"
    cursor_class = "cursor-pointer" if on_click is not None else ""

    column = ui.column().classes(
        f"flex-1 min-w-[260px] p-3 rounded {bg_class} "
        f"{border_class} {selection_classes} {cursor_class} gap-1 "
        "transition-shadow hover:shadow-md"
    )
    if on_click is not None:
        # Make the entire card surface a click target.
        column.on("click", lambda _e=None: on_click())

    with column:
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(visual["label"]).classes("text-xs font-medium uppercase opacity-80")
            with ui.row().classes("items-center gap-1"):
                if is_selected:
                    ui.icon("check_circle").classes("text-emerald-600").tooltip(
                        "Currently selected — detail shown below"
                    )
                if is_proposed:
                    ui.icon("star").classes("text-amber-500").tooltip("Default proposed scenario for the bid")
        price = pkg.get("total_proposed_price") or 0
        ui.label(f"${price:,.0f}").classes("text-2xl font-semibold pt-1")
        ui.label(visual["subtitle"]).classes("text-xs opacity-60")

        # Margin + position + recommendation chips
        with ui.row().classes("items-center gap-2 pt-2 flex-wrap"):
            margin_pct = (pkg.get("indirect_costs_json") or {}).get("profit_pct") or 0
            ui.chip(
                f"{margin_pct:.0%} margin",
                icon="trending_up",
            ).props("dense color=blue-grey-3 text-color=black")

            position = pkg.get("vs_market_position") or ""
            pos_label, pos_color = _COST_POSITION_VISUAL.get(
                position,
                (position.upper(), "blue-grey-3"),
            )
            ui.chip(pos_label, icon="straighten").props(f"dense color={pos_color} text-color=white")

            rec = pkg.get("bid_recommendation") or ""
            rec_label, rec_color = _COST_BID_REC_VISUAL.get(
                rec,
                (rec.upper(), "primary"),
            )
            ui.chip(rec_label, icon="gavel").props(f"dense color={rec_color} text-color=white")

        rationale = (pkg.get("recommendation_rationale") or "").strip()
        if rationale:
            ui.label(rationale).classes("text-xs opacity-70 pt-1")


def _render_cost_market_scan_section(
    scan: dict,
    packages: list[dict],
) -> None:
    """Market scan card — band, methodology, comparable awards table,
    competitors table. Awards and competitors are collapsed by default."""
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Market scan").classes("text-base font-medium")
            ui.label(
                f"updated {scan['updated_at']:%Y-%m-%d %H:%M}" if scan.get("updated_at") else ""
            ).classes("text-xs font-mono opacity-60")

        # Band summary row.
        with ui.row().classes("items-center gap-4 pt-2 flex-wrap"):
            _render_band_chip(
                "Low",
                scan.get("market_band_low_usd"),
                "blue-grey-7",
            )
            _render_band_chip(
                "Mid",
                scan.get("market_band_mid_usd"),
                "blue-7",
            )
            _render_band_chip(
                "High",
                scan.get("market_band_high_usd"),
                "deep-orange-7",
            )
            ui.element("div").classes("flex-1")
            ui.label(
                f"{len(scan.get('comparable_awards') or [])} comparable "
                f"award(s) · "
                f"{len(scan.get('competitors') or [])} competitor(s)"
            ).classes("text-xs opacity-70")

        # Visual band relative to our scenarios. ASCII-style number
        # line: low-mid-high markers + our 3 prices overlaid.
        if scan.get("market_band_low_usd") is not None and scan.get("market_band_high_usd") is not None:
            _render_band_visualization(scan, packages)

        # Methodology — preserved verbatim from agent + sparse-data
        # warning the service composes in.
        methodology = (scan.get("methodology") or "").strip()
        if methodology:
            with ui.expansion(
                "How the band was derived",
                icon="info",
                value=False,
            ).classes("w-full pt-2"):
                ui.label(methodology).classes("text-sm whitespace-pre-wrap opacity-80")

        # Comparable awards table.
        awards = scan.get("comparable_awards") or []
        if awards:
            with ui.expansion(
                f"Comparable awards ({len(awards)})",
                icon="business",
                value=False,
            ).classes("w-full pt-2"):
                _render_awards_table(awards)

        # Competitors table.
        competitors = scan.get("competitors") or []
        if competitors:
            with ui.expansion(
                f"Likely competitors ({len(competitors)})",
                icon="groups",
                value=False,
            ).classes("w-full pt-2"):
                _render_competitors_table(competitors)


def _render_band_chip(label: str, v: float | None, color: str) -> None:
    """One number-with-label chip in the market band row."""
    val = f"${float(v):,.0f}" if v is not None else "—"
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-xs opacity-60 uppercase")
        # `color` is a Quasar palette name (e.g., "blue-grey-7"); use
        # the corresponding CSS variable so it works regardless of
        # whether Tailwind has the matching utility class.
        ui.label(val).classes("text-lg font-semibold").style(f"color: var(--q-{color});")


def _render_band_visualization(
    scan: dict,
    packages: list[dict],
) -> None:
    """Horizontal bar showing the market band with our H/M/L prices
    overlaid as ticks. Helps the user see at a glance whether we're
    in / above / below market."""
    band_low = float(scan["market_band_low_usd"])
    band_high = float(scan["market_band_high_usd"])
    band_mid = scan.get("market_band_mid_usd")
    band_mid_v = float(band_mid) if band_mid is not None else None

    by_scenario = {p["scenario"]: p for p in packages}
    our_prices: list[tuple[str, float]] = []
    for sc in ("LOW", "MEDIUM", "HIGH"):
        pkg = by_scenario.get(sc)
        if pkg and pkg.get("total_proposed_price") is not None:
            our_prices.append((sc, float(pkg["total_proposed_price"])))

    # Range for the visualization: stretch to whichever is wider —
    # market band or our scenario range.
    all_vals = [band_low, band_high]
    all_vals.extend(p for _, p in our_prices)
    range_low = min(all_vals)
    range_high = max(all_vals)
    if range_high <= range_low:
        return  # degenerate — skip
    span = range_high - range_low
    pad = span * 0.05  # 5% padding each side
    plot_low = range_low - pad
    plot_high = range_high + pad
    plot_span = plot_high - plot_low

    def _pct(x: float) -> float:
        return (x - plot_low) / plot_span * 100.0

    band_low_pct = _pct(band_low)
    band_high_pct = _pct(band_high)
    band_width_pct = max(0.5, band_high_pct - band_low_pct)

    with ui.column().classes("w-full pt-3 gap-1"):
        ui.label("Where our scenarios sit vs the market band:").classes("text-xs opacity-70")

        with ui.element("div").classes("w-full h-10 relative bg-slate-100 rounded mt-1"):
            # Band bar.
            ui.element("div").classes("absolute h-full bg-blue-200 rounded").style(
                f"left: {band_low_pct:.1f}%; width: {band_width_pct:.1f}%;"
            )
            # Mid-band tick.
            if band_mid_v is not None:
                mid_pct = _pct(band_mid_v)
                ui.element("div").classes("absolute h-full w-px bg-blue-700").style(f"left: {mid_pct:.1f}%;")
            # Our scenario ticks (vertical lines + labels).
            for label, price in our_prices:
                tick_pct = _pct(price)
                color = {
                    "LOW": "rgb(59,130,246)",  # blue-500
                    "MEDIUM": "rgb(16,185,129)",  # emerald-500
                    "HIGH": "rgb(245,158,11)",  # amber-500
                }.get(label, "rgb(0,0,0)")
                ui.element("div").classes("absolute w-1 rounded").style(
                    f"left: {tick_pct:.1f}%; top: -6px; height: calc(100% + 12px); background: {color};"
                )
                ui.label(label).classes("absolute text-xs font-medium").style(
                    f"left: {tick_pct:.1f}%; top: 100%; transform: translate(-50%, 4px); color: {color};"
                )

        # Endpoint labels for the band.
        with ui.row().classes("w-full justify-between pt-5"):
            ui.label(f"${plot_low:,.0f}").classes("text-xs font-mono opacity-50")
            ui.label(f"${plot_high:,.0f}").classes("text-xs font-mono opacity-50")


def _provenance_label(row: dict) -> str:
    """Compact text label for the dual-pipeline provenance column on
    market-scan tables. Empty for legacy single-provider rows so the
    cell renders blank rather than a confusing "(none)"."""
    cb = row.get("confirmed_by") or []
    suffix = " ⚠" if row.get("needs_review") else ""
    if len(cb) >= 2:
        return f"✓ Both{suffix}"
    if "gemini" in cb:
        return f"Gemini{suffix}"
    if "claude" in cb:
        return f"Claude{suffix}"
    return ""


def _render_provenance_summary(
    rows: list[dict],
    *,
    label_subject: str,
) -> None:
    """Aggregate provenance chips above a market-scan table. Renders
    nothing when no row carries provenance (legacy single-provider
    data) so legacy scans don't sprout extra UI clutter."""
    n_consensus = sum(1 for r in rows if len(r.get("confirmed_by") or []) >= 2)
    n_gemini = sum(1 for r in rows if (r.get("confirmed_by") or []) == ["gemini"])
    n_claude = sum(1 for r in rows if (r.get("confirmed_by") or []) == ["claude"])
    n_review = sum(1 for r in rows if r.get("needs_review"))
    n_attributed = n_consensus + n_gemini + n_claude
    if not n_attributed:
        return
    with ui.row().classes("gap-1 pb-2 flex-wrap"):
        if n_consensus:
            ui.chip(
                f"CONSENSUS · {n_consensus}",
                icon="verified",
            ).props("color=green-7 text-color=white size=sm").tooltip(
                f"Both Gemini + Claude+web independently surfaced these {n_consensus} {label_subject}(s)."
            )
        if n_gemini:
            ui.chip(
                f"Gemini only · {n_gemini}",
                icon="travel_explore",
            ).props("color=blue-grey-6 text-color=white outline size=sm").tooltip(
                f"Only Gemini grounded research surfaced these {n_gemini} {label_subject}(s)."
            )
        if n_claude:
            ui.chip(
                f"Claude only · {n_claude}",
                icon="travel_explore",
            ).props("color=blue-grey-6 text-color=white outline size=sm").tooltip(
                f"Only Claude+web research surfaced these {n_claude} {label_subject}(s)."
            )
        if n_review:
            ui.chip(
                f"Verify · {n_review}",
                icon="error_outline",
            ).props("color=amber-7 text-color=white size=sm").tooltip(
                f"{n_review} {label_subject}(s) are single-provider "
                f"hits worth verifying — for awards, low relevance; "
                f"for competitors, rate inference depends on which "
                f"awards were found."
            )


def _render_awards_table(awards: list[dict]) -> None:
    """Comparable awards as a NiceGUI table with clickable URLs."""
    _render_provenance_summary(awards, label_subject="award")
    columns = [
        {"name": "title", "label": "Award", "field": "title", "align": "left"},
        {"name": "value", "label": "Value", "field": "value", "align": "right"},
        {"name": "pop", "label": "PoP", "field": "pop", "align": "right"},
        {"name": "awardee", "label": "Awardee", "field": "awardee"},
        {"name": "agency", "label": "Customer", "field": "agency"},
        {"name": "rel", "label": "Relevance", "field": "rel", "align": "right"},
        {"name": "src", "label": "Source", "field": "src", "align": "center"},
        {"name": "url", "label": "URL", "field": "url", "align": "left"},
    ]
    rows: list[dict] = []
    for a in awards:
        v = a.get("award_value_usd")
        rows.append(
            {
                "title": (a.get("award_title") or "")[:90],
                "value": f"${float(v):,.0f}" if v is not None else "—",
                "pop": (
                    f"{a.get('period_of_performance_months')}mo"
                    if a.get("period_of_performance_months")
                    else "—"
                ),
                "awardee": a.get("awardee_name") or "—",
                "agency": a.get("customer_agency") or "—",
                "rel": (
                    f"{float(a['relevance_score']):.2f}" if a.get("relevance_score") is not None else "—"
                ),
                "src": _provenance_label(a),
                "url": a.get("source_url") or "",
            }
        )
    table = ui.table(
        columns=columns,
        rows=rows,
        row_key="title",
    ).classes("w-full")
    table.add_slot(
        "body-cell-url",
        r"""
<q-td :props="props">
  <a :href="props.value" target="_blank" rel="noopener"
     class="text-blue-600 underline text-xs"
     v-if="props.value">{{ props.value.substring(0, 60) }}…</a>
  <span v-else class="opacity-50 text-xs">—</span>
</q-td>
    """,
    )

    notes = [a.get("notes") for a in awards if a.get("notes")]
    if any(notes):
        with ui.expansion(
            "Per-award notes",
            icon="notes",
            value=False,
        ).classes("w-full pt-2"):
            for a in awards:
                note = (a.get("notes") or "").strip()
                if note:
                    ui.label(f"{(a.get('award_title') or '?')[:60]}: {note}").classes("text-xs opacity-80")


def _render_competitors_table(competitors: list[dict]) -> None:
    """Competitors with rate inference and source URLs."""
    _render_provenance_summary(competitors, label_subject="competitor")
    columns = [
        {"name": "name", "label": "Competitor", "field": "name", "align": "left"},
        {"name": "likelihood", "label": "Likelihood", "field": "likelihood"},
        {"name": "rate_low", "label": "Rate low", "field": "rate_low", "align": "right"},
        {"name": "rate_high", "label": "Rate high", "field": "rate_high", "align": "right"},
        {"name": "src", "label": "Source", "field": "src", "align": "center"},
        {"name": "basis", "label": "Basis", "field": "basis", "align": "left"},
    ]
    rows: list[dict] = []
    for c in competitors:
        rl = c.get("estimated_rate_low_usd")
        rh = c.get("estimated_rate_high_usd")
        rows.append(
            {
                "name": c.get("competitor_name") or "—",
                "likelihood": (c.get("likelihood_to_bid") or "").upper(),
                "rate_low": f"${float(rl):.0f}/hr" if rl is not None else "—",
                "rate_high": f"${float(rh):.0f}/hr" if rh is not None else "—",
                "src": _provenance_label(c),
                "basis": (c.get("rate_estimation_basis") or "")[:160],
            }
        )
    ui.table(
        columns=columns,
        rows=rows,
        row_key="name",
    ).classes("w-full")

    # Source URLs surfaced as a separate compact list because table
    # cells get crowded with multiple links per row.
    with ui.expansion(
        "Competitor source URLs",
        icon="link",
        value=False,
    ).classes("w-full pt-2"):
        for c in competitors:
            urls = list(c.get("source_urls") or [])
            if not urls:
                continue
            with ui.row().classes("items-start gap-2 pt-1 flex-wrap"):
                ui.label(c.get("competitor_name") or "—").classes("text-xs font-medium w-40 truncate")
                with ui.column().classes("gap-0 flex-1"):
                    for u in urls:
                        ui.link(u, u, new_tab=True).classes("text-xs text-blue-600 truncate")


def _render_payment_market_scan_section(
    proposal_id: int,
    scan_data: dict,
) -> None:
    """Payment-systems Market Scan card. Equivalent of the labor-flow
    `_render_cost_market_scan_section` but shaped for the
    PaymentMarketScanResult schema: pricing recommendation (model +
    median-vs-proposed rate comparison + positioning), processed-
    volume estimate (low/mid/high), profit math (revenue - cost =
    profit with caveats), comparable processor awards table,
    competitor processors table, insufficient-data warning. Reuses
    the labor-flow's provenance helpers for consistency.

    Renders nothing when scan_data is empty — caller is responsible
    for the gate."""
    if not scan_data:
        return

    pricing = scan_data.get("pricing_structure") or {}
    volume = scan_data.get("volume_estimate") or {}
    profit = scan_data.get("profit_math") or {}
    awards = scan_data.get("comparable_awards") or []
    competitors = scan_data.get("competitor_processors") or []
    insufficient = bool(scan_data.get("insufficient_data_warning"))
    citations = scan_data.get("citations") or []

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Payment Market Scan").classes("text-base font-medium")
            with ui.row().classes("items-center gap-2"):
                if pricing.get("pricing_model"):
                    ui.chip(
                        pricing["pricing_model"],
                        icon="account_balance",
                    ).props("color=blue-7 text-color=white outline size=sm").tooltip(
                        pricing.get("pricing_model_rationale") or ""
                    )
                ui.label(f"{len(awards)} award(s) · {len(competitors)} competitor(s)").classes(
                    "text-xs opacity-70"
                )

        if insufficient:
            with ui.row().classes(
                "items-center gap-2 pt-2 px-3 py-2 rounded bg-amber-50 border-l-4 border-amber-500 w-full"
            ):
                ui.icon("warning").classes("text-amber-700")
                ui.label(
                    "Insufficient data warning — fewer than 3 "
                    "comparable rate disclosures found. Treat the "
                    "proposed rates as informed by industry-typical "
                    "ranges; cite the limitation in narrative if the "
                    "buyer asks for comparable-award benchmarking."
                ).classes("text-sm text-amber-900")

        # ----- Pricing model selector + mismatch handling -----
        _render_payment_pricing_model_selector(proposal_id, pricing)

        # ----- LLM RECOMMENDATION CALLOUT (prominent) -----
        # Compose a one-line rendering of the proposed bid posture so
        # the user sees the recommendation up-front instead of having
        # to read the rate chips and rationale expansion separately.
        _render_payment_bid_posture_callout(pricing)

        # ----- Pricing recommendation chips row -----
        with ui.row().classes("w-full gap-3 pt-3 flex-wrap"):
            _render_payment_rate_chip(
                "Credit-card markup",
                pricing.get("proposed_credit_card_markup_bps"),
                pricing.get("median_market_credit_card_markup_bps"),
                unit="bps",
            )
            _render_payment_rate_chip(
                "Per-transaction",
                pricing.get("proposed_per_txn_fee_usd"),
                pricing.get("median_market_per_txn_fee_usd"),
                unit="usd_decimal",
            )
            _render_payment_rate_chip(
                "ACH fee",
                pricing.get("proposed_ach_fee_usd"),
                pricing.get("median_market_ach_fee_usd"),
                unit="usd_decimal",
            )
            _render_payment_rate_chip(
                "Monthly fee",
                pricing.get("proposed_monthly_fee_usd"),
                pricing.get("median_market_monthly_fee_usd"),
                unit="usd",
            )
            ui.element("div").classes("flex-1")
            positioning = (pricing.get("rate_positioning") or "match").replace("_", " ")
            ui.chip(
                f"posture: {positioning}",
                icon="trending_down",
            ).props("color=emerald-7 text-color=white size=sm").tooltip(
                "Where our proposed rates sit relative to market median."
            )

        if pricing.get("other_fees_recommended"):
            with ui.expansion(
                "Other recommended fees",
                icon="receipt_long",
                value=False,
            ).classes("w-full pt-2"):
                for fee in pricing["other_fees_recommended"]:
                    name = fee.get("name", "?")
                    amt = fee.get("amount_usd")
                    amt_str = f"${amt:.2f}" if amt is not None else "(custom)"
                    note = f" — {fee['notes']}" if fee.get("notes") else ""
                    ui.label(f"• {name}: {amt_str}{note}").classes("text-sm")

        # The pricing_model_rationale was moved up into the
        # _render_payment_bid_posture_callout above — no separate
        # expansion needed; the recommendation reads as one unit.

        # ----- Volume + profit math row -----
        with ui.row().classes("w-full gap-3 pt-4 flex-wrap"):
            _render_payment_volume_card(volume)
            _render_payment_profit_card(profit)

        if volume.get("estimation_basis"):
            with ui.expansion(
                "How the volume was estimated",
                icon="calculate",
                value=False,
            ).classes("w-full pt-2"):
                ui.label(volume["estimation_basis"]).classes("text-sm whitespace-pre-wrap opacity-80")

        # ----- Side-by-side competitive comparison (us vs each
        # competitor's typical pricing) — defaults to expanded so the
        # user can size up the field without clicking.
        if competitors:
            _render_payment_competitive_comparison(pricing, competitors)

        # ----- Awards table — default expanded -----
        if awards:
            with ui.expansion(
                f"Comparable processor awards ({len(awards)})",
                icon="business",
                value=True,
            ).classes("w-full pt-2"):
                _render_payment_awards_table(awards)

        # ----- Competitors table — default expanded -----
        if competitors:
            with ui.expansion(
                f"Likely competitor processors ({len(competitors)}) — full detail",
                icon="groups",
                value=True,
            ).classes("w-full pt-2"):
                _render_payment_competitors_table(competitors)

        # ----- Citations expansion -----
        if citations:
            with ui.expansion(
                f"Source citations ({len(citations)})",
                icon="link",
                value=False,
            ).classes("w-full pt-2"):
                for c in citations:
                    title = c.get("title") or "(untitled)"
                    uri = c.get("uri") or ""
                    if uri:
                        ui.link(
                            f"{title[:80]}",
                            uri,
                            new_tab=True,
                        ).classes("text-xs text-blue-600 truncate")


def _render_payment_pricing_model_selector(
    proposal_id: int,
    pricing: dict,
) -> None:
    """Row of selector chips for the four supported pricing models.
    The agent's recommended model gets a ★ badge; the user's selected
    model is highlighted. Click persists immediately. When the user's
    selection differs from the agent's recommendation, an amber
    mismatch banner appears with a 'Re-run scan with <model> focus'
    button — kicks off a fresh dual-pipeline scan with the user's
    chosen model as a directive to the agent."""
    from app.services.service_line import (
        get_selected_pricing_model,
        list_payment_pricing_models,
        set_selected_pricing_model,
    )

    agent_recommended = (pricing.get("pricing_model") or "").strip() or None
    user_selected = get_selected_pricing_model(proposal_id)
    effective = user_selected or agent_recommended

    def _on_pick(model_id: str) -> None:
        if model_id == effective:
            return  # no-op click
        try:
            set_selected_pricing_model(proposal_id, model_id)
        except Exception as exc:
            log.exception(
                "set_selected_pricing_model failed for proposal %d",
                proposal_id,
            )
            ui.notify(f"Failed to save selection: {exc}", type="negative")
            return
        if model_id == agent_recommended:
            ui.notify(
                f"Reverted to agent's recommended model ({agent_recommended}).",
                type="positive",
            )
        else:
            ui.notify(
                f"Selected pricing model: {model_id}. The bid posture "
                f"below still shows rates for the agent's "
                f"recommendation — click 'Re-run scan with "
                f"{model_id} focus' to get rates aligned with your "
                f"selection, then rerun Payment Cost Reviewer.",
                type="info", multi_line=True, timeout=8000,
            )
        # Trigger a refresh of the cost tab so the chip + banner
        # state updates without requiring a manual reload.
        ui.navigate.reload()

    def _on_rerun_focus(model_id: str) -> None:
        spawn_payment_market_research(proposal_id, model_focus=model_id)
        ui.notify(
            f"Re-running Payment Market Research with {model_id} "
            f"focus — Gemini + Claude will research rates aligned "
            f"with your selected model. Watch progress on Run "
            f"Progress; this tab will reflect the new scan when done.",
            type="positive",
            multi_line=True,
            timeout=8000,
        )
        ui.navigate.to(f"/proposals/{proposal_id}/progress")

    with ui.card().classes("w-full mt-2"):
        ui.label("Pricing model").classes("text-xs opacity-70 uppercase tracking-wide")
        with ui.row().classes("items-center gap-2 pt-1 flex-wrap"):
            for entry in list_payment_pricing_models():
                model_id = entry["id"]
                is_selected = model_id == effective
                is_agent_pick = model_id == agent_recommended
                star = " ★" if is_agent_pick else ""
                label = f"{entry['label']}{star}"
                tooltip = entry["description"]
                if is_agent_pick:
                    tooltip += " (Agent's recommendation for this procurement.)"
                btn = ui.button(
                    label,
                    on_click=lambda m=model_id: _on_pick(m),
                ).tooltip(tooltip)
                if is_selected:
                    btn.props("color=primary unelevated")
                else:
                    btn.props("flat color=primary")

        # Mismatch banner — only when user explicitly overrode the
        # agent's recommendation. Suppressed when user_selected is
        # None (== agent recommendation) or matches it.
        if user_selected and agent_recommended and user_selected != agent_recommended:
            with ui.row().classes(
                "items-center gap-2 mt-3 px-3 py-2 rounded bg-amber-50 border-l-4 border-amber-500 w-full"
            ):
                ui.icon("info").classes("text-amber-700")
                with ui.column().classes("gap-0 flex-1"):
                    ui.label(
                        f"Agent recommended {agent_recommended}; you've selected {user_selected}."
                    ).classes("text-sm font-medium text-amber-900")
                    ui.label(
                        f"The rates below are still for "
                        f"{agent_recommended}. Re-run the scan with "
                        f"{user_selected} focus to get rates aligned "
                        f"with the model you've selected (~$0.24, "
                        f"~60-90s)."
                    ).classes("text-xs text-amber-800")
                ui.button(
                    f"Re-run scan with {user_selected} focus",
                    icon="refresh",
                    on_click=lambda m=user_selected: _on_rerun_focus(m),
                ).props("color=amber-7 unelevated size=sm")


def _render_payment_bid_posture_callout(pricing: dict) -> None:
    """Prominent recommended-bid-posture block at the top of the
    Payment Market Scan card. Shows the LLM's headline recommendation
    in plain language: pricing model + one-line rate summary +
    market-comparison sentence + rationale paragraph. The user
    shouldn't need to read every chip + expansion to understand 'what
    we're proposing and why.'"""
    model = (pricing.get("pricing_model") or "").strip()
    if not model and not pricing.get("pricing_model_rationale"):
        return  # nothing to highlight — agent didn't produce a recommendation

    one_line = _payment_proposed_rate_one_liner(pricing)
    vs_market = _payment_market_comparison_one_liner(pricing)

    with ui.card().classes("w-full bg-emerald-50 border-l-4 border-emerald-500 mt-3"):
        with ui.row().classes("items-center gap-2 w-full"):
            ui.icon("auto_awesome").classes("text-emerald-700")
            ui.label("Recommended Bid Posture").classes(
                "text-sm font-semibold text-emerald-900 uppercase tracking-wide"
            )
        if one_line:
            ui.label(one_line).classes("text-lg font-semibold text-emerald-950 pt-1")
        if vs_market:
            ui.label(vs_market).classes("text-sm text-emerald-800")
        rationale = (pricing.get("pricing_model_rationale") or "").strip()
        if rationale:
            ui.label(rationale).classes("text-sm text-slate-700 whitespace-pre-wrap pt-2")


def _payment_proposed_rate_one_liner(pricing: dict) -> str:
    """One-line natural-language rendering of the proposed bid: e.g.
    'Interchange + 25 bps + $0.10/transaction + $50/month'. Returns
    an empty string when the agent left every field null."""
    model = (pricing.get("pricing_model") or "").strip().lower()
    bps = pricing.get("proposed_credit_card_markup_bps")
    per_txn = pricing.get("proposed_per_txn_fee_usd")
    monthly = pricing.get("proposed_monthly_fee_usd")
    ach = pricing.get("proposed_ach_fee_usd")

    parts: list[str] = []
    if model == "interchange_plus":
        if bps is not None:
            parts.append(f"Interchange + {int(bps)} bps")
        if per_txn is not None:
            parts.append(f"${float(per_txn):.2f}/transaction")
    elif model == "flat_rate":
        if bps is not None:
            parts.append(f"{float(bps) / 100:.2f}%")
        if per_txn is not None:
            parts.append(f"${float(per_txn):.2f}/transaction")
    elif model == "tiered":
        if bps is not None:
            parts.append(f"~{int(bps)} bps blended")
    elif model == "percentage_of_collected":
        if bps is not None:
            parts.append(f"{float(bps) / 100:.2f}% of collected")
    else:
        # Unknown / hybrid model — render whatever's present.
        if bps is not None:
            parts.append(f"{int(bps)} bps")
        if per_txn is not None:
            parts.append(f"${float(per_txn):.2f}/transaction")

    if ach is not None:
        parts.append(f"${float(ach):.2f}/ACH")
    if monthly is not None:
        parts.append(f"${float(monthly):,.0f}/month")

    if not parts:
        return ""
    return " + ".join(parts)


def _payment_market_comparison_one_liner(pricing: dict) -> str:
    """One-line summary of where our proposed rate sits relative to
    the market median. Returns empty string when the agent didn't
    produce a comparable median."""
    proposed = pricing.get("proposed_credit_card_markup_bps")
    median = pricing.get("median_market_credit_card_markup_bps")
    positioning = (pricing.get("rate_positioning") or "").replace("_", " ")

    if proposed is None or median is None or median == 0:
        if positioning:
            return f"Posture: {positioning} relative to market median."
        return ""

    delta_bps = float(proposed) - float(median)
    pct_below = -delta_bps / float(median) * 100.0 if median else 0.0
    if abs(delta_bps) < 0.5:
        verdict = f"At market median ({int(median)} bps)"
    elif delta_bps < 0:
        verdict = (
            f"Beats market median by {abs(int(delta_bps))} bps "
            f"({abs(pct_below):.1f}%) — median is {int(median)} bps"
        )
    else:
        verdict = (
            f"{int(delta_bps)} bps above market median "
            f"({abs(pct_below):.1f}% premium) — median is {int(median)} bps"
        )
    if positioning:
        verdict += f". Stated posture: {positioning}."
    return verdict


def _render_payment_competitive_comparison(
    pricing: dict,
    competitors: list[dict],
) -> None:
    """Side-by-side compact table putting our proposed rate next to
    each competitor's typical pricing summary. Helps the user size
    up the field at a glance without scrolling through the full
    competitor table."""
    # Build the rows: us first, then each competitor.
    our_summary = _payment_proposed_rate_one_liner(pricing)
    if not our_summary:
        our_summary = "(no proposed rate yet — re-run the scan)"

    rows: list[dict] = [
        {
            "vendor": "Quadratic Financial / NAC (us)",
            "summary": our_summary,
            "likelihood": "—",
            "position": "proposed bid",
            "highlight": True,
        }
    ]
    for c in competitors:
        rows.append(
            {
                "vendor": c.get("name") or "—",
                "summary": (c.get("typical_pricing_summary") or "")[:160] or "(no rate disclosed)",
                "likelihood": (c.get("likelihood_to_bid") or "").upper() or "—",
                "position": (c.get("market_position") or "").lower() or "—",
                "highlight": False,
            }
        )

    with ui.card().classes("w-full mt-3"):
        ui.label("Competitive comparison").classes("text-base font-medium")
        ui.label(
            "Side-by-side: our proposed rate vs each likely competitor's "
            "typical pricing for this kind of procurement. Rate posture "
            "should beat or match the median competitor; outliers above "
            "us are pricing leaders we shouldn't anchor against."
        ).classes("text-xs opacity-70 pb-2")

        with ui.element("div").classes("w-full"):
            # Header row
            with ui.row().classes(
                "items-center w-full gap-2 px-3 py-2 "
                "bg-slate-100 text-xs font-semibold uppercase tracking-wide"
            ):
                ui.label("Vendor").classes("flex-1")
                ui.label("Pricing summary").classes("flex-[2]")
                ui.label("Position").classes("w-32")
                ui.label("Likely to bid").classes("w-28")
            # Body rows
            for row in rows:
                bg = (
                    "bg-emerald-50 border-l-4 border-emerald-500"
                    if row["highlight"]
                    else "bg-white border-l-4 border-transparent"
                )
                with ui.row().classes(
                    f"items-center w-full gap-2 px-3 py-2 text-sm {bg} border-b border-slate-200"
                ):
                    weight = "font-semibold" if row["highlight"] else ""
                    ui.label(row["vendor"]).classes(f"flex-1 {weight}")
                    ui.label(row["summary"]).classes(f"flex-[2] {weight}")
                    ui.label(row["position"]).classes("w-32 text-xs opacity-70")
                    likelihood_color = {
                        "HIGH": "text-rose-700",
                        "MEDIUM": "text-amber-700",
                        "LOW": "text-slate-500",
                    }.get(row["likelihood"], "text-slate-500")
                    ui.label(row["likelihood"]).classes(f"w-28 text-xs font-medium {likelihood_color}")


def _render_payment_rate_chip(
    label: str,
    proposed: float | int | None,
    median: float | int | None,
    *,
    unit: str,
) -> None:
    """One rate-comparison chip in the payment scan header. Shows the
    proposed value bold + the median value in muted text below for
    quick read of where we sit vs market."""

    def _fmt(v: float | int | None) -> str:
        if v is None:
            return "—"
        if unit == "bps":
            return f"{int(v)} bps"
        if unit == "usd":
            return f"${float(v):,.0f}"
        if unit == "usd_decimal":
            return f"${float(v):.2f}"
        return str(v)

    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-xs opacity-60 uppercase")
        ui.label(_fmt(proposed)).classes("text-lg font-semibold")
        if median is not None:
            ui.label(f"market: {_fmt(median)}").classes("text-xs opacity-50")
        else:
            ui.label("market: —").classes("text-xs opacity-30")


def _render_payment_volume_card(volume: dict) -> None:
    """Annual processed-volume estimate (low / mid / high)."""
    with ui.card().classes("flex-1 min-w-72 bg-blue-50"):
        ui.label("Annual processed-volume estimate").classes("text-xs opacity-70 uppercase")
        with ui.row().classes("items-baseline gap-2 pt-1"):
            mid = volume.get("annual_processed_volume_midpoint_usd")
            if mid is not None:
                ui.label(f"${float(mid):,.0f}").classes("text-2xl font-semibold text-blue-900")
                ui.label("midpoint").classes("text-xs opacity-60")
            else:
                ui.label("—").classes("text-2xl font-semibold opacity-50")
        with ui.row().classes("items-center gap-3 text-xs opacity-70"):
            low = volume.get("annual_processed_volume_low_usd")
            high = volume.get("annual_processed_volume_high_usd")
            if low is not None:
                ui.label(f"low ${float(low):,.0f}")
            if high is not None:
                ui.label(f"high ${float(high):,.0f}")
        if volume.get("estimated_transaction_count_annual"):
            n = int(volume["estimated_transaction_count_annual"])
            avg = volume.get("average_transaction_size_usd")
            avg_str = f" · avg ${float(avg):.2f}/txn" if avg is not None else ""
            ui.label(f"~{n:,} transactions/yr{avg_str}").classes("text-xs opacity-60 pt-1")
        confidence = (volume.get("confidence") or "").lower()
        if confidence:
            color = {
                "high": "emerald-7",
                "medium": "amber-7",
                "low": "deep-orange-7",
            }.get(confidence, "blue-grey-6")
            ui.chip(
                f"confidence: {confidence}",
                icon="speed",
            ).props(f"color={color} text-color=white size=sm outline").classes("mt-1")


def _render_payment_profit_card(profit: dict) -> None:
    """Annual profit projection (revenue - costs = profit)."""
    with ui.card().classes("flex-1 min-w-72 bg-emerald-50"):
        ui.label("Annual profit projection").classes("text-xs opacity-70 uppercase")
        rev = profit.get("annual_processor_revenue_midpoint_usd")
        cost = profit.get("annual_internal_costs_usd")
        net = profit.get("annual_net_profit_midpoint_usd")
        margin = profit.get("profit_margin_pct_at_midpoint")

        with ui.row().classes("items-baseline gap-2 pt-1"):
            if net is not None:
                color_class = "text-emerald-900" if net > 0 else "text-red-700"
                ui.label(f"${float(net):,.0f}").classes(f"text-2xl font-semibold {color_class}")
                if margin is not None:
                    ui.label(f"({float(margin):.1%} margin)").classes("text-xs opacity-70")
            else:
                ui.label("—").classes("text-2xl font-semibold opacity-50")
                ui.label("(insufficient data)").classes("text-xs opacity-50")

        with ui.column().classes("gap-0 pt-2 text-xs opacity-70"):
            if rev is not None:
                ui.label(f"revenue (mid): ${float(rev):,.0f}")
            if cost is not None:
                ui.label(f"internal costs (mid): ${float(cost):,.0f}")

        caveats = profit.get("cost_basis_assumptions") or []
        if caveats:
            with ui.expansion(
                "Cost basis caveats",
                icon="info_outline",
                value=False,
            ).classes("w-full pt-2"):
                for caveat in caveats:
                    ui.label(f"• {caveat}").classes("text-xs opacity-80 whitespace-normal")

        if profit.get("computation_notes"):
            with ui.expansion(
                "Profit computation",
                icon="functions",
                value=False,
            ).classes("w-full pt-1"):
                ui.label(profit["computation_notes"]).classes("text-xs whitespace-pre-wrap opacity-70")


def _render_payment_awards_table(awards: list[dict]) -> None:
    """Comparable processor awards table — payment-specific columns."""
    _render_provenance_summary(awards, label_subject="award")
    columns = [
        {"name": "processor", "label": "Processor", "field": "processor", "align": "left"},
        {"name": "customer", "label": "Customer", "field": "customer", "align": "left"},
        {"name": "year", "label": "Year", "field": "year", "align": "right"},
        {"name": "model", "label": "Model", "field": "model"},
        {"name": "rate", "label": "Disclosed rate", "field": "rate", "align": "left"},
        {"name": "volume", "label": "Annual vol.", "field": "volume", "align": "right"},
        {"name": "term", "label": "Term", "field": "term", "align": "right"},
        {"name": "src", "label": "Source", "field": "src", "align": "center"},
        {"name": "url", "label": "URL", "field": "url", "align": "left"},
    ]
    rows: list[dict] = []
    for a in awards:
        v = a.get("annual_volume_estimate_usd")
        rows.append(
            {
                "processor": a.get("processor_name") or "—",
                "customer": a.get("customer_name") or "—",
                "year": (str(a["award_year"]) if a.get("award_year") else "—"),
                "model": (a.get("pricing_model") or "—"),
                "rate": (a.get("disclosed_credit_card_rate_text") or "—")[:90],
                "volume": (f"${float(v):,.0f}" if v is not None else "—"),
                "term": (f"{a['contract_term_years']}yr" if a.get("contract_term_years") else "—"),
                "src": _provenance_label(a),
                "url": a.get("source_url") or "",
            }
        )
    table = ui.table(
        columns=columns,
        rows=rows,
        row_key="processor",
    ).classes("w-full")
    table.add_slot(
        "body-cell-url",
        r"""
<q-td :props="props">
  <a :href="props.value" target="_blank" rel="noopener"
     class="text-blue-600 underline text-xs"
     v-if="props.value">{{ props.value.substring(0, 50) }}…</a>
  <span v-else class="opacity-50 text-xs">—</span>
</q-td>
    """,
    )

    if any(a.get("notes") for a in awards):
        with ui.expansion(
            "Per-award notes",
            icon="notes",
            value=False,
        ).classes("w-full pt-2"):
            for a in awards:
                note = (a.get("notes") or "").strip()
                if note:
                    ui.label(
                        f"{(a.get('processor_name') or '?')} → {(a.get('customer_name') or '?')}: {note}"
                    ).classes("text-xs opacity-80")


def _render_payment_competitors_table(competitors: list[dict]) -> None:
    """Likely competitor processors table — payment-specific columns."""
    _render_provenance_summary(competitors, label_subject="competitor")
    columns = [
        {"name": "name", "label": "Processor", "field": "name", "align": "left"},
        {"name": "position", "label": "Position", "field": "position"},
        {"name": "likelihood", "label": "Likelihood", "field": "likelihood"},
        {"name": "summary", "label": "Pricing summary", "field": "summary", "align": "left"},
        {"name": "src", "label": "Source", "field": "src", "align": "center"},
    ]
    rows: list[dict] = []
    for c in competitors:
        rows.append(
            {
                "name": c.get("name") or "—",
                "position": (c.get("market_position") or "").lower(),
                "likelihood": (c.get("likelihood_to_bid") or "").upper(),
                "summary": (c.get("typical_pricing_summary") or "")[:160],
                "src": _provenance_label(c),
            }
        )
    ui.table(
        columns=columns,
        rows=rows,
        row_key="name",
    ).classes("w-full")

    with ui.expansion(
        "Competitor source URLs",
        icon="link",
        value=False,
    ).classes("w-full pt-2"):
        for c in competitors:
            urls = list(c.get("source_urls") or [])
            if not urls:
                continue
            with ui.row().classes("items-start gap-2 pt-1 flex-wrap"):
                ui.label(c.get("name") or "—").classes("text-xs font-medium w-40 truncate")
                with ui.column().classes("gap-0 flex-1"):
                    for u in urls:
                        ui.link(u, u, new_tab=True).classes("text-xs text-blue-600 truncate")

        if any(c.get("notes") for c in competitors):
            ui.label("Notes:").classes("text-xs opacity-60 pt-2")
            for c in competitors:
                note = (c.get("notes") or "").strip()
                if note:
                    ui.label(f"{c.get('name') or '?'}: {note}").classes("text-xs opacity-80")


def _render_cost_build_section(packages: list[dict]) -> None:
    """Custom-slider exploration view — free-form what-if pricing
    with editable labor lines and ODCs.

    Per-scenario detail (LOW / MEDIUM / HIGH) lives in the Proposed
    Price section above (click a card to see that scenario's
    detail). This section is for free exploration outside the policy
    stops: drag the slider for any margin/contingency point between
    AGGRESSIVE 5% and HIGH 30%, edit labor lines and ODCs to
    fine-tune the estimate, watch the budget tracker."""
    with ui.card().classes("w-full"):
        ui.label("Cost build detail — Custom slider").classes("text-base font-medium")
        ui.label(
            "Free-form what-if view. Drag the slider, edit labor "
            "lines, edit ODCs — all numbers recompute against the "
            "deterministic wrap-rate / margin formulas. Edits do "
            "NOT persist; they're for exploration. The persisted "
            "policy scenarios (LOW / MEDIUM / HIGH) are above."
        ).classes("text-xs opacity-70 pb-2")
        _render_custom_scenario_tab(packages)


# Slider-position policy: 4-stop piecewise-linear interpolation.
# Slider expands below the policy LOW posture down to a 5% margin
# floor so the user can explore aggressive bidding. Policy stops
# (LOW / MEDIUM / HIGH) still sit at exact slider positions so
# users can land on them precisely; the new "Aggressive" floor sits
# at slider position 0.
#
# Slider 0   → 5% margin / 0% contingency  (Aggressive, below floor)
# Slider 52  → 18% / 0%                    (LOW — Competitive)
# Slider 80  → 25% / 5%                    (MEDIUM — Target)
# Slider 100 → 30% / 10%                   (HIGH — Protective)
#
# Anywhere left of slider position 52, margin sits below the
# profit_policy floor and the bid_recommendation will flag
# walk_away — that's informative, not a bug.
_SLIDER_FLOOR_MARGIN_PCT = 0.05
_SLIDER_FLOOR_CONTINGENCY_PCT = 0.0
_LOW_MARGIN_PCT = 0.18
_MEDIUM_MARGIN_PCT = 0.25
_HIGH_MARGIN_PCT = 0.30
_LOW_CONTINGENCY_PCT = 0.0
_MEDIUM_CONTINGENCY_PCT = 0.05
_HIGH_CONTINGENCY_PCT = 0.10

# Slider positions for the policy stops. LOW lives at 52% because
# (0.18 - 0.05) / (0.30 - 0.05) = 0.52; MEDIUM at 80%, HIGH at 100%.
_LOW_SLIDER_POS = 52
_MEDIUM_SLIDER_POS = 80
_HIGH_SLIDER_POS = 100
# Default slider position when the Custom tab opens. MEDIUM (Target)
# is the default proposed scenario across the rest of the system.
_DEFAULT_SLIDER_POS = _MEDIUM_SLIDER_POS


def _slider_to_params(slider_val: float) -> tuple[float, float]:
    """Map slider 0-100 → (margin_pct, contingency_pct).

    4-stop piecewise-linear interpolation:
      0  → AGGRESSIVE (5% / 0%)
      52 → LOW (18% / 0%)
      80 → MEDIUM (25% / 5%)
      100 → HIGH (30% / 10%)

    Each segment lerps linearly between its endpoints, so the policy
    postures sit at the exact integer positions above and the user
    can dial below the policy floor for what-if exploration.
    """
    s = max(0.0, min(100.0, float(slider_val)))
    if s <= _LOW_SLIDER_POS:
        # Segment 1: AGGRESSIVE → LOW. Contingency stays at 0% across
        # this stretch since both endpoints are 0%.
        t = s / _LOW_SLIDER_POS
        margin = _SLIDER_FLOOR_MARGIN_PCT + t * (_LOW_MARGIN_PCT - _SLIDER_FLOOR_MARGIN_PCT)
        contingency = _LOW_CONTINGENCY_PCT  # 0.0 → 0.0
    elif s <= _MEDIUM_SLIDER_POS:
        # Segment 2: LOW → MEDIUM. Contingency starts climbing here.
        t = (s - _LOW_SLIDER_POS) / (_MEDIUM_SLIDER_POS - _LOW_SLIDER_POS)
        margin = _LOW_MARGIN_PCT + t * (_MEDIUM_MARGIN_PCT - _LOW_MARGIN_PCT)
        contingency = _LOW_CONTINGENCY_PCT + t * (_MEDIUM_CONTINGENCY_PCT - _LOW_CONTINGENCY_PCT)
    else:
        # Segment 3: MEDIUM → HIGH.
        t = (s - _MEDIUM_SLIDER_POS) / (_HIGH_SLIDER_POS - _MEDIUM_SLIDER_POS)
        margin = _MEDIUM_MARGIN_PCT + t * (_HIGH_MARGIN_PCT - _MEDIUM_MARGIN_PCT)
        contingency = _MEDIUM_CONTINGENCY_PCT + t * (_HIGH_CONTINGENCY_PCT - _MEDIUM_CONTINGENCY_PCT)
    return margin, contingency


def _render_custom_scenario_tab(packages: list[dict]) -> None:
    """Slider-driven custom scenario view with editable labor lines.

    Two layers of user control:
      1. Slider + coverage radio set the scenario *parameters* (margin /
         contingency / coverage), which determine the TARGET budget.
      2. Editable labor-line cells let the user fine-tune Salary / Hours /
         Loaded $/hr / Billed $/hr per category. Edits drive the ACTUAL
         price; budget bar at the top tracks Actual vs Target with green/
         yellow/red coloring.

    Edits are ephemeral (in-memory; not persisted). 'Reset to original'
    restores the agent's labor estimate.
    """
    from app.services.pricing import (
        compute_custom_scenario_package,
        get_pricing_rules,
        reconstruct_cost_analyst_output,
    )

    output = reconstruct_cost_analyst_output(packages)
    if output is None or not output.labor_lines:
        _empty_state(
            "No persisted cost build to drive the slider. Run the "
            "Cost Analyst first; the slider lets you explore "
            "scenarios between the policy LOW and HIGH stops.",
            icon="tune",
        )
        return

    # Pull market band from any package (they all share the same one).
    band_low = None
    band_mid = None
    band_high = None
    for p in packages:
        pnl = p.get("pnl_projection_json") or {}
        if pnl.get("vs_market_band_low_usd") is not None:
            band_low = pnl.get("vs_market_band_low_usd")
            band_mid = pnl.get("vs_market_band_mid_usd")
            band_high = pnl.get("vs_market_band_high_usd")
            break

    # Slider + per-line edit state. Edits are keyed by labor_category
    # since that's the natural identity (LLM doesn't produce IDs).
    get_pricing_rules()
    # Salary dropdown — 5K-increment band keys from $85K to $230K.
    # Values not in the documented wage_bands fall through to the
    # wrap_rate_formula path (computed identically).
    wage_band_options = {f"{w}k": f"${w}K" for w in range(85, 235, 5)}

    # Snapshot the original ODCs as plain dicts so reset can restore
    # them and the editable inputs have a starting state.
    original_odcs_persisted = [
        {
            "item": o.item,
            "amount_usd": float(o.amount_usd),
            "justification": o.justification,
            "year_count": int(o.year_count or 1),
        }
        for o in output.odcs
    ]

    state = {
        "slider_val": float(_DEFAULT_SLIDER_POS),  # = MEDIUM stop
        "coverage_level": "high",
        # edits[category] = {"wage_band": str, "hours": float,
        #                    "loaded_override": float|None,
        #                    "billed_override": float|None}
        "edits": {},
        # odc_edits is the live ODC list — starts as a copy of the
        # original ODCs and is mutated as the user edits / adds /
        # removes rows. Reset puts this back to original_odcs.
        "odc_edits": [dict(o) for o in original_odcs_persisted],
    }

    # Header card with slider + coverage controls.
    with ui.card().classes("w-full mt-2 bg-slate-50"):
        ui.label("Custom bid posture").classes("text-base font-medium")
        ui.label(
            "Drag the slider to interpolate between the LOW (competitive) "
            "and HIGH (protective) policy postures. MEDIUM (target) sits "
            "at the midpoint. Coverage is a categorical hire assumption "
            "— pick low when bidding lean against single-coverage hires; "
            "high otherwise. Editable labor lines below let you fine-tune "
            "the estimate within the slider-set budget."
        ).classes("text-xs opacity-70 pb-2")

        margin_lbl = ui.label("").classes("text-sm font-medium font-mono")
        contingency_lbl = ui.label("").classes("text-xs opacity-80 font-mono")

        with ui.row().classes("items-center gap-3 w-full pt-1 flex-wrap"):
            with ui.column().classes("flex-1 min-w-[320px] gap-0"):
                # Stops above the slider — absolutely positioned at
                # their slider %s so the labels align with the actual
                # ticks. The 5% floor sits at slider 0; LOW at 52;
                # MEDIUM at 80; HIGH at 100.
                with ui.element("div").classes("relative w-full text-xs opacity-70").style("height: 1.4em;"):
                    ui.label("Aggressive 5%").classes("absolute font-medium").style(
                        "left: 0%; transform: translateX(0%);"
                    )
                    ui.label("LOW 18%").classes("absolute font-medium").style(
                        f"left: {_LOW_SLIDER_POS}%; transform: translateX(-50%);"
                    )
                    ui.label("MEDIUM 25%").classes("absolute font-medium").style(
                        f"left: {_MEDIUM_SLIDER_POS}%; transform: translateX(-50%);"
                    )
                    ui.label("HIGH 30%").classes("absolute font-medium").style(
                        "left: 100%; transform: translateX(-100%);"
                    )
                slider = ui.slider(
                    min=0,
                    max=100,
                    value=_DEFAULT_SLIDER_POS,
                    step=1,
                ).props("label-always markers")

    @ui.refreshable
    def render_custom() -> None:
        margin_pct, contingency_pct = _slider_to_params(
            state["slider_val"],
        )
        margin_lbl.set_text(f"Margin: {margin_pct:.1%}    Contingency: {contingency_pct:.1%}")
        contingency_lbl.set_text(f"slider {state['slider_val']:.0f}/100 · coverage={state['coverage_level']}")

        # ORIGINAL output (no edits) → target package = the budget.
        try:
            target_pkg = compute_custom_scenario_package(
                output=output,
                coverage_level=state["coverage_level"],
                margin_pct=margin_pct,
                contingency_pct=contingency_pct,
                market_band_low_usd=band_low,
                market_band_mid_usd=band_mid,
                market_band_high_usd=band_high,
                scenario_label="TARGET",
            )
        except Exception as exc:
            ui.label(f"Target compute failed: {exc}").classes("text-sm text-red-600")
            return

        # EDITED output → actual package = current cost build.
        edited_output = _apply_edits_to_output(
            output,
            state["edits"],
            state["odc_edits"],
        )
        try:
            actual_pkg = compute_custom_scenario_package(
                output=edited_output,
                coverage_level=state["coverage_level"],
                margin_pct=margin_pct,
                contingency_pct=contingency_pct,
                market_band_low_usd=band_low,
                market_band_mid_usd=band_mid,
                market_band_high_usd=band_high,
                scenario_label="CUSTOM",
            )
        except Exception as exc:
            ui.label(f"Custom compute failed: {exc}").classes("text-sm text-red-600")
            return

        # Budget bar — green/yellow/red based on actual vs target.
        # n_edits is a rough indicator that something has been changed
        # vs the original (labor edits OR ODC list mismatch).
        odcs_changed = state["odc_edits"] != original_odcs_persisted
        n_edits = sum(1 for _ in state["edits"].values()) + (1 if odcs_changed else 0)
        _render_budget_bar(
            target_price=target_pkg.total_proposed_price_usd,
            actual_price=actual_pkg.total_proposed_price_usd,
            n_edits=n_edits,
            on_reset=_make_reset_handler(
                state,
                original_odcs_persisted,
                render_custom.refresh,
            ),
        )

        # Aggregate metric strip (from ACTUAL).
        actual_snap = _computed_package_to_snapshot_dict(actual_pkg)
        indirect = actual_snap.get("indirect_costs_json") or {}
        pnl = actual_snap.get("pnl_projection_json") or {}
        with ui.row().classes("w-full gap-4 flex-wrap pt-2"):
            _render_metric(
                "Proposed price",
                f"${actual_pkg.total_proposed_price_usd:,.0f}",
            )
            _render_metric(
                "Subtotal cost",
                f"${float(indirect.get('total_subtotal_cost_usd') or 0):,.0f}",
            )
            # Effective margin = profit / price. When the user has
            # set per-line Billed $/hr overrides, this diverges from
            # the scenario's nominal margin_pct; show the effective
            # number so the metric reflects what's actually being bid.
            eff_pct = float(
                indirect.get("effective_profit_pct")
                if indirect.get("effective_profit_pct") is not None
                else (indirect.get("profit_pct") or 0)
            )
            nominal_pct = float(indirect.get("profit_pct") or 0)
            margin_subtitle = f"{eff_pct:.1%} effective"
            if abs(eff_pct - nominal_pct) > 0.001:
                # User has overridden line rates — surface the gap.
                margin_subtitle = f"{eff_pct:.1%} effective (nominal {nominal_pct:.1%})"
            _render_metric(
                "Profit",
                f"${float(indirect.get('profit_usd') or 0):,.0f}",
                subtitle=margin_subtitle,
            )
            _render_metric(
                "Blended billing rate",
                f"${float(pnl.get('blended_hourly_rate') or 0):,.2f}/hr",
                subtitle=(f"{float(pnl.get('total_billable_hours') or 0):,.0f} billable hrs"),
            )

        # Editable labor lines.
        ui.label(f"Labor lines ({len(actual_pkg.lines)}) — editable").classes("text-sm font-medium pt-3")
        _render_editable_labor_lines(
            output=output,
            actual_lines=actual_pkg.lines,
            state=state,
            wage_band_options=wage_band_options,
            on_change=render_custom.refresh,
        )

        # Indirect, ODCs, phases, recommendation, exec summary —
        # reuse the standard renderers from actual_snap.
        with ui.row().classes("w-full gap-3 pt-3 flex-wrap"):
            with ui.card().classes("flex-1 min-w-[260px]"):
                ui.label("Indirect costs").classes("text-sm font-medium pb-1")
                _render_kv(
                    "G&A hourly add-on",
                    f"${float(indirect.get('ga_hourly_addon_usd') or 0):.2f}/hr",
                )
                _render_kv(
                    "G&A total",
                    f"${float(indirect.get('ga_total_usd') or 0):,.2f}",
                )
                _render_kv(
                    "Contingency hours",
                    f"{float(indirect.get('contingency_hours') or 0):,.1f} hrs",
                )
                _render_kv(
                    "Contingency cost",
                    f"${float(indirect.get('contingency_cost_usd') or 0):,.2f}",
                )

            _render_editable_odcs_card(state, render_custom.refresh)

        # Lifecycle phases (recomputed against edited lines).
        _render_lifecycle_phases(actual_snap)

        # Recommendation + executive summary.
        rec_rationale = (actual_snap.get("recommendation_rationale") or "").strip()
        if rec_rationale:
            with ui.card().classes("w-full bg-slate-50 mt-3"):
                ui.label("Recommendation rationale").classes("text-xs font-medium uppercase opacity-70")
                ui.label(rec_rationale).classes("text-sm")

    # Wire change handlers AFTER render_custom is defined so the
    # closure captures the refresh function. e.sender.value is read
    # off the widget directly so the same handler works whether the
    # event is a raw Quasar event (.on('change', ...)) or NiceGUI's
    # high-level on_value_change.
    def _on_slider_change(e) -> None:
        state["slider_val"] = float(e.sender.value)
        render_custom.refresh()

    # Slider — bind to Quasar's 'change' event (fires on release)
    # rather than 'update:model-value' (fires per-pixel during drag).
    # The Custom tab's panel rebuild is heavy (editable labor inputs,
    # editable ODCs, phase cards, recommendation), so per-pixel
    # re-renders even at 0.15s throttle felt sluggish. Release-only
    # gives a snappy single re-render once the user lands the thumb.
    # The slider's visual position still updates smoothly mid-drag —
    # Quasar handles that internally without needing Python events.
    slider.on("change", _on_slider_change)

    render_custom()


def _apply_edits_to_output(output, labor_edits: dict, odc_edits: list | None):
    """Return a new CostAnalystOutput with labor + ODC edits applied.

    `labor_edits` is keyed by labor_category and may set wage_band /
    hours / loaded_override / billed_override; lines without edits
    pass through unchanged.

    `odc_edits` is the full live ODC list (possibly add/removed); when
    None, the original output.odcs is preserved verbatim. When a list,
    each entry is a dict with item / amount_usd / justification /
    year_count and replaces the original ODC set entirely.
    """
    from app.services.pricing import (
        CostAnalystLaborLine,
        CostAnalystOdc,
        CostAnalystOutput,
    )

    new_lines: list[CostAnalystLaborLine] = []
    for ll in output.labor_lines:
        ed = labor_edits.get(ll.labor_category) or {}
        new_lines.append(
            CostAnalystLaborLine(
                labor_category=ll.labor_category,
                wage_band=str(ed.get("wage_band") or ll.wage_band),
                hours=float(ed.get("hours") if ed.get("hours") is not None else ll.hours),
                rationale=ll.rationale,
                loaded_hourly_override_usd=ed.get("loaded_override"),
                billed_hourly_override_usd=ed.get("billed_override"),
            )
        )
    if odc_edits is None:
        new_odcs = list(output.odcs)
    else:
        new_odcs = []
        for o in odc_edits:
            try:
                year_count = int(o.get("year_count") or 1)
            except (TypeError, ValueError):
                year_count = 1
            try:
                amount = float(o.get("amount_usd") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            new_odcs.append(
                CostAnalystOdc(
                    item=str(o.get("item") or ""),
                    amount_usd=amount,
                    justification=str(o.get("justification") or ""),
                    year_count=max(1, year_count),
                )
            )
    return CostAnalystOutput(
        labor_lines=new_lines,
        avg_headcount_during_pop=output.avg_headcount_during_pop,
        odcs=new_odcs,
        subcontractor_costs_usd=output.subcontractor_costs_usd,
        key_risks=output.key_risks,
        executive_summary=output.executive_summary,
        lifecycle_phases=output.lifecycle_phases,
    )


def _make_reset_handler(
    state: dict,
    original_odcs: list,
    refresh,
):
    """Closure factory — clears all labor edits AND restores ODCs to
    the original agent-produced list, then triggers a re-render.
    Bound to the 'Reset' button on the budget tracker."""

    def _reset() -> None:
        state["edits"] = {}
        state["odc_edits"] = [dict(o) for o in original_odcs]
        refresh()
        ui.notify(
            "Reset labor and ODC edits to the original agent estimate.",
            type="info",
            timeout=3000,
        )

    return _reset


def _render_budget_bar(
    *,
    target_price: float,
    actual_price: float,
    n_edits: int,
    on_reset,
) -> None:
    """Visual budget bar — green/yellow/red based on actual vs target.
    Always shows headroom or overage in $ terms so the user knows
    exactly how much room they have to keep editing."""
    target_price = max(target_price, 0.01)  # avoid div-by-zero
    pct = (actual_price / target_price) * 100.0

    if pct <= 95.0:
        # Comfortable — green.
        bar_color = "bg-emerald-400"
        track_bg = "bg-emerald-50"
        text_color = "text-emerald-700"
        delta_label = "headroom"
        delta = target_price - actual_price
        delta_str = f"${delta:,.0f}"
    elif pct <= 100.0:
        # Close to budget — yellow.
        bar_color = "bg-amber-400"
        track_bg = "bg-amber-50"
        text_color = "text-amber-700"
        delta_label = "headroom"
        delta = target_price - actual_price
        delta_str = f"${delta:,.0f}"
    else:
        # Over budget — red.
        bar_color = "bg-red-500"
        track_bg = "bg-red-50"
        text_color = "text-red-700"
        delta_label = "OVER BUDGET BY"
        delta = actual_price - target_price
        delta_str = f"${delta:,.0f}"

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full flex-wrap gap-2"):
            with ui.column().classes("gap-0"):
                ui.label("Budget tracker").classes("text-xs opacity-70 uppercase")
                ui.label(f"Target ${target_price:,.0f}    ·    Actual ${actual_price:,.0f}").classes(
                    "text-sm font-mono font-medium"
                )
            with ui.column().classes("gap-0 items-end"):
                ui.label(delta_label).classes(f"text-xs uppercase {text_color}")
                ui.label(delta_str).classes(f"text-base font-mono font-semibold {text_color}")
            if n_edits > 0:
                ui.button(
                    f"Reset {n_edits} edit(s)",
                    icon="undo",
                    on_click=on_reset,
                ).props("flat dense color=primary")

        # Bar — clamp visual to 110% so over-budget shows the overage
        # but doesn't blow the layout.
        bar_pct = min(pct, 110.0)
        with ui.row().classes(f"w-full h-3 rounded {track_bg} overflow-hidden relative mt-1"):
            ui.element("div").classes(f"h-full {bar_color}").style(
                f"width: {bar_pct:.1f}%; transition: width 0.25s ease-out;"
            )
        ui.label(f"{pct:.1f}% of target").classes("text-xs opacity-60 pt-1")


def _render_editable_labor_lines(
    *,
    output,
    actual_lines: list,
    state: dict,
    wage_band_options: dict,
    on_change,
) -> None:
    """Editable labor-lines view — one row per labor category with
    inputs for Salary / Hours / Loaded $/hr / Billed $/hr. Coverage,
    Billed total, and Rationale stay read-only.

    Each input has its own on_change handler that updates the edits
    dict and triggers re-render. The displayed rate values reflect
    the CURRENT computed values (including any overrides), so the
    user sees the cascade as they type."""
    # Header row.
    with ui.row().classes(
        "w-full items-center gap-2 px-2 py-1 text-xs font-medium "
        "uppercase opacity-70 border-b border-slate-200"
    ):
        ui.label("Category").classes("w-44")
        ui.label("Salary").classes("w-24")
        ui.label("Coverage").classes("w-20")
        ui.label("Hours").classes("w-24 text-right")
        ui.label("Loaded $/hr").classes("w-28 text-right")
        ui.label("Billed $/hr").classes("w-28 text-right")
        ui.label("Billed total").classes("w-32 text-right")
        ui.label("Rationale").classes("flex-1")

    actual_by_cat = {ln.labor_category: ln for ln in actual_lines}
    for orig_ll in output.labor_lines:
        cat = orig_ll.labor_category
        actual_ln = actual_by_cat.get(cat)
        if actual_ln is None:
            continue
        ed = state["edits"].get(cat, {})
        current_band = str(ed.get("wage_band") or orig_ll.wage_band)
        current_hours = float(ed["hours"] if ed.get("hours") is not None else orig_ll.hours)
        current_loaded = float(actual_ln.loaded_hourly_rate_usd)
        current_billed = float(actual_ln.proposed_billing_rate_usd)
        current_billed_total = float(actual_ln.billed_total_usd)

        with ui.row().classes("w-full items-center gap-2 px-2 py-1 border-b border-slate-100"):
            ui.label(cat).classes("w-44 text-sm truncate").tooltip(cat)

            # Salary dropdown — editable. Options are 5K-increment
            # bands from $85K to $230K. Bands not in the documented
            # JSON compute via the wrap_rate_formula at math time.
            salary_select = (
                ui.select(
                    options=wage_band_options,
                    value=current_band,
                )
                .props("dense outlined options-dense")
                .classes("w-28")
            )

            ui.label(actual_ln.coverage_level).classes("w-20 text-xs opacity-70")

            # Hours number — editable.
            hours_input = (
                ui.number(
                    value=current_hours,
                    min=0,
                    step=1,
                    format="%.0f",
                )
                .props("dense outlined")
                .classes("w-24")
            )

            # Loaded $/hr — editable.
            loaded_input = (
                ui.number(
                    value=current_loaded,
                    min=0,
                    step=0.01,
                    format="%.2f",
                )
                .props("dense outlined prefix=$")
                .classes("w-28")
            )

            # Billed $/hr — editable.
            billed_input = (
                ui.number(
                    value=current_billed,
                    min=0,
                    step=0.01,
                    format="%.2f",
                )
                .props("dense outlined prefix=$")
                .classes("w-28")
            )

            ui.label(f"${current_billed_total:,.0f}").classes("w-32 text-right text-sm font-mono")
            rationale = (actual_ln.rationale or "").strip()
            ui.label(rationale[:120] + ("…" if len(rationale) > 120 else "")).classes(
                "flex-1 text-xs opacity-70 truncate"
            ).tooltip(rationale)

        # Bind change handlers AFTER widgets exist so the closure captures
        # the right `cat` (default-arg pattern locks the variable).
        # e.sender.value reads the current widget value, which works for
        # both NiceGUI's high-level on_value_change events (selectivity)
        # and the raw 'change' event (fires on blur for numbers).
        def _on_salary(e, cat=cat) -> None:
            edits = state["edits"].setdefault(cat, {})
            edits["wage_band"] = str(e.sender.value)
            # Clear loaded override so the new salary's wrap rate
            # takes effect — keeps the cascade intuitive.
            edits["loaded_override"] = None
            on_change()

        def _on_hours(e, cat=cat) -> None:
            try:
                v = float(e.sender.value) if e.sender.value is not None else 0.0
            except (TypeError, ValueError):
                return
            edits = state["edits"].setdefault(cat, {})
            edits["hours"] = max(0.0, v)
            on_change()

        def _on_loaded(e, cat=cat) -> None:
            try:
                v = float(e.sender.value) if e.sender.value is not None else 0.0
            except (TypeError, ValueError):
                return
            edits = state["edits"].setdefault(cat, {})
            edits["loaded_override"] = max(0.0, v)
            on_change()

        def _on_billed(e, cat=cat) -> None:
            try:
                v = float(e.sender.value) if e.sender.value is not None else 0.0
            except (TypeError, ValueError):
                return
            edits = state["edits"].setdefault(cat, {})
            edits["billed_override"] = max(0.0, v)
            on_change()

        # Salary dropdown — atomic event, fire on selection.
        salary_select.on_value_change(_on_salary)
        # Numeric inputs — bind to 'change' (Quasar fires on blur or
        # Enter) instead of on_value_change (per-keystroke). Avoids
        # spamming re-renders mid-typing on a multi-digit edit.
        hours_input.on("change", _on_hours)
        loaded_input.on("change", _on_loaded)
        billed_input.on("change", _on_billed)


def _computed_package_to_snapshot_dict(pkg) -> dict:
    """Convert a ComputedScenarioPackage dataclass into the dict
    shape get_pricing_packages_snapshot returns, so the same
    _render_scenario_detail helper renders both persisted and
    custom-computed scenarios identically.

    The lines' rationale carries any ceiling-violation note
    appended via the same WARNING: prefix the persistence layer
    uses, so the warning surfaces in the Custom tab the same way
    it does in the policy tabs."""
    lines = []
    for ln in pkg.lines:
        rationale = ln.rationale or ""
        if ln.ceiling_violation_note:
            rationale = (
                f"{rationale}\n\nWARNING: {ln.ceiling_violation_note}"
                if rationale.strip()
                else f"WARNING: {ln.ceiling_violation_note}"
            )
        lines.append(
            {
                "labor_category": ln.labor_category,
                "wage_band": ln.wage_band,
                "coverage_level": ln.coverage_level,
                "hours": ln.hours,
                "loaded_hourly_rate_usd": ln.loaded_hourly_rate_usd,
                "loaded_cost_usd": ln.loaded_cost_usd,
                "ga_allocation_usd": ln.ga_allocation_usd,
                "proposed_billing_rate_usd": ln.proposed_billing_rate_usd,
                "billed_total_usd": ln.billed_total_usd,
                "profit_per_hour_usd": ln.profit_per_hour_usd,
                "rationale": rationale,
            }
        )
    return {
        "id": None,
        "scenario": pkg.scenario,
        "indirect_costs_json": pkg.indirect_costs,
        "pnl_projection_json": pkg.pnl_projection,
        "lines": lines,
        "odcs_json": pkg.odcs_persisted,
        "subcontractor_costs": pkg.subcontractor_costs_usd,
        "total_proposed_price": pkg.total_proposed_price_usd,
        "vs_market_position": pkg.vs_market_position,
        "bid_recommendation": pkg.bid_recommendation,
        "recommendation_rationale": pkg.recommendation_rationale,
        "phase_breakdown_json": pkg.phases,
        "loaded_labor_cost": pkg.total_loaded_labor_cost_usd,
    }


def _render_scenario_detail(pkg: dict) -> None:
    """All the detail for one scenario: aggregates, labor table,
    ODCs, indirect breakdown, executive summary."""
    indirect = pkg.get("indirect_costs_json") or {}
    pnl = pkg.get("pnl_projection_json") or {}

    # Aggregate strip — price, cost, profit, blended billing rate.
    with ui.row().classes("w-full gap-4 flex-wrap pt-2"):
        _render_metric(
            "Proposed price",
            f"${float(pkg.get('total_proposed_price') or 0):,.0f}",
        )
        _render_metric(
            "Subtotal cost",
            f"${float(indirect.get('total_subtotal_cost_usd') or 0):,.0f}",
        )
        _render_metric(
            "Profit",
            f"${float(indirect.get('profit_usd') or 0):,.0f}",
            subtitle=f"{(indirect.get('profit_pct') or 0):.0%} margin",
        )
        _render_metric(
            "Blended billing rate",
            f"${float(pnl.get('blended_hourly_rate') or 0):,.2f}/hr",
            subtitle=f"{float(pnl.get('total_billable_hours') or 0):,.0f} billable hrs",
        )

    # Labor lines table.
    lines = pkg.get("lines") or []
    if lines:
        ui.label(f"Labor lines ({len(lines)})").classes("text-sm font-medium pt-3")
        _render_labor_lines_table(lines)

    # Indirect costs breakdown.
    with ui.row().classes("w-full gap-3 pt-3 flex-wrap"):
        with ui.card().classes("flex-1 min-w-[260px]"):
            ui.label("Indirect costs").classes("text-sm font-medium pb-1")
            _render_kv("G&A hourly add-on", f"${float(indirect.get('ga_hourly_addon_usd') or 0):.2f}/hr")
            _render_kv("G&A total", f"${float(indirect.get('ga_total_usd') or 0):,.2f}")
            _render_kv("Contingency hours", f"{float(indirect.get('contingency_hours') or 0):,.1f} hrs")
            _render_kv("Contingency cost", f"${float(indirect.get('contingency_cost_usd') or 0):,.2f}")

        # ODCs panel.
        odcs = pkg.get("odcs_json") or []
        with ui.card().classes("flex-1 min-w-[260px]"):
            ui.label("Other Direct Costs (ODCs)").classes("text-sm font-medium pb-1")
            if not odcs:
                ui.label("None proposed.").classes("text-xs opacity-60")
            else:
                total = 0.0
                for o in odcs:
                    amt = float(o.get("amount_usd") or 0)
                    total += amt
                    with ui.row().classes("items-start justify-between w-full gap-2 pt-1"):
                        with ui.column().classes("gap-0 flex-1"):
                            ui.label(o.get("item") or "—").classes("text-sm")
                            just = (o.get("justification") or "").strip()
                            if just:
                                ui.label(just).classes("text-xs opacity-60")
                        ui.label(f"${amt:,.2f}").classes("text-sm font-mono")
                with ui.row().classes(
                    "items-center justify-between w-full gap-2 pt-2 border-t border-slate-200"
                ):
                    ui.label("Total ODCs").classes("text-xs font-medium")
                    ui.label(f"${total:,.2f}").classes("text-sm font-mono font-medium")

    # Subcontractor passthrough.
    sub_costs = pkg.get("subcontractor_costs")
    if sub_costs not in (None, 0, 0.0):
        with ui.row().classes("items-center gap-2 pt-2"):
            ui.label("Subcontractor passthrough:").classes("text-sm font-medium")
            ui.label(f"${float(sub_costs):,.2f}").classes("text-sm font-mono")

    # Lifecycle phase breakdown — itemized BOE by phase. Shown
    # before the rationale so the user sees the WBS-style cost view
    # right after the labor table.
    _render_lifecycle_phases(pkg)

    # Recommendation rationale, surfaced inline so user understands
    # why this scenario got its bid/walk_away verdict.
    rec_rationale = (pkg.get("recommendation_rationale") or "").strip()
    if rec_rationale:
        with ui.card().classes("w-full bg-slate-50 mt-3"):
            ui.label("Recommendation rationale").classes("text-xs font-medium uppercase opacity-70")
            ui.label(rec_rationale).classes("text-sm")

    # Executive summary in collapsible.
    exec_summary = (pnl.get("executive_summary") or "").strip()
    if exec_summary:
        with ui.expansion(
            "Executive summary (Cost Analyst narrative)",
            icon="article",
            value=False,
        ).classes("w-full pt-2"):
            ui.label(exec_summary).classes("text-sm whitespace-pre-wrap opacity-90")


def _render_metric(label: str, value: str, subtitle: str = "") -> None:
    with ui.column().classes("gap-0 flex-1 min-w-[160px]"):
        ui.label(label).classes("text-xs opacity-60 uppercase")
        ui.label(value).classes("text-xl font-semibold")
        if subtitle:
            ui.label(subtitle).classes("text-xs opacity-60")


def _render_kv(label: str, value: str) -> None:
    with ui.row().classes("items-center justify-between w-full pt-1"):
        ui.label(label).classes("text-sm opacity-80")
        ui.label(value).classes("text-sm font-mono")


def _render_labor_lines_table(lines: list[dict]) -> None:
    """Per-FTE labor table. Ceiling-violation warnings render as a
    yellow icon next to the category cell."""
    columns = [
        {"name": "category", "label": "Category", "field": "category", "align": "left"},
        {"name": "wage_band", "label": "Salary", "field": "wage_band"},
        {"name": "coverage", "label": "Coverage", "field": "coverage"},
        {"name": "hours", "label": "Hrs", "field": "hours", "align": "right"},
        {"name": "loaded_rate", "label": "Loaded $/hr", "field": "loaded_rate", "align": "right"},
        {"name": "billed_rate", "label": "Billed $/hr", "field": "billed_rate", "align": "right"},
        {"name": "billed_total", "label": "Billed total", "field": "billed_total", "align": "right"},
        {"name": "rationale", "label": "Rationale", "field": "rationale", "align": "left"},
    ]
    rows: list[dict] = []
    for ln in lines:
        rationale = (ln.get("rationale") or "").strip()
        warning = "WARNING:" in rationale
        rows.append(
            {
                "category": (f"{'⚠ ' if warning else ''}{ln.get('labor_category') or '—'}"),
                "wage_band": ln.get("wage_band") or "—",
                "coverage": ln.get("coverage_level") or "—",
                "hours": f"{float(ln.get('hours') or 0):,.0f}",
                "loaded_rate": f"${float(ln.get('loaded_hourly_rate_usd') or 0):.2f}",
                "billed_rate": f"${float(ln.get('proposed_billing_rate_usd') or 0):.2f}",
                "billed_total": f"${float(ln.get('billed_total_usd') or 0):,.0f}",
                "rationale": rationale[:240] + ("…" if len(rationale) > 240 else ""),
            }
        )
    ui.table(
        columns=columns,
        rows=rows,
        row_key="category",
        pagination=20,
    ).classes("w-full")


def _render_editable_odcs_card(state: dict, on_change) -> None:
    """Editable Other Direct Costs card. Each row has item, annual
    amount, year_count, and justification inputs plus a delete button.
    'Add ODC' button at the top inserts a new blank row.

    For the cost build, each ODC contributes amount × year_count to
    the project total. Recurring items (cloud hosting, SaaS licenses,
    pen tests) use year_count > 1; one-time spend stays at 1. The
    total row breaks out the multi-year items so the PoP-extension
    math is visible (e.g., '$60K/yr × 3yr = $180K')."""
    with ui.card().classes("flex-1 min-w-[420px]"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Other Direct Costs (ODCs)").classes("text-sm font-medium")
            ui.button(
                "Add ODC",
                icon="add",
                on_click=lambda: _odc_add(state, on_change),
            ).props("flat dense color=primary")
        ui.label(
            "Annual amount × year count = full-PoP contribution. Use "
            "year count for recurring items (hosting, licenses); 1 "
            "for one-time spend."
        ).classes("text-xs opacity-60 pb-1")

        odcs = state.get("odc_edits") or []
        if not odcs:
            ui.label("None proposed. Click 'Add ODC' above to add a line.").classes("text-xs opacity-60 pt-1")
            return

        for i, odc in enumerate(odcs):
            _render_one_editable_odc(state, i, odc, on_change)

        # Total row — extended (amount × year_count) summed across rows.
        total = sum(float(o.get("amount_usd") or 0) * max(1, int(o.get("year_count") or 1)) for o in odcs)
        any_multi_year = any(int(o.get("year_count") or 1) > 1 for o in odcs)
        with ui.row().classes("items-center justify-between w-full gap-2 pt-2 border-t border-slate-200"):
            ui.label("Total ODCs").classes("text-xs font-medium")
            ui.label(f"${total:,.2f}").classes("text-sm font-mono font-medium")
        # When any item recurs, surface the per-year breakdown so the
        # multi-year math is visible to the reader.
        if any_multi_year:
            with ui.column().classes("gap-0 pt-1"):
                for o in odcs:
                    yr = int(o.get("year_count") or 1)
                    if yr <= 1:
                        continue
                    amt = float(o.get("amount_usd") or 0)
                    item = (o.get("item") or "?").strip()[:50] or "?"
                    ui.label(f"   {item}: ${amt:,.0f}/yr × {yr}yr = ${amt * yr:,.0f}").classes(
                        "text-xs font-mono opacity-70"
                    )


def _render_one_editable_odc(
    state: dict,
    idx: int,
    odc: dict,
    on_change,
) -> None:
    """One editable ODC row. Top row: item + amount + year_count +
    delete. Below: justification input."""
    with ui.row().classes("items-center gap-2 w-full pt-2"):
        item_input = (
            ui.input(
                value=odc.get("item") or "",
                placeholder="Item (e.g., Cloud hosting)",
            )
            .props("dense outlined")
            .classes("flex-1 min-w-[160px]")
        )
        amt_input = (
            ui.number(
                value=float(odc.get("amount_usd") or 0),
                min=0,
                step=100,
                format="%.2f",
            )
            .props("dense outlined prefix=$")
            .classes("w-32")
        )
        year_input = (
            ui.number(
                value=int(odc.get("year_count") or 1),
                min=1,
                step=1,
                format="%.0f",
            )
            .props("dense outlined suffix=yr")
            .classes("w-24")
        )
        ui.button(
            icon="close",
            on_click=lambda: _odc_delete(state, idx, on_change),
        ).props("flat dense round color=negative").tooltip("Remove this ODC")
    just_input = (
        ui.input(
            value=odc.get("justification") or "",
            placeholder="Justification (cited in cost narrative)",
        )
        .props("dense outlined")
        .classes("w-full")
    )

    # Bind handlers AFTER widget creation so the closure captures
    # the right idx (default-arg pattern locks the variable). Use
    # 'change' event so the handler fires on blur/Enter, not per
    # keystroke — avoids spammy re-renders on long item / justification
    # text edits.
    def _on_item(e, idx=idx) -> None:
        state["odc_edits"][idx]["item"] = str(e.sender.value or "")
        on_change()

    def _on_amount(e, idx=idx) -> None:
        try:
            v = float(e.sender.value) if e.sender.value is not None else 0.0
        except (TypeError, ValueError):
            return
        state["odc_edits"][idx]["amount_usd"] = max(0.0, v)
        on_change()

    def _on_year(e, idx=idx) -> None:
        try:
            v = int(float(e.sender.value)) if e.sender.value is not None else 1
        except (TypeError, ValueError):
            return
        state["odc_edits"][idx]["year_count"] = max(1, v)
        on_change()

    def _on_just(e, idx=idx) -> None:
        state["odc_edits"][idx]["justification"] = str(e.sender.value or "")
        on_change()

    item_input.on("change", _on_item)
    amt_input.on("change", _on_amount)
    year_input.on("change", _on_year)
    just_input.on("change", _on_just)


def _odc_add(state: dict, on_change) -> None:
    """Append a new blank ODC row to state and trigger re-render."""
    state.setdefault("odc_edits", []).append(
        {
            "item": "",
            "amount_usd": 0.0,
            "justification": "",
            "year_count": 1,
        }
    )
    on_change()


def _odc_delete(state: dict, idx: int, on_change) -> None:
    """Remove the ODC at idx and trigger re-render."""
    odcs = state.get("odc_edits") or []
    if 0 <= idx < len(odcs):
        odcs.pop(idx)
        on_change()


def _render_lifecycle_phases(pkg: dict) -> None:
    """Itemized cost-by-phase breakdown for ONE scenario. Each phase
    is its own card with header (name + duration + price) plus a
    per-labor-category allocation table. Federal cost-proposal
    convention — Basis of Estimate by phase, not just by labor
    category.

    Hidden when the package has no phase data — shows an inline
    notice prompting the user to re-run the Cost Analyst.
    """
    phases = pkg.get("phase_breakdown_json") or []
    real_phases = [ph for ph in phases if not ph.get("_synthetic_summary")]
    summary = next(
        (ph for ph in phases if ph.get("_synthetic_summary")),
        None,
    )

    ui.label("Lifecycle phases (Basis of Estimate)").classes("text-sm font-medium pt-3")

    if not real_phases:
        with ui.card().classes("w-full bg-slate-50"):
            ui.label(
                "No phase breakdown for this scenario. Re-run the "
                "Cost Analyst — the agent now produces lifecycle "
                "phase allocations alongside labor lines."
            ).classes("text-sm opacity-70")
        return

    # Allocation balance warnings (over- or under-allocated hours).
    # Surface above the phase cards so the user sees them before
    # drilling into detail.
    if summary:
        warnings = (summary.get("over_allocations") or []) + (summary.get("under_allocations") or [])
        if warnings:
            with ui.card().classes("w-full bg-amber-50 border border-amber-300"):
                ui.label("Phase allocation notes").classes("text-sm font-medium pb-1")
                for w in warnings:
                    ui.label(f"• {w}").classes("text-xs opacity-80")

    # Aggregate phase totals so the user can sanity-check them
    # against the scenario total at the top of the tab.
    phase_total_price = sum(float(ph.get("phase_price_usd") or 0) for ph in real_phases)
    phase_total_hours = sum(float(ph.get("phase_total_hours") or 0) for ph in real_phases)
    with ui.row().classes("items-center gap-3 pt-2 pb-2 flex-wrap px-2 rounded bg-slate-50"):
        ui.label(f"{len(real_phases)} phases · {phase_total_hours:,.0f} hrs allocated").classes(
            "text-xs font-medium"
        )
        ui.element("div").classes("flex-1")
        ui.label("Sum of phase prices: ").classes("text-xs opacity-70")
        ui.label(f"${phase_total_price:,.0f}").classes("text-xs font-mono font-medium")

    # One card per phase.
    for ph in real_phases:
        _render_one_phase_card(ph)


def _render_one_phase_card(phase: dict) -> None:
    """One phase: header strip + collapsible labor allocation table."""
    import math

    name = phase.get("name") or "Phase"
    description = (phase.get("description") or "").strip()
    start_m = int(phase.get("start_month") or 1)
    duration = float(phase.get("duration_months") or 0)
    # End-month label: ceil so a 1.5-month phase starting at M1 reads
    # as "M1-M2" (covers half of M2), not "M1-M1" (truncated).
    end_m_exact = start_m + duration - 1
    end_m = max(start_m, math.ceil(end_m_exact))

    hours = float(phase.get("phase_total_hours") or 0)
    loaded = float(phase.get("phase_loaded_cost_usd") or 0)
    ga = float(phase.get("phase_ga_usd") or 0)
    cont = float(phase.get("phase_contingency_cost_usd") or 0)
    subtotal = float(phase.get("phase_subtotal_cost_usd") or 0)
    profit = float(phase.get("phase_profit_usd") or 0)
    price = float(phase.get("phase_price_usd") or 0)

    with ui.card().classes("w-full"):
        # Header strip.
        with ui.row().classes("items-center justify-between w-full flex-wrap gap-3"):
            with ui.column().classes("gap-0 flex-1 min-w-[260px]"):
                ui.label(name).classes("text-base font-medium")
                if description:
                    ui.label(description).classes("text-xs opacity-70")
            with ui.column().classes("gap-0"):
                ui.label("Months").classes("text-xs opacity-60 uppercase")
                # Single-month phases render as "M3 (1mo)"; multi-month
                # phases render as "M3-M6 (4mo)". Cleaner than always
                # showing the range form.
                month_str = f"M{start_m}" if start_m == end_m else f"M{start_m}-M{end_m}"
                ui.label(f"{month_str} ({duration:g}mo)").classes("text-sm font-medium")
            with ui.column().classes("gap-0"):
                ui.label("Hours").classes("text-xs opacity-60 uppercase")
                ui.label(f"{hours:,.0f}").classes("text-sm font-medium font-mono")
            with ui.column().classes("gap-0"):
                ui.label("Cost").classes("text-xs opacity-60 uppercase")
                ui.label(f"${subtotal:,.0f}").classes("text-sm font-medium font-mono")
            with ui.column().classes("gap-0"):
                ui.label("Price").classes("text-xs opacity-60 uppercase")
                ui.label(f"${price:,.0f}").classes("text-base font-semibold font-mono")

        # Cost decomposition strip — labor + G&A + contingency = subtotal
        # + profit = price. Helpful for the user to spot any phase
        # that's burdened heavily (high G&A relative to labor) or
        # has unusual contingency.
        with ui.row().classes("items-center gap-4 pt-2 pb-1 flex-wrap text-xs opacity-70 font-mono"):
            ui.label(f"loaded labor ${loaded:,.0f}")
            ui.label("·")
            ui.label(f"G&A ${ga:,.0f}")
            ui.label("·")
            ui.label(f"contingency ${cont:,.0f}")
            ui.label("·")
            ui.label(f"profit ${profit:,.0f}")

        # Per-category allocation table — collapsed by default.
        allocations = phase.get("labor_allocations") or []
        warns = phase.get("allocation_warnings") or []
        if allocations:
            with ui.expansion(
                f"Itemized labor ({len(allocations)} category(ies))",
                icon="list_alt",
                value=False,
            ).classes("w-full pt-1"):
                _render_phase_allocations_table(allocations)
                if warns:
                    for w in warns:
                        ui.label(f"⚠ {w}").classes("text-xs text-amber-700 pt-1")


def _render_phase_allocations_table(allocations: list[dict]) -> None:
    """Per-category labor allocation table inside one phase."""
    columns = [
        {"name": "category", "label": "Category", "field": "category", "align": "left"},
        {"name": "hours", "label": "Hours", "field": "hours", "align": "right"},
        {"name": "loaded_rate", "label": "Loaded $/hr", "field": "loaded_rate", "align": "right"},
        {"name": "loaded_cost", "label": "Loaded $", "field": "loaded_cost", "align": "right"},
        {"name": "ga_alloc", "label": "G&A $", "field": "ga_alloc", "align": "right"},
        {"name": "billed_rate", "label": "Billed $/hr", "field": "billed_rate", "align": "right"},
        {"name": "billed_total", "label": "Billed $", "field": "billed_total", "align": "right"},
    ]
    rows: list[dict] = []
    for a in allocations:
        rows.append(
            {
                "category": a.get("labor_category") or "—",
                "hours": f"{float(a.get('hours') or 0):,.0f}",
                "loaded_rate": f"${float(a.get('loaded_hourly_rate_usd') or 0):.2f}",
                "loaded_cost": f"${float(a.get('loaded_cost_usd') or 0):,.0f}",
                "ga_alloc": f"${float(a.get('ga_allocation_usd') or 0):,.0f}",
                "billed_rate": f"${float(a.get('proposed_billing_rate_usd') or 0):.2f}",
                "billed_total": f"${float(a.get('billed_total_usd') or 0):,.0f}",
            }
        )
    ui.table(
        columns=columns,
        rows=rows,
        row_key="category",
    ).classes("w-full")


def _render_cost_section_drafts(
    proposal_id: int,
    sections: list[dict],
) -> None:
    """One row per cost-deferred section with status + preview."""
    with ui.card().classes("w-full"):
        ui.label("Cost section drafts").classes("text-base font-medium")
        ui.label(
            "Sections flagged requires_cost_analysis=True on the "
            "Outline tab. Drafts here use the proposed (MEDIUM) "
            "scenario's pricing data and include citations to the "
            "structured cost build."
        ).classes("text-xs opacity-70 pb-2")

        for sec in sections:
            with ui.expansion(
                f"{sec['section_id']} — {sec['section_title']}",
                icon=("article" if sec["has_draft"] else "edit_note"),
                value=False,
            ).classes("w-full"):
                _render_one_cost_section(proposal_id, sec)


def _render_one_cost_section(proposal_id: int, sec: dict) -> None:
    """Status header + preview + per-section regenerate."""
    with ui.row().classes("items-center gap-3 w-full flex-wrap pt-1"):
        if sec["has_draft"]:
            n_chars = len(sec["draft_text_markdown"])
            n_cites = len(sec["citations"])
            n_nh = len(sec["needs_human_placeholders"])
            ui.chip(
                f"drafted · rev {sec['revision']}",
                icon="check_circle",
            ).props("color=positive text-color=white dense")
            ui.label(
                f"{n_chars:,} chars · {n_cites} citation(s) · {n_nh} needs-human placeholder(s)"
            ).classes("text-xs opacity-70 font-mono")
        else:
            ui.chip(
                "not yet drafted",
                icon="hourglass_empty",
            ).props("color=blue-grey-3 text-color=black dense")
        if sec["excluded_from_draft"]:
            ui.chip(
                "excluded from draft",
                icon="block",
            ).props("color=warning text-color=white dense")

        ui.element("div").classes("flex-1")
        if sec["updated_at"]:
            ui.label(f"updated {sec['updated_at']:%Y-%m-%d %H:%M}").classes("text-xs font-mono opacity-50")

    if sec["section_brief"]:
        with ui.expansion(
            "Section brief",
            icon="info",
            value=False,
        ).classes("w-full pt-1"):
            ui.label(sec["section_brief"]).classes("text-sm whitespace-pre-wrap opacity-80")

    if sec["has_draft"]:
        # Markdown preview.
        ui.markdown(sec["draft_text_markdown"]).classes("w-full prose prose-sm max-w-none pt-2")

        if sec["needs_human_placeholders"]:
            with ui.expansion(
                f"Needs-human placeholders ({len(sec['needs_human_placeholders'])})",
                icon="pending_actions",
                value=False,
            ).classes("w-full pt-2"):
                for ph in sec["needs_human_placeholders"]:
                    marker = ph.get("marker") or "?"
                    desc = (ph.get("description") or "").strip()
                    with ui.row().classes("items-start gap-2 pt-1"):
                        ui.label(f"[[{marker}]]").classes(
                            "text-xs font-mono font-medium text-amber-700 w-48 break-all"
                        )
                        ui.label(desc).classes("text-xs flex-1")


# ---- end Cost tab --------------------------------------------------------


# `_render_compliance_tab` extracted to app/ui/tabs/compliance.py
# (see top-of-file import). Kept here as a comment so future grep
# for the function name lands on a breadcrumb to its new home.


# ----- Knowledge Base ------------------------------------------------------------

# Class label and a one-line description for the legend.
_KB_CLASS_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    KbDocumentClass.PAST_PERFORMANCE_WON.value: (
        "Past performance — won",
        "Completed work as prime",
    ),
    KbDocumentClass.PAST_PERFORMANCE_SUBBED.value: (
        "Past performance — subbed",
        "Completed work as subcontractor",
    ),
    KbDocumentClass.REFERENCES_PROJECT.value: (
        "References — project",
        "Customer reference letters or contact info for completed projects",
    ),
    KbDocumentClass.REFERENCES_PERSONNEL.value: (
        "References — personnel",
        "Letters or contact info backing up individual staff",
    ),
    KbDocumentClass.PRIOR_PROPOSAL_WON.value: (
        "Prior proposal — won",
        "Voice + lessons learned",
    ),
    KbDocumentClass.PRIOR_PROPOSAL_PENDING.value: (
        "Prior proposal — pending",
        "Voice grounding only",
    ),
    KbDocumentClass.PRIOR_PROPOSAL_LOST.value: (
        "Prior proposal — lost",
        "Voice + post-mortem context",
    ),
    KbDocumentClass.CORPORATE.value: (
        "Corporate",
        "Capability statement, bio, website",
    ),
    KbDocumentClass.PERSONNEL.value: (
        "Personnel",
        "Resume, structured bio",
    ),
    KbDocumentClass.COMPLIANCE_EVIDENCE.value: (
        "Compliance evidence",
        "Insurance, financials, certs",
    ),
    KbDocumentClass.AGENCY_CONTEXT.value: (
        "Agency context",
        "Strategic plans, GAO/OIG, Q&A",
    ),
    KbDocumentClass.BOILERPLATE.value: (
        "Boilerplate",
        "Standard reusable sections",
    ),
    KbDocumentClass.PROCUREMENT_CRAFT.value: (
        "Procurement craft",
        "Public-domain proposal-writing guides, evaluator-psychology references",
    ),
}

# Grouped by citation legitimacy — the actual functional distinction.
_KB_CLASS_GROUPS: list[dict] = [
    {
        "title": "Citable as past performance",
        "subtitle": "These can back up 'we delivered X' claims in proposals.",
        "icon": "verified",
        "color": "green-7",
        "bg": "bg-green-50 border-green-300",
        "classes": [
            KbDocumentClass.PAST_PERFORMANCE_WON.value,
            KbDocumentClass.PAST_PERFORMANCE_SUBBED.value,
        ],
    },
    {
        "title": "References — RFP-ready",
        "subtitle": (
            "Customer letters, named referees, contact info. Most RFPs require both "
            "project references (3 recent contracts) and personnel references for key staff. "
            "Keep these populated so the Writer Team can plug them in directly."
        ),
        "icon": "thumb_up",
        "color": "teal-7",
        "bg": "bg-teal-50 border-teal-300",
        "classes": [
            KbDocumentClass.REFERENCES_PROJECT.value,
            KbDocumentClass.REFERENCES_PERSONNEL.value,
        ],
    },
    {
        "title": "Voice grounding only",
        "subtitle": "Use to match Quadratic's writing style and structure. NEVER cite as completed work — Reviewer A flags violations.",
        "icon": "warning",
        "color": "amber-8",
        "bg": "bg-amber-50 border-amber-300",
        "classes": [
            KbDocumentClass.PRIOR_PROPOSAL_WON.value,
            KbDocumentClass.PRIOR_PROPOSAL_PENDING.value,
            KbDocumentClass.PRIOR_PROPOSAL_LOST.value,
        ],
    },
    {
        "title": "Corporate & personnel grounding",
        "subtitle": "Source of capabilities, certifications, and key personnel facts.",
        "icon": "business",
        "color": "blue-7",
        "bg": "bg-blue-50 border-blue-300",
        "classes": [
            KbDocumentClass.CORPORATE.value,
            KbDocumentClass.PERSONNEL.value,
        ],
    },
    {
        "title": "Reference & support",
        "subtitle": "Used for compliance proof, agency intel, and reusable boilerplate.",
        "icon": "library_books",
        "color": "slate-6",
        "bg": "bg-slate-50 border-slate-300",
        "classes": [
            KbDocumentClass.COMPLIANCE_EVIDENCE.value,
            KbDocumentClass.AGENCY_CONTEXT.value,
            KbDocumentClass.BOILERPLATE.value,
        ],
    },
    {
        "title": "Procurement craft",
        "subtitle": (
            "Public-domain proposal-writing guides (APMP, Shipley, GSA, agency procurement "
            "institutes). The Writer Team uses these to shape structure and evaluator-psychology "
            "choices — never to quote text from. Keep copyrighted training material and "
            "competitor proposals OUT."
        ),
        "icon": "school",
        "color": "purple-7",
        "bg": "bg-purple-50 border-purple-300",
        "classes": [
            KbDocumentClass.PROCUREMENT_CRAFT.value,
        ],
    },
]


# ---- Learned Guidance tab -------------------------------------------------

_LESSON_KIND_LABELS = {
    "writer_avoid": "Writer should avoid",
    "reviewer_calibrate": "Reviewer should not flag",
}
_LESSON_STATUS_VISUAL = {
    "draft": ("hourglass_top", "amber-700", "bg-amber-50", "border-amber-300"),
    "approved": ("check_circle", "green-700", "bg-green-50", "border-green-400"),
    "archived": ("inventory_2", "slate-600", "bg-slate-50", "border-slate-300"),
}


def _render_learned_guidance_tab() -> None:
    """Learned Guidance tab — durable rules extracted from user
    accept/dismiss actions on reviewer findings.

    Three sections:
    - Pending Review (status='draft') — rules awaiting user approval.
      These are NOT injected into agent prompts yet. Approving promotes
      them; archiving removes them.
    - Active Rules (status='approved') — currently injected into the
      writer + reviewer system prompts on every run.
    - Archived (status='archived') — past rules kept for audit.

    Plus a per-category accept/dismiss-rate panel so the user can see
    where reviewers are running hot vs. accurate.
    """

    @ui.refreshable
    def render() -> None:
        rules = list_rules()
        cat_rates = get_category_action_rates()

        n_draft = sum(1 for r in rules if r.status == "draft")
        n_approved = sum(1 for r in rules if r.status == "approved")
        n_archived = sum(1 for r in rules if r.status == "archived")

        # Header + intro
        with ui.card().classes("w-full"):
            with ui.row().classes("items-start gap-3 w-full"):
                ui.icon("school").classes("text-indigo-700 text-2xl pt-1")
                with ui.column().classes("gap-0 flex-1"):
                    ui.label(
                        "The system learns from your accept / dismiss actions on reviewer findings."
                    ).classes("text-base font-semibold")
                    ui.label(
                        "Accepting a finding teaches the writer to avoid "
                        "the pattern. Dismissing with a reason teaches the "
                        "reviewer not to flag it. Each rule starts as a "
                        "draft for your approval — only approved rules are "
                        "injected into future agent prompts."
                    ).classes("text-sm opacity-80")
                    parts = [
                        f"{n_draft} pending",
                        f"{n_approved} active",
                        f"{n_archived} archived",
                    ]
                    ui.label(" · ".join(parts)).classes("text-xs opacity-70 pt-2 font-mono")

        # Category calibration panel
        if cat_rates:
            with ui.card().classes("w-full"):
                ui.label("Per-category user feedback").classes("text-sm font-semibold")
                ui.label(
                    "How often you accept vs. dismiss findings by "
                    "category. Categories you dismiss frequently are "
                    "automatically surfaced to the reviewer as "
                    "calibration guidance (≥5 actions threshold)."
                ).classes("text-xs opacity-70 pb-1")
                rows_for_table: list[dict] = []
                for cat, stats in sorted(cat_rates.items()):
                    total = stats["accepted"] + stats["dismissed"]
                    if total == 0:
                        continue
                    accept_pct = round(100 * stats["accepted"] / total)
                    rows_for_table.append(
                        {
                            "category": _FINDING_CATEGORY_LABELS.get(cat, cat),
                            "accepted": stats["accepted"],
                            "dismissed": stats["dismissed"],
                            "total": total,
                            "accept_pct": f"{accept_pct}%",
                            "dismiss_pct": f"{100 - accept_pct}%",
                            "calibrated": "yes" if total >= 5 else "—",
                        }
                    )
                if rows_for_table:
                    ui.table(
                        columns=[
                            {"name": "category", "label": "Category", "field": "category", "align": "left"},
                            {"name": "accepted", "label": "Accepted", "field": "accepted", "align": "right"},
                            {
                                "name": "dismissed",
                                "label": "Dismissed",
                                "field": "dismissed",
                                "align": "right",
                            },
                            {"name": "total", "label": "Total", "field": "total", "align": "right"},
                            {
                                "name": "accept_pct",
                                "label": "Accept %",
                                "field": "accept_pct",
                                "align": "right",
                            },
                            {
                                "name": "dismiss_pct",
                                "label": "Dismiss %",
                                "field": "dismiss_pct",
                                "align": "right",
                            },
                            {
                                "name": "calibrated",
                                "label": "Injected as calibration",
                                "field": "calibrated",
                                "align": "center",
                            },
                        ],
                        rows=rows_for_table,
                        row_key="category",
                    ).classes("w-full")
                else:
                    ui.label(
                        "No accept/dismiss actions yet — calibration "
                        "stats will appear once you start reviewing "
                        "findings."
                    ).classes("text-xs opacity-60")

        # Pending Review (draft)
        draft_rules = [r for r in rules if r.status == "draft"]
        if draft_rules:
            with ui.column().classes("w-full gap-2 pt-2"):
                ui.label(f"Pending review ({len(draft_rules)})").classes("text-base font-semibold")
                ui.label(
                    "These rules were extracted from your recent "
                    "accept/dismiss actions. Approve the ones you want "
                    "applied; archive the rest."
                ).classes("text-xs opacity-70")
                for r in draft_rules:
                    _render_lesson_card(r, on_change=render.refresh)
        else:
            with ui.card().classes("w-full"):
                ui.label("No pending rules.").classes("text-sm opacity-70")
                ui.label(
                    "Accept or dismiss-with-reason a reviewer finding "
                    "to start building the system's learned guidance."
                ).classes("text-xs opacity-60")

        # Active (approved)
        approved_rules = [r for r in rules if r.status == "approved"]
        if approved_rules:
            with ui.expansion(
                f"Active rules ({len(approved_rules)}) — injected into every writer + reviewer call",
                icon="check_circle",
                value=True,
            ).classes("w-full"):
                for r in approved_rules:
                    _render_lesson_card(r, on_change=render.refresh)

        # Archived
        archived_rules = [r for r in rules if r.status == "archived"]
        if archived_rules:
            with ui.expansion(
                f"Archived ({len(archived_rules)})",
                icon="inventory_2",
                value=False,
            ).classes("w-full"):
                for r in archived_rules:
                    _render_lesson_card(r, on_change=render.refresh)

    render()
    # Light auto-refresh: extraction is async (a Haiku call after each
    # accept/dismiss), so a draft may appear a few seconds after the
    # action. A 5s poll catches it without forcing the user to re-navigate.
    ui.timer(5.0, render.refresh)


def _render_lesson_card(r, on_change) -> None:
    """One learned-rule card with status-appropriate actions.

    `r` is a LessonRow dataclass instance (from app.services.lessons).
    """
    icon, accent, bg, border = _LESSON_STATUS_VISUAL.get(
        r.status,
        ("help", "slate-600", "bg-slate-50", "border-slate-300"),
    )
    accent_color = accent.split("-")[0]

    with ui.card().classes(f"w-full {bg} border-l-4 {border}"):
        with ui.row().classes("items-start gap-3 w-full"):
            ui.icon(icon).classes(f"text-{accent} text-xl pt-0.5")
            with ui.column().classes("gap-0 flex-1"):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.chip(_LESSON_KIND_LABELS.get(r.kind, r.kind)).props(
                        f"dense color={accent_color}-2 text-color={accent_color}-9"
                    )
                    ui.chip(r.status).props("dense color=slate-2 text-color=slate-9").classes("text-xs")
                    if r.source_category:
                        ui.chip(_FINDING_CATEGORY_LABELS.get(r.source_category, r.source_category)).props(
                            "dense color=slate-2 text-color=slate-9"
                        ).classes("text-xs")
                    if r.source_severity:
                        ui.chip(r.source_severity).props("dense color=slate-2 text-color=slate-9").classes(
                            "text-xs"
                        )
                    if r.source_reviewer:
                        ui.label(f"Reviewer {r.source_reviewer}").classes("text-xs opacity-60 font-mono")
                    if r.hits:
                        ui.label(f"used {r.hits}×").classes("text-xs opacity-60 font-mono")
                ui.label(r.rule_text).classes("text-sm pt-1 whitespace-pre-wrap")
                if r.source_action:
                    ui.label(f"Source: user {r.source_action}ed a finding").classes(
                        "text-xs opacity-60 pt-1 italic"
                    )

            with ui.column().classes("gap-1 items-stretch"):
                if r.status == "draft":
                    ui.button(
                        "Approve",
                        icon="check",
                        on_click=(lambda rid=r.id: _approve_rule_action(rid, on_change)),
                    ).props("color=primary dense size=sm")
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=(
                            lambda rid=r.id, txt=r.rule_text: _open_edit_rule_dialog(rid, txt, on_change)
                        ),
                    ).props("flat dense size=sm")
                    ui.button(
                        "Archive",
                        icon="inventory_2",
                        on_click=(lambda rid=r.id: _archive_rule_action(rid, on_change)),
                    ).props("flat dense size=sm color=slate-7")
                elif r.status == "approved":
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=(
                            lambda rid=r.id, txt=r.rule_text: _open_edit_rule_dialog(rid, txt, on_change)
                        ),
                    ).props("flat dense size=sm")
                    ui.button(
                        "Archive",
                        icon="inventory_2",
                        on_click=(lambda rid=r.id: _archive_rule_action(rid, on_change)),
                    ).props("flat dense size=sm color=slate-7")
                elif r.status == "archived":
                    ui.button(
                        "Re-approve",
                        icon="restart_alt",
                        on_click=(lambda rid=r.id: _approve_rule_action(rid, on_change)),
                    ).props("flat dense size=sm color=primary")
                    ui.button(
                        "Delete",
                        icon="delete",
                        on_click=(lambda rid=r.id: _delete_rule_action(rid, on_change)),
                    ).props("flat dense size=sm color=red-7")


def _approve_rule_action(rule_id: int, on_change) -> None:
    if approve_rule(rule_id):
        ui.notify(
            "Rule approved — it will be injected into the next agent run.",
            type="positive",
        )
        on_change()
    else:
        ui.notify("Could not approve rule.", type="negative")


def _archive_rule_action(rule_id: int, on_change) -> None:
    if archive_rule(rule_id):
        ui.notify("Rule archived.", type="positive")
        on_change()
    else:
        ui.notify("Could not archive rule.", type="negative")


def _delete_rule_action(rule_id: int, on_change) -> None:
    if delete_rule(rule_id):
        ui.notify("Rule deleted.", type="positive")
        on_change()
    else:
        ui.notify("Could not delete rule.", type="negative")


def _open_edit_rule_dialog(rule_id: int, current_text: str, on_change) -> None:
    """In-place editor so the user can sharpen an extracted rule's wording
    before approving."""
    with ui.dialog() as dlg, ui.card().classes("min-w-[36rem]"):
        ui.label("Edit rule").classes("text-base font-semibold")
        ui.label(
            "Rules are injected verbatim into agent system prompts. "
            "Imperative voice and concrete pattern descriptions work best."
        ).classes("text-xs opacity-70 pt-1")
        text_input = (
            ui.textarea("Rule text", value=current_text)
            .classes("w-full pt-2")
            .props("autogrow rounded outlined")
        )
        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Cancel", on_click=dlg.close).props("flat")

            def apply() -> None:
                new_text = (text_input.value or "").strip()
                if not new_text:
                    ui.notify("Rule text cannot be empty.", type="negative")
                    return
                ok = update_rule_text(rule_id, new_text)
                dlg.close()
                if ok:
                    ui.notify("Rule updated.", type="positive")
                    on_change()
                else:
                    ui.notify("Could not update rule.", type="negative")

            ui.button("Save", icon="save", on_click=apply).props("color=primary")
    dlg.open()


def _render_reclassify_control() -> None:
    """Top-right control on the KB page: 'Re-classify all' button + live progress."""
    container = ui.row().classes("items-center gap-2")

    def render() -> None:
        container.clear()
        with container:
            progress = get_reclassify_progress()
            if progress.get("running"):
                done = progress.get("done", 0)
                total = progress.get("total", 0)
                ui.spinner("dots", size="sm", color="primary")
                ui.label(f"Re-classifying… {done}/{total}").classes("text-sm opacity-80")
            else:
                # Show a one-time summary if a recent run finished.
                if progress.get("completed_at") and progress.get("total"):
                    updated = progress.get("updated", 0)
                    skipped = progress.get("skipped", 0)
                    ui.label(
                        f"Last run: {updated} updated" + (f", {skipped} skipped" if skipped else "")
                    ).classes("text-xs opacity-60")

                def confirm_and_run() -> None:
                    with SessionLocal() as db:
                        n = db.query(KnowledgeBaseDocument).count()
                    with ui.dialog() as dlg, ui.card():
                        ui.label(f"Re-classify all {n} KB document(s)?").classes("text-base font-medium")
                        ui.label(
                            "Runs the auto-classifier against each doc's already-extracted "
                            "text. Updates document_class and tags. Drops pending profile "
                            "suggestions and re-runs fact extraction (approved/rejected "
                            "suggestions are preserved)."
                        ).classes("text-xs opacity-60 pt-1")
                        ui.label(
                            f"Approximate cost: ~${n * 0.012:.2f} "
                            "(Haiku classify + Haiku fact extract per doc)."
                        ).classes("text-xs opacity-60 pt-1")
                        with ui.row().classes("w-full justify-end gap-2 pt-3"):
                            ui.button("Cancel", on_click=dlg.close).props("flat")

                            def go() -> None:
                                t = spawn_reclassify_all()
                                dlg.close()
                                if t is None:
                                    ui.notify(
                                        "A re-classify job is already running.",
                                        type="warning",
                                    )
                                else:
                                    ui.notify(
                                        "Re-classify started. Progress shows in the header.",
                                        type="positive",
                                    )
                                    render()  # immediate re-render to show running state

                            ui.button("Re-classify all", icon="refresh", on_click=go).props("color=primary")
                    dlg.open()

                ui.button("Re-classify all", icon="auto_fix_high", on_click=confirm_and_run).props(
                    "flat dense"
                )

    render()
    ui.timer(2.0, render)


def _render_class_legend() -> None:
    ui.label("Document classes — citation legitimacy reference").classes("text-base font-medium pt-4")
    ui.label(
        "Class metadata determines what each document can be cited for. The fact-extraction "
        "agent only generates profile suggestions for personnel, corporate, and past_performance_*."
    ).classes("text-xs opacity-60 pb-3")

    with ui.column().classes("w-full gap-3"):
        for group in _KB_CLASS_GROUPS:
            with ui.card().classes(f"w-full border-l-4 {group['bg']}"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.icon(group["icon"]).classes(f"text-{group['color']}")
                    ui.label(group["title"]).classes(f"text-sm font-semibold text-{group['color']}")
                ui.label(group["subtitle"]).classes("text-xs opacity-70")
                with ui.row().classes("flex-wrap gap-2 pt-2"):
                    for cls_value in group["classes"]:
                        label, desc = _KB_CLASS_DESCRIPTIONS.get(cls_value, (cls_value, ""))
                        with ui.chip(label).props(f"color={group['color']} text-color=white"):
                            if desc:
                                ui.tooltip(desc)


# ----- Knowledge Base ------------------------------------------------------------

# Class options for the upload form, ordered by frequency-of-use.
_KB_CLASS_OPTIONS = {
    KbDocumentClass.CORPORATE.value: "Corporate (capability statement, bio)",
    KbDocumentClass.PERSONNEL.value: "Personnel (resume, structured bio)",
    KbDocumentClass.PAST_PERFORMANCE_WON.value: "Past performance — won (citable)",
    KbDocumentClass.PAST_PERFORMANCE_SUBBED.value: "Past performance — subcontractor (citable)",
    KbDocumentClass.REFERENCES_PROJECT.value: "References — project / customer (RFP-ready)",
    KbDocumentClass.REFERENCES_PERSONNEL.value: "References — personnel / staff (RFP-ready)",
    KbDocumentClass.PRIOR_PROPOSAL_PENDING.value: "Prior proposal — pending (voice only, not citable)",
    KbDocumentClass.PRIOR_PROPOSAL_WON.value: "Prior proposal — won (voice only, not citable)",
    KbDocumentClass.PRIOR_PROPOSAL_LOST.value: "Prior proposal — lost (voice + lessons learned)",
    KbDocumentClass.COMPLIANCE_EVIDENCE.value: "Compliance evidence (insurance, financials, certs)",
    KbDocumentClass.AGENCY_CONTEXT.value: "Agency context (strategic plans, GAO/OIG, Q&A)",
    KbDocumentClass.BOILERPLATE.value: "Boilerplate (standard sections)",
    KbDocumentClass.PROCUREMENT_CRAFT.value: "Procurement craft (proposal-writing guides, evaluator psych)",
}

_KB_STATUS_VISUAL = {
    "pending": ("hourglass_top", "text-amber-700"),
    "active": ("check_circle", "text-green-700"),
    "deactivated": ("block", "text-slate-500"),
}


@ui.page("/kb")
def knowledge_base() -> None:
    with page_frame("Knowledge Base"):
        if get_settings().is_demo:
            _empty_state(
                "Knowledge Base management is locked in the curated demo workspace.",
                icon="lock",
            )
            return

        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-0"):
                ui.label("Knowledge Base").classes("text-xl font-semibold")
                ui.label(
                    "Class metadata determines citation legitimacy. Past performance citations "
                    "trace ONLY to past_performance_won/subbed; pending and lost prior proposals "
                    "can ground voice but cannot be cited as completed work."
                ).classes("text-sm opacity-70")
            _render_reclassify_control()

        with ui.tabs().classes("w-full") as tabs:
            ui.tab("Documents", icon="library_books")
            ui.tab("Learned Guidance", icon="school")
        with ui.tab_panels(tabs, value="Documents").classes("w-full"):
            with ui.tab_panel("Documents"):
                _render_kb_documents_panel()
            with ui.tab_panel("Learned Guidance"):
                _render_learned_guidance_tab()


def _render_kb_documents_panel() -> None:
    """Documents tab body — upload form + classified document list. Extracted
    from knowledge_base() so it can be hosted inside a tab panel without
    indenting ~540 lines in place."""
    # Per-page upload state. Each staged file carries its own class + tags
    # from the auto-classifier; the form's class/tags are optional overrides
    # applied to every file on save.
    staged: dict[str, dict] = {}
    # entry: {data, status, cls, tags, confidence, rationale}

    with ui.card().classes("w-full"):
        ui.label("Add documents").classes("text-base font-medium")

        staged_list = ui.column().classes("w-full")
        staged_list.props["role"] = "list"
        staged_list.props["aria-label"] = "Staged KB files"

        def render_staged() -> None:
            staged_list.clear()
            with staged_list:
                if not staged:
                    ui.label("No files staged.").classes("text-sm opacity-50")
                    return
                for name, info in staged.items():
                    size_kb = len(info["data"]) / 1024
                    staged_row = ui.row().classes("items-center gap-2 w-full")
                    staged_row.props["role"] = "listitem"
                    staged_row.props["aria-label"] = f"Staged KB file {name}"
                    with staged_row:
                        ui.icon("description")
                        with ui.column().classes("gap-0 flex-1"):
                            ui.label(name).classes("text-sm")
                            status = info.get("status", "pending")
                            if status == "classifying":
                                ui.label("Classifying…").classes("text-xs italic text-blue-700")
                            elif status == "failed":
                                ui.label("⚠ Classification failed — using form fallback").classes(
                                    "text-xs italic text-amber-700"
                                )
                            elif status == "done" and info.get("cls"):
                                conf = info.get("confidence", "medium")
                                conf_color = {
                                    "high": "text-green-700",
                                    "medium": "text-blue-700",
                                    "low": "text-amber-700",
                                }.get(conf, "text-blue-700")
                                tag_str = (
                                    " · tags: " + ", ".join(info.get("tags") or [])
                                    if info.get("tags")
                                    else ""
                                )
                                ui.label(f"✓ {info['cls']} (confidence: {conf}){tag_str}").classes(
                                    f"text-xs italic {conf_color}"
                                )
                        ui.label(f"{size_kb:.1f} KB").classes("text-xs opacity-60")
                        remove_button = ui.button(
                            icon="close",
                            on_click=lambda n=name: (staged.pop(n, None), render_staged()),
                        ).props("flat dense")
                        remove_button.props["aria-label"] = (
                            f"Remove staged KB file {name}"
                        )

        render_staged()

        async def classify_file(name: str) -> None:
            info = staged.get(name)
            if info is None:
                return
            info["status"] = "classifying"
            render_staged()
            try:
                result: ClassificationResult | None = await asyncio.to_thread(
                    classify_kb_upload, name, info["data"]
                )
            except Exception:
                log.exception("kb classify failed for %s", name)
                if name in staged:
                    staged[name]["status"] = "failed"
                    render_staged()
                return
            if name not in staged:
                return  # user removed mid-classify
            if result is None:
                staged[name]["status"] = "failed"
                render_staged()
                return
            staged[name].update(
                status="done",
                cls=result.document_class.value,
                tags=result.tags,
                confidence=result.confidence,
                rationale=result.rationale,
            )
            render_staged()
            _maybe_autofill_form()

        def _maybe_autofill_form() -> None:
            """For single-file uploads, mirror the classifier's choice into the
            form so the user sees what's about to be saved. For multi-file
            uploads we don't auto-fill — per-file detection handles each
            file's class and tags independently. Never clobber user input."""
            done = [info for info in staged.values() if info.get("status") == "done" and info.get("cls")]
            if not done:
                return

            classes = {info["cls"] for info in done}

            # Single file: fill both fields from its result.
            if len(staged) == 1 and len(done) == 1:
                only = done[0]
                if not class_in.value:
                    class_in.value = only["cls"]
                if only.get("tags") and not (tags_in.value or "").strip():
                    tags_in.value = ", ".join(only["tags"])
                return

            # Multi-file batch where all classifications agree: just the class.
            # Tags differ per file, so we don't fill the "extra tags" field.
            if len(classes) == 1:
                only_class = next(iter(classes))
                if not class_in.value:
                    class_in.value = only_class

        async def on_upload(e) -> None:
            try:
                data = await e.file.read()
                name = e.file.name
                staged[name] = {
                    "data": data,
                    "status": "pending",
                    "cls": None,
                    "tags": [],
                    "confidence": None,
                    "rationale": None,
                }
                ui.notify(f"Staged {name} ({len(data) / 1024:.1f} KB)", type="positive")
                render_staged()
                # Fire-and-forget; multiple files classify in parallel.
                asyncio.create_task(classify_file(name))
            except Exception as exc:
                log.exception("kb upload handler failed")
                ui.notify(f"Upload failed: {exc}", type="negative")

        upload_el = (
            ui.upload(
                multiple=True,
                max_file_size=MAX_KB_FILE_BYTES,
                max_total_size=MAX_KB_BATCH_BYTES,
                auto_upload=True,
                on_upload=on_upload,
                on_rejected=lambda: ui.notify(
                    "Upload rejected: files must be 50 MB or less each and 200 MB or less total.",
                    type="negative",
                ),
                label="Drop KB files here",
            )
            .props("accept=.pdf,.docx,.xlsx,.txt,.md,.markdown,.csv")
            .classes("w-full")
        )

        ui.label(
            "Each file is classified individually by the auto-classifier. The "
            "fields below are OVERRIDES — leave them empty to use per-file detection."
        ).classes("text-xs opacity-60")
        with ui.row().classes("w-full gap-3"):
            class_in = ui.select(
                options=_KB_CLASS_OPTIONS,
                label="Override class for all files (optional)",
                value=None,
            ).classes("flex-1")
            tags_in = ui.input(
                "Additional tags applied to all (optional)",
                placeholder="e.g. CMS, Medicaid, MMIS",
            ).classes("flex-1")

        def _do_save(skip_dup_filenames: set[str] | None = None) -> None:
            """Inner save — used by on_save and by the duplicate dialog's
            'Save anyway' / 'Skip duplicates' actions."""
            skip = skip_dup_filenames or set()

            # Optional override class — only validated if the user picked one.
            override_cls: KbDocumentClass | None = None
            if class_in.value:
                try:
                    override_cls = KbDocumentClass(class_in.value)
                except ValueError:
                    ui.notify("Override class is invalid.", type="warning")
                    return

            # Files that haven't been classified yet AND have no override fall
            # back to a hard 'pick a class' error so we never save un-classed.
            missing = [n for n, info in staged.items() if not info.get("cls") and override_cls is None]
            if missing:
                ui.notify(
                    f"Still classifying {len(missing)} file(s) — wait a moment, or "
                    f"set the override class above.",
                    type="warning",
                )
                return

            # Heads-up if the override class disagrees with per-file detection on
            # any file — common when batch-uploading a heterogeneous mix while
            # the form still has the first file's class. Doesn't block save.
            if override_cls is not None:
                mismatched = [
                    n for n, info in staged.items() if info.get("cls") and info["cls"] != override_cls.value
                ]
                if mismatched:
                    ui.notify(
                        f"Override class '{override_cls.value}' will overwrite "
                        f"per-file detection on {len(mismatched)} file(s). "
                        f"Clear the override field above if that's not what you want.",
                        type="warning",
                        multi_line=True,
                        timeout=8000,
                    )

            extra_tags = [t.strip() for t in (tags_in.value or "").split(",") if t.strip()]

            # Build per-file KbUploadedFile with each file's own class + tags.
            files = []
            for name, info in staged.items():
                if name in skip:
                    continue
                file_cls = override_cls
                if file_cls is None and info.get("cls"):
                    try:
                        file_cls = KbDocumentClass(info["cls"])
                    except ValueError:
                        file_cls = None
                files.append(
                    KbUploadedFile(
                        filename=name,
                        content=info["data"],
                        document_class=file_cls,
                        tags=info.get("tags") or [],
                    )
                )

            if not files:
                ui.notify(
                    "All files were duplicates and skipped. Nothing to save.",
                    type="warning",
                )
                return

            try:
                with session_scope() as db:
                    docs = create_kb_documents(
                        db,
                        files=files,
                        document_class=override_cls,  # fallback only
                        tags=extra_tags,  # merged with per-file
                        status=KbDocumentStatus.PENDING,
                    )
                    doc_ids = [d.id for d in docs]
            except Exception as exc:
                log.exception("create kb documents failed")
                ui.notify(f"Save failed: {exc}", type="negative")
                return

            for did in doc_ids:
                spawn_kb_ingest(did)

            ui.notify(
                f"Saved {len(doc_ids)} document(s). Ingesting in background — "
                "watch the list and the Config → Pending Profile Updates panel.",
                type="positive",
            )
            staged.clear()
            render_staged()
            tags_in.value = ""
            class_in.value = None
            upload_el.reset()  # clear the widget's own file-row display
            refresh_list()

        def on_save() -> None:
            """Public save handler. Checks for duplicates first; if any found,
            opens a dialog so the user can skip them, save anyway, or cancel.
            Otherwise delegates straight to _do_save."""
            if not staged:
                ui.notify("Add at least one file first.", type="warning")
                return

            # Build candidate files just for the duplicate check.
            candidates = [KbUploadedFile(filename=n, content=info["data"]) for n, info in staged.items()]
            with SessionLocal() as db:
                dups = find_duplicate_documents(db, candidates)
                dup_info = {
                    n: {
                        "id": d.id,
                        "filename": d.filename,
                        "cls": d.document_class.value
                        if hasattr(d.document_class, "value")
                        else str(d.document_class),
                    }
                    for n, d in dups.items()
                }

            if not dup_info:
                _do_save()
                return

            # Duplicate(s) detected — ask the user.
            with ui.dialog() as dlg, ui.card().classes("max-w-2xl"):
                n = len(dup_info)
                ui.label(f"{n} of these file(s) match existing KB documents (same content):").classes(
                    "text-base font-medium"
                )
                with ui.column().classes("gap-1 pt-2"):
                    for new_name, existing in dup_info.items():
                        ui.label(
                            f"• {new_name}  →  matches #{existing['id']} "
                            f"({existing['cls']}) {existing['filename']}"
                        ).classes("text-sm")
                ui.label(
                    "Recommended: skip the duplicates so your KB stays clean. "
                    "If the documents have changed and you want both versions, "
                    "save anyway."
                ).classes("text-xs opacity-60 pt-2")

                with ui.row().classes("w-full justify-end gap-2 pt-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")

                    def skip_dups() -> None:
                        dlg.close()
                        _do_save(skip_dup_filenames=set(dup_info.keys()))

                    def save_anyway() -> None:
                        dlg.close()
                        _do_save()

                    ui.button("Skip duplicates", icon="skip_next", on_click=skip_dups).props("color=primary")
                    ui.button("Save anyway", icon="warning_amber", on_click=save_anyway).props(
                        "flat color=amber-9"
                    )
            dlg.open()

        with ui.row().classes("w-full justify-end pt-1"):
            ui.button("Save to KB", icon="save", on_click=on_save).props("color=primary")

    # Document list — refreshed manually after save and on a slow timer.
    list_card = ui.card().classes("w-full")
    with list_card:
        ui.label("Documents").classes("text-base font-medium")
        list_container = ui.column().classes("w-full")

    def _confirm_delete_kb(doc_id: int, filename: str) -> None:
        with ui.dialog() as dlg, ui.card():
            ui.label(f"Delete KB document #{doc_id}?").classes("text-base font-medium")
            ui.label(filename).classes("text-sm opacity-70")
            ui.label(
                "Removes the file, all associated profile suggestions, and the row. "
                "Approved suggestions already applied to the profile remain."
            ).classes("text-xs opacity-60 pt-2")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                ui.button("Cancel", on_click=dlg.close).props("flat")

                def do_delete() -> None:
                    with session_scope() as db:
                        result = delete_kb_document(db, doc_id)
                    dlg.close()
                    if result.get("deleted"):
                        ui.notify(f"Deleted KB document #{doc_id}.", type="positive")
                        refresh_list()
                    else:
                        ui.notify(f"Could not delete: {result.get('reason')}", type="negative")

                ui.button("Delete", on_click=do_delete, icon="delete").props("color=negative")
        dlg.open()

    # Track expansion elements so we can expand/collapse all, AND
    # which classes were open at last render so re-render preserves state.
    expansion_refs: list = []
    expanded_classes: set[str] = set()

    def refresh_list() -> None:
        list_container.clear()
        expansion_refs.clear()
        with SessionLocal() as db:
            docs = (
                db.execute(select(KnowledgeBaseDocument).order_by(KnowledgeBaseDocument.created_at.desc()))
                .scalars()
                .all()
            )
            snapshot = [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "cls": d.document_class.value
                    if hasattr(d.document_class, "value")
                    else str(d.document_class),
                    "tags": d.tags_json or [],
                    "status": d.status.value if hasattr(d.status, "value") else str(d.status),
                    "char_count": len(d.extracted_text_md or ""),
                }
                for d in docs
            ]
            pending_by_doc: dict[int, int] = {}
            for (did,) in db.execute(
                select(ProfileSuggestion.kb_document_id).where(ProfileSuggestion.status == "pending")
            ).all():
                pending_by_doc[did] = pending_by_doc.get(did, 0) + 1

        with list_container:
            if not snapshot:
                ui.label(
                    "No KB documents yet. Add capability statement, bios, prior "
                    "proposals (correctly classified), and agency context here."
                ).classes("text-sm opacity-60")
                return

            # Group by class.
            by_class: dict[str, list[dict]] = {}
            for s in snapshot:
                by_class.setdefault(s["cls"], []).append(s)

            # Order classes per the legend grouping; unknown classes go last.
            ordered: list[str] = []
            for group in _KB_CLASS_GROUPS:
                for c in group["classes"]:
                    if c in by_class and c not in ordered:
                        ordered.append(c)
            for c in by_class:
                if c not in ordered:
                    ordered.append(c)

            # Header row with class count + expand/collapse controls + Refresh.
            total_pending = sum(pending_by_doc.values())

            def expand_all() -> None:
                expanded_classes.update(ordered)
                for e in expansion_refs:
                    e.value = True

            def collapse_all() -> None:
                expanded_classes.clear()
                for e in expansion_refs:
                    e.value = False

            with ui.row().classes("items-center justify-between w-full"):
                bits = [f"{len(snapshot)} document{'s' if len(snapshot) != 1 else ''}"]
                if total_pending:
                    bits.append(f"{total_pending} pending suggestion{'s' if total_pending != 1 else ''}")
                ui.label(" · ".join(bits)).classes("text-sm opacity-70")
                with ui.row().classes("gap-1"):
                    ui.button("Expand all", icon="unfold_more", on_click=expand_all).props(
                        "flat dense size=sm"
                    )
                    ui.button("Collapse all", icon="unfold_less", on_click=collapse_all).props(
                        "flat dense size=sm"
                    )
                    ui.button("Refresh", icon="refresh", on_click=refresh_list).props("flat dense size=sm")

            def _track_toggle(cls_name: str, e_args) -> None:
                # NiceGUI's ui.expansion fires update:model-value with the new boolean.
                # We capture it so the open/closed state survives re-renders.
                is_open = bool(getattr(e_args, "value", False))
                if is_open:
                    expanded_classes.add(cls_name)
                else:
                    expanded_classes.discard(cls_name)

            for cls in ordered:
                docs_in = by_class[cls]
                label, _desc = _KB_CLASS_DESCRIPTIONS.get(cls, (cls, ""))
                n_pending_cls = sum(pending_by_doc.get(d["id"], 0) for d in docs_in)

                header_text = f"{label}  ({len(docs_in)})"
                if n_pending_cls:
                    header_text += f"  · {n_pending_cls} suggestion{'s' if n_pending_cls != 1 else ''}"

                is_open = cls in expanded_classes
                exp = ui.expansion(
                    header_text,
                    icon="folder",
                    value=is_open,
                    on_value_change=lambda e, c=cls: _track_toggle(c, e),
                ).classes("w-full")
                expansion_refs.append(exp)
                with exp:
                    for s in docs_in:
                        _render_kb_doc_row(s, pending_by_doc.get(s["id"], 0))

    def _render_kb_doc_row(s: dict, n_pending: int) -> None:
        icon, color_cls = _KB_STATUS_VISUAL.get(
            s["status"], ("help_outline", "text-slate-500")
        )
        document_row = ui.row().classes(
            "items-center w-full gap-3 py-1 px-2 rounded hover:bg-slate-50"
        )
        document_row.props["data-kb-document-id"] = str(s["id"])
        with document_row:
            ui.icon(icon).classes(color_cls)
            with ui.column().classes("gap-0 flex-1"):
                ui.label(s["filename"]).classes("font-medium text-sm")
                meta_parts: list[str] = []
                if s["tags"]:
                    meta_parts.append(", ".join(s["tags"]))
                if s["char_count"]:
                    meta_parts.append(f"{s['char_count']:,} chars extracted")
                if meta_parts:
                    ui.label(" · ".join(meta_parts)).classes("text-xs opacity-60")
            if n_pending > 0:
                ui.chip(
                    f"{n_pending} suggestion{'s' if n_pending != 1 else ''}",
                    icon="rule_folder",
                    on_click=lambda: ui.navigate.to("/config?tab=suggestions"),
                ).props("color=amber-3 text-color=black clickable").classes("cursor-pointer")
            ui.label(f"#{s['id']}").classes("text-xs opacity-50 font-mono")
            delete_button = ui.button(
                icon="delete_outline",
                on_click=lambda e, did=s["id"], fn=s["filename"]: _confirm_delete_kb(did, fn),
            ).props("flat dense color=negative")
            delete_button.props["aria-label"] = f"Delete KB document #{s['id']}"

    refresh_list()
    # Slow background refresh so ingestion progress shows up without manual reload.
    ui.timer(5.0, refresh_list)

    ui.separator()
    _render_class_legend()


# ----- Config -------------------------------------------------------------------


@ui.page("/config")
def config_page(request: Request) -> None:
    """`tab` query parameter (e.g. /config?tab=suggestions) deep-links into a
    specific tab. Valid values: profile | suggestions | pricing | models | costs.
    NiceGUI 3.x doesn't auto-bind query params to function args, so we read
    them off the Request object explicitly.
    """
    tab = request.query_params.get("tab", "profile")
    if tab not in {"profile", "suggestions", "decisions", "pricing", "models", "costs"}:
        tab = "profile"
    with page_frame("Configuration"):
        if get_settings().is_demo:
            _empty_state(
                "Configuration is locked in the curated demo workspace.",
                icon="lock",
            )
            return

        with ui.tabs().classes("w-full") as tabs:
            ui.tab("profile", label="Company Profile", icon="business")
            ui.tab("suggestions", label="Pending Profile Updates", icon="rule_folder")
            ui.tab("decisions", label="Decisions", icon="history_edu")
            ui.tab("pricing", label="Pricing Rules", icon="payments")
            ui.tab("models", label="Models", icon="psychology")
            ui.tab("costs", label="Cost Caps", icon="account_balance_wallet")

        with ui.tab_panels(tabs, value=tab).classes("w-full"):
            with ui.tab_panel("profile"):
                profile = get_company_profile()
                meta = profile.get("_meta", {})
                ui.label(
                    f"Version {meta.get('version', '?')} · effective {meta.get('effective_from', '?')}"
                ).classes("text-base font-medium")
                ui.label(profile.get("company", {}).get("legal_name", "")).classes("text-sm opacity-70")

                with ui.expansion("Certifications", icon="verified").classes("w-full"):
                    for cert in get_certifications():
                        ui.label(f"· {cert}")

                with ui.expansion("Clearances inventory", icon="lock_person").classes("w-full"):
                    cleared = get_clearances_inventory()
                    if not cleared:
                        ui.label("No active clearances.").classes("opacity-60")
                    for person in cleared:
                        ui.label(f"· {person['name']} ({person['role']}): {', '.join(person['clearances'])}")

                with ui.expansion("Deep specializations", icon="star").classes("w-full"):
                    for s in get_deep_specializations():
                        ui.label(f"· {s}")

                with ui.expansion("Capability areas", icon="construction").classes("w-full"):
                    for area in get_capability_areas():
                        ui.label(f"· {area.get('area')}").classes("font-medium")

                with ui.expansion(f"Key personnel ({len(get_key_personnel())})", icon="group").classes(
                    "w-full"
                ):
                    for p in get_key_personnel():
                        ui.label(f"· {p.get('name')} — {p.get('role')}")

                with ui.expansion("Labor rate card", icon="badge").classes("w-full"):
                    rate_card = get_labor_rate_card()
                    rows = [
                        {
                            "title": cat["title"],
                            "rate": f"${cat['hourly_rate_usd']:.2f}",
                            "min_years": cat["min_years"],
                        }
                        for cat in rate_card.get("categories", [])
                    ]
                    columns = [
                        {"name": "title", "label": "Category", "field": "title", "align": "left"},
                        {"name": "rate", "label": "Hourly", "field": "rate"},
                        {"name": "min_years", "label": "Min years", "field": "min_years"},
                    ]
                    ui.table(columns=columns, rows=rows, row_key="title").classes("w-full")

                ui.label(
                    "Edit company_profile.json in the active data workspace and "
                    "bump _meta.version. "
                    "Changes take effect after restart (or call reload_company_profile())."
                ).classes("text-xs opacity-60 pt-3")

            with ui.tab_panel("suggestions"):
                _render_pending_suggestions_tab()

            with ui.tab_panel("decisions"):
                _render_decisions_tab()

            with ui.tab_panel("pricing"):
                _empty_state(
                    "Pricing rules are active and loaded from "
                    "internal_pricing_rules.json in the active data workspace. "
                    "This view is read-only; "
                    "edit that file and restart the app to apply changes.",
                    icon="payments",
                )
            with ui.tab_panel("models"):
                _empty_state(
                    "Per-agent model assignments are active and loaded from .env, "
                    "with defaults in app/config.py. This view is read-only; restart "
                    "the app after changing .env.",
                    icon="psychology",
                )
            with ui.tab_panel("costs"):
                _empty_state(
                    "Per-run and monthly cost-cap values are loaded from .env. "
                    "They are advisory and are not currently enforced. Proposal-level "
                    "LLM spend is available in each proposal's Spend tab.",
                    icon="account_balance_wallet",
                )


# ---------- Pending Profile Updates -----------------------------------------

# Fields shown in friendly-named order. Anything not listed falls back to
# str(field). For lists we join with commas. For ints/floats we cast to str.
_PERSON_FIELDS: list[tuple[str, str]] = [
    ("name", "Name"),
    ("role", "Role"),
    ("years_experience", "Years experience"),
    ("location", "Location"),
    ("veteran_status", "Veteran status"),
    ("certifications", "Certifications"),
    ("clearances", "Clearances"),
    ("strengths", "Strengths"),
    ("tech", "Tech"),
    ("past_roles", "Past roles"),
    ("education", "Education"),
]
_PROJECT_FIELDS: list[tuple[str, str]] = [
    ("project", "Project"),
    ("customer", "Customer"),
    ("role", "Quadratic's role"),
    ("status", "Status"),
    ("scope", "Scope"),
    ("tags", "Tags"),
    ("citation_class", "Citation class"),
]
_FIELD_LABELS: dict[str, dict[str, str]] = {
    "key_personnel": dict(_PERSON_FIELDS),
    "past_performance": dict(_PROJECT_FIELDS),
}


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        if not value:
            return "(none)"
        return ", ".join(str(x) for x in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def _kv_row(label: str, value: str, *, label_classes: str = "") -> None:
    with ui.row().classes("gap-3 text-sm w-full items-start"):
        ui.label(label).classes(f"font-medium opacity-70 w-40 shrink-0 {label_classes}")
        ui.label(value).classes("flex-1 whitespace-pre-wrap")


def _render_object_summary(obj: dict, fields: list[tuple[str, str]]) -> None:
    """Pretty-print a person/project dict using the field-order map. Skips
    empty values so the panel doesn't show 'None' rows."""
    if not isinstance(obj, dict):
        ui.label(_fmt(obj)).classes("text-sm")
        return
    rendered_any = False
    for key, label in fields:
        if key not in obj:
            continue
        val = obj[key]
        if val is None or val == "" or val == []:
            continue
        _kv_row(label, _fmt(val))
        rendered_any = True
    if not rendered_any:
        ui.label("(no fields)").classes("text-sm opacity-50")


def _render_object_changes(current: dict, updates: dict, fields: list[tuple[str, str]]) -> None:
    """For a merge op: show field-by-field what's added or changed.
    + = adding to a list or filling an empty field
    ~ = changing an existing scalar value
    """
    if not isinstance(updates, dict):
        return
    current = current if isinstance(current, dict) else {}

    rendered_any = False
    for key, label in fields:
        if key not in updates:
            continue
        new_val = updates[key]
        old_val = current.get(key)

        if isinstance(new_val, list):
            additions = [x for x in (new_val or []) if x not in (old_val or [])]
            if additions:
                _kv_row(
                    f"+ {label}",
                    "Add: " + ", ".join(str(a) for a in additions),
                    label_classes="text-green-700",
                )
                rendered_any = True
        else:
            if old_val in (None, ""):
                _kv_row(
                    f"+ {label}",
                    f"Set to: {_fmt(new_val)}",
                    label_classes="text-green-700",
                )
                rendered_any = True
            elif old_val != new_val:
                _kv_row(
                    f"~ {label}",
                    f"{_fmt(old_val)} → {_fmt(new_val)}",
                    label_classes="text-amber-700",
                )
                rendered_any = True
    if not rendered_any:
        ui.label("(no field changes detected)").classes("text-sm opacity-50")


def _render_human_diff(s: dict) -> None:
    """Render the suggestion's effect in human language, not JSON."""
    op = s["operation"]
    section = s["section"]
    proposed = s["proposed"]
    current = s["current"]

    # Object-list sections — key_personnel, past_performance
    if section in ("key_personnel", "past_performance"):
        fields = _PERSON_FIELDS if section == "key_personnel" else _PROJECT_FIELDS
        if op == "append":
            ui.label("Currently: not in profile").classes("text-sm opacity-70 pb-1")
            ui.label("Will be added:").classes("text-sm font-medium pt-2")
            _render_object_summary(proposed if isinstance(proposed, dict) else {}, fields)
            return
        if op == "merge":
            cur_obj = current if isinstance(current, dict) else {}
            name = cur_obj.get("name") or cur_obj.get("project") or s.get("match_key") or ""
            ui.label(f"Existing entry — {name}:").classes("text-sm font-medium")
            _render_object_summary(cur_obj, fields)
            ui.separator().classes("my-2")
            ui.label("Proposed updates:").classes("text-sm font-medium")
            _render_object_changes(cur_obj, proposed if isinstance(proposed, dict) else {}, fields)
            return

    # NAICS — section "naics.opportunistic" etc; current is the whole naics dict
    if section.startswith("naics."):
        bucket = section.split(".", 1)[1]
        existing_bucket = (current or {}).get(bucket, []) if isinstance(current, dict) else []
        ui.label(f"Currently in NAICS '{bucket}': {_fmt(existing_bucket)}").classes("text-sm opacity-70")
        _kv_row("+ Add NAICS code", str(proposed), label_classes="text-green-700")
        return

    # Simple list-of-strings sections — certifications, deep_specializations,
    # differentiators_for_proposals.
    if op == "append":
        if isinstance(current, list):
            count = len(current)
            ui.label(f"Currently {count} item{'s' if count != 1 else ''} in '{section}'.").classes(
                "text-sm opacity-70"
            )
            if current:
                with ui.expansion(f"Show current ({count})", icon="visibility").classes("w-full"):
                    ui.label(_fmt(current)).classes("text-sm whitespace-pre-wrap")
        else:
            ui.label(f"Currently nothing recorded under '{section}'.").classes("text-sm opacity-70")
        _kv_row("+ Add", str(proposed), label_classes="text-green-700")
        return

    # Fallback for any op/section we haven't given special handling.
    _kv_row("Section", section)
    _kv_row("Operation", op)
    _kv_row("Proposed", _fmt(proposed))
    if current:
        _kv_row("Current", _fmt(current))


def _render_decisions_tab() -> None:
    """Cross-RFP decisions ledger — view and delete entries.

    Decisions are added via the 'Remember this decision' checkbox in the
    gap notes dialog. The Shortfall Strategist reads this entire ledger
    on every run.
    """
    container = ui.column().classes("w-full gap-2")

    @ui.refreshable
    def render() -> None:
        from app.core.decisions import (
            DECISIONS_PATH,
            delete_decision,
            get_decisions_list,
            reload_decisions,
        )

        reload_decisions()
        decisions = get_decisions_list()

        ui.label(
            "Cross-RFP memory of how Quadratic resolves recurring gaps. The "
            "Shortfall Strategist reads every entry on every run and applies "
            "matching decisions to similar gaps in new RFPs."
        ).classes("text-sm opacity-70")
        ui.label(f"Stored at: {DECISIONS_PATH.relative_to(DECISIONS_PATH.parent.parent)}").classes(
            "text-xs opacity-50 font-mono"
        )

        if not decisions:
            ui.label(
                "No decisions recorded yet. Open a gap on a Proposal Review page, "
                "click 'Add notes' or 'Edit notes', and tick "
                "'Remember this decision for future similar gaps' when saving."
            ).classes("text-sm opacity-60 py-4")
            return

        ui.label(f"{len(decisions)} decision{'s' if len(decisions) != 1 else ''}").classes(
            "text-base font-medium pt-2"
        )

        for d in decisions:
            with ui.card().classes("w-full"):
                with ui.row().classes("items-start w-full gap-3"):
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(d.get("topic", "(no topic)")).classes("text-base font-medium")
                        meta_bits = [d.get("id", "?")]
                        if d.get("established_on"):
                            meta_bits.append(f"established {d['established_on']}")
                        if d.get("source_proposal_id"):
                            src = f"from proposal #{d['source_proposal_id']}"
                            if d.get("source_gap_id"):
                                src += f" · {d['source_gap_id']}"
                            meta_bits.append(src)
                        ui.label(" · ".join(meta_bits)).classes("text-xs font-mono opacity-60")
                        ui.label("Decision:").classes("text-xs opacity-60 pt-2")
                        ui.label(d.get("decision", "")).classes("text-sm whitespace-pre-wrap")
                        if d.get("applies_to_gaps_like"):
                            ui.label("Applies to gaps like:").classes("text-xs opacity-60 pt-2")
                            ui.label(d["applies_to_gaps_like"]).classes("text-xs italic opacity-80")

                    decision_id = d.get("id")
                    decision_topic = d.get("topic", "")

                    def _confirm_delete(did=decision_id, topic=decision_topic):
                        with ui.dialog() as dlg, ui.card():
                            ui.label(f"Delete decision {did}?").classes("text-base font-medium")
                            ui.label(topic).classes("text-sm opacity-70")
                            ui.label(
                                "Future shortfall runs will no longer see this "
                                "decision. Existing proposals are not affected."
                            ).classes("text-xs opacity-60 pt-2")
                            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                                ui.button("Cancel", on_click=dlg.close).props("flat")

                                def go() -> None:
                                    ok = delete_decision(did)
                                    dlg.close()
                                    if ok:
                                        ui.notify(f"Deleted {did}.", type="positive")
                                        render.refresh()
                                    else:
                                        ui.notify("Could not delete.", type="negative")

                                ui.button("Delete", icon="delete", on_click=go).props("color=negative")
                        dlg.open()

                    delete_button = ui.button(
                        icon="delete_outline", on_click=_confirm_delete
                    ).props("flat dense color=negative")
                    delete_label = f"Delete decision {d.get('id', '?')}"
                    if d.get("topic"):
                        delete_label += f": {d['topic']}"
                    delete_button.props["aria-label"] = delete_label

    with container:
        render()


def _render_pending_suggestions_tab() -> None:
    """Pending Profile Updates — review queue from KB fact extraction.

    Auto-refresh deliberately omitted: the show-diff expansion is stateful
    and gets recreated by re-render, which would close it. Approving or
    rejecting refreshes manually after the action completes.
    """
    container = ui.column().classes("w-full gap-3")

    def render() -> None:
        container.clear()
        with SessionLocal() as db:
            suggestions = db.execute(
                select(ProfileSuggestion, KnowledgeBaseDocument)
                .join(
                    KnowledgeBaseDocument,
                    KnowledgeBaseDocument.id == ProfileSuggestion.kb_document_id,
                )
                .where(ProfileSuggestion.status == "pending")
                .order_by(ProfileSuggestion.created_at.asc())
            ).all()
            snapshot = [
                {
                    "id": s.id,
                    "summary": s.summary,
                    "rationale": s.rationale,
                    "operation": s.operation,
                    "section": s.section,
                    "match_key": s.match_key,
                    "proposed": s.proposed_value_json,
                    "current": s.current_value_json,
                    "doc_filename": d.filename,
                    "doc_id": d.id,
                    "doc_class": d.document_class.value
                    if hasattr(d.document_class, "value")
                    else str(d.document_class),
                }
                for s, d in suggestions
            ]

        with container:
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(f"{len(snapshot)} pending update{'s' if len(snapshot) != 1 else ''}").classes(
                    "text-base font-medium"
                )
                ui.button("Refresh", icon="refresh", on_click=render).props("flat dense")

            if not snapshot:
                ui.label("No pending profile updates.").classes("text-sm opacity-60 py-4")
                ui.label(
                    "Suggestions appear here after the fact-extraction agent runs on "
                    "newly-uploaded KB documents (corporate, personnel, past_performance_*)."
                ).classes("text-xs opacity-50")
                return

            ui.label(
                "Approving writes back to company_profile.json in the active "
                "data workspace with a version bump. "
                "Rejecting just dismisses; the source KB document is unchanged either way."
            ).classes("text-xs opacity-60")

            for s in snapshot:
                _render_suggestion_card(s, render)

    def _render_suggestion_card(s: dict, on_change) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-start w-full gap-3"):
                with ui.column().classes("flex-1 gap-1"):
                    ui.label(s["summary"]).classes("text-base font-medium")
                    ui.label(f"From: {s['doc_filename']} ({s['doc_class']})").classes("text-xs opacity-60")
                    if s["rationale"]:
                        ui.label(s["rationale"]).classes("text-xs opacity-70 italic")

                    with ui.expansion("Show details", icon="difference").classes("w-full"):
                        _render_human_diff(s)

                with ui.column().classes("gap-2"):

                    def on_approve(sid=s["id"]):
                        with session_scope() as db:
                            result = apply_suggestion(db, sid)
                        if result.get("applied"):
                            ui.notify(
                                f"Applied → profile v{result['new_version']}",
                                type="positive",
                            )
                            on_change()
                        else:
                            ui.notify(
                                f"Could not apply: {result.get('error')}",
                                type="negative",
                            )

                    def on_reject(sid=s["id"]):
                        with session_scope() as db:
                            ok = reject_suggestion(db, sid)
                        ui.notify(
                            "Rejected." if ok else "Could not reject.",
                            type="positive" if ok else "negative",
                        )
                        on_change()

                    ui.button("Approve", icon="check", on_click=on_approve).props("color=positive size=sm")
                    ui.button("Reject", icon="close", on_click=on_reject).props("color=negative flat size=sm")

    render()


# ----- Admin --------------------------------------------------------------------


@ui.page("/admin")
def admin() -> None:
    with page_frame("Admin"):
        with SessionLocal() as db:
            n_proposals = db.execute(select(Proposal)).scalars().all()
            n_kb = db.execute(select(KnowledgeBaseDocument)).scalars().all()

        with ui.row().classes("gap-4 w-full"):
            with ui.card().classes("flex-1"):
                ui.label("Proposals").classes("text-sm opacity-70")
                ui.label(str(len(n_proposals))).classes("text-3xl font-semibold")
            with ui.card().classes("flex-1"):
                ui.label("KB documents").classes("text-sm opacity-70")
                ui.label(str(len(n_kb))).classes("text-3xl font-semibold")
            with ui.card().classes("flex-1"):
                ui.label("Profile version").classes("text-sm opacity-70")
                from app.core.company_profile import get_profile_version

                ui.label(get_profile_version()).classes("text-3xl font-semibold")

        ui.separator()
        ui.label(
            "This is a read-only installation summary. Open a proposal's Run "
            "Progress page for run history and errors, or its Spend tab for "
            "LLM cost details."
        ).classes(
            "text-sm opacity-60"
        )

        ui.separator()
        ui.label("Proposal status reference").classes("text-base font-medium pt-2")
        with ui.row().classes("flex-wrap gap-2"):
            for s in ProposalStatus:
                ui.chip(s.value).props("color=blue-grey-3 text-color=black")
