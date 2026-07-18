"""Proposal Review > Completed Draft tab.

User-facing review surface for the finished proposal: a letter-page-
styled preview of the full proposal as a single scrollable document,
plus DOCX export controls (download, copy markdown, refresh, options
for what to include).

The Final Polish tab's modal is the quick-peek path; this tab is the
work-area for the actual export.
"""

from __future__ import annotations

import logging

from nicegui import ui

from app.db.session import SessionLocal
from app.models import Proposal
from app.ui._shared import _extract_section_markdown

log = logging.getLogger(__name__)


def _render_completed_draft_tab(proposal_id: int) -> None:
    """Completed Draft tab — the user-facing review surface for the
    finished proposal. Always-on letter-page-styled preview of the
    full proposal as a single scrollable document, plus DOCX export
    controls. The Final Polish tab's modal is the quick-peek path;
    this tab is the work-area for the actual export.

    Layout:
      - Header card: status summary + DOCX export controls (Download
        DOCX, Copy markdown, Refresh, options for what to include).
      - Empty state when no drafts exist.
      - Letter-page-styled rendered preview below, page-paddinged so
        the on-screen view matches the printed deliverable's layout.
    """
    from app.services.export import compile_proposal_to_docx
    from app.services.sections import compile_proposal_markdown
    from app.services.submission_commitments import (
        get_submission_checklist_snapshot,
    )

    # Per-tab options state — survives @ui.refreshable rebuilds
    # because it lives at the outer scope.
    options: dict = {
        "include_submission_checklist": True,
        "include_cost_deferred": True,
    }

    @ui.refreshable
    def render() -> None:
        # Pull proposal title once for the export filename + page
        # title bar.
        proposal_title = ""
        with SessionLocal() as db:
            p = db.get(Proposal, proposal_id)
            if p:
                proposal_title = (p.title or "").strip()

        result = compile_proposal_markdown(
            proposal_id,
            include_cost_deferred=options["include_cost_deferred"],
        )
        md = result["markdown"]
        sections = result["sections_included"]
        skipped = result["sections_skipped"]
        total_chars = result["total_chars"]

        # ---- Header card -----------------------------------------
        with ui.card().classes("w-full"):
            with ui.row().classes("items-start justify-between w-full gap-3 flex-wrap"):
                with ui.column().classes("gap-0 flex-1"):
                    ui.label("Completed Draft").classes("text-xl font-semibold")
                    if proposal_title:
                        ui.label(proposal_title).classes("text-sm opacity-80 pt-1")
                    summary_bits = [
                        f"{len(sections)} section{'s' if len(sections) != 1 else ''}",
                        f"{total_chars:,} chars",
                    ]
                    if skipped:
                        summary_bits.append(f"{len(skipped)} skipped")
                    if options["include_submission_checklist"]:
                        # Pull the checklist totals so the user sees
                        # "+26 pending submission items" before
                        # downloading.
                        try:
                            cl = get_submission_checklist_snapshot(
                                proposal_id,
                            )
                            pending = cl["totals"].get(
                                "all_obtained_pending",
                                0,
                            )
                            if pending:
                                summary_bits.append(
                                    f"{pending} checklist item"
                                    f"{'s' if pending != 1 else ''} "
                                    f"pending in appendix"
                                )
                            else:
                                summary_bits.append("checklist appendix included")
                        except Exception:
                            log.exception(
                                "completed_draft tab: checklist snapshot failed (non-fatal)",
                            )
                    ui.label(" · ".join(summary_bits)).classes("text-xs opacity-60 pt-2")

                # Right-side: DOCX actions + options.
                with ui.column().classes("gap-2 items-stretch"):
                    if not sections:
                        # No drafts → no actions yet.
                        ui.label("Drafts not ready").classes("text-xs opacity-50")
                    else:

                        def _download_docx() -> None:
                            try:
                                data, filename, summary = compile_proposal_to_docx(
                                    proposal_id,
                                    proposal_title=(proposal_title or None),
                                    include_submission_checklist=(options["include_submission_checklist"]),
                                    include_cost_deferred=(options["include_cost_deferred"]),
                                )
                            except Exception as exc:
                                log.exception(
                                    "DOCX export failed for proposal %d",
                                    proposal_id,
                                )
                                ui.notify(
                                    f"DOCX export failed: {type(exc).__name__}: {str(exc)[:120]}",
                                    type="negative",
                                    multi_line=True,
                                    timeout=6000,
                                )
                                return
                            ui.download(data, filename=filename)
                            chk = summary.get("submission_checklist") or {}
                            chk_msg = ""
                            if chk:
                                chk_msg = (
                                    f" + checklist appendix ({chk.get('all_obtained_pending', 0)} pending)"
                                )
                            filename_note = (
                                f" Filename follows RFP convention: '{filename}'."
                                if summary.get("filename_source") == "rfp"
                                else ""
                            )
                            ui.notify(
                                f"DOCX export sent ("
                                f"{summary['byte_count']:,} bytes, "
                                f"{summary['total_sections']} "
                                f"section"
                                f"{'s' if summary['total_sections'] != 1 else ''}"
                                f"{chk_msg}). Check your downloads."
                                f"{filename_note}",
                                type="positive",
                                multi_line=True,
                                timeout=5000,
                            )

                        ui.button(
                            "Download DOCX",
                            icon="download",
                            on_click=_download_docx,
                        ).props("color=primary")

                        def _copy_markdown() -> None:
                            safe = (
                                md.replace(
                                    "\\",
                                    "\\\\",
                                )
                                .replace(
                                    "`",
                                    "\\`",
                                )
                                .replace("$", "\\$")
                            )
                            ui.run_javascript(f"navigator.clipboard.writeText(`{safe}`);")
                            ui.notify(
                                f"Copied {len(md):,} chars of markdown to clipboard.",
                                type="positive",
                                timeout=3000,
                            )

                        with ui.row().classes("gap-2"):
                            ui.button(
                                "Copy markdown",
                                icon="content_copy",
                                on_click=_copy_markdown,
                            ).props("flat dense size=sm")
                            ui.button(
                                "Refresh",
                                icon="refresh",
                                on_click=render.refresh,
                            ).props("flat dense size=sm").tooltip(
                                "Re-pulls the latest section drafts "
                                "from the database. Use after a "
                                "polish run / regenerate completes."
                            )

            # Options row — toggles control what the DOCX includes.
            if sections:
                with ui.row().classes("items-center gap-4 pt-3 flex-wrap"):
                    ui.label("DOCX export options:").classes("text-xs uppercase opacity-60")
                    ui.switch(
                        "Include Submission Checklist appendix",
                        value=options["include_submission_checklist"],
                        on_change=lambda e: (
                            options.update(
                                include_submission_checklist=bool(
                                    e.value,
                                ),
                            ),
                            render.refresh(),
                        ),
                    ).tooltip(
                        "When ON, the DOCX ends with an appendix "
                        "listing every RFP-required form, user-"
                        "tracked commitment, and system-verified "
                        "readiness check the submitter must address."
                    )
                    ui.switch(
                        "Include cost-deferred sections",
                        value=options["include_cost_deferred"],
                        on_change=lambda e: (
                            options.update(
                                include_cost_deferred=bool(e.value),
                            ),
                            render.refresh(),
                        ),
                    ).tooltip(
                        "When ON, the cost-narrative section "
                        "(SEC-009 / Cost Proposal) is included in "
                        "both the on-screen preview and the DOCX. "
                        "Turn off for a technical-volume-only export."
                    )

        # ---- Empty state when no drafts -------------------------
        if not sections:
            with ui.column().classes("items-center justify-center w-full py-12 gap-3"):
                ui.icon("article", size="xl").classes("opacity-60")
                ui.label("No drafted sections yet.").classes("text-base opacity-80 text-center")
                ui.label(
                    "Once the Writer Team finishes drafting all "
                    "sections, the completed proposal renders here "
                    "with controls for DOCX export."
                ).classes("text-sm opacity-60 text-center max-w-xl")
            return

        # ---- Skipped-sections banner ----------------------------
        if skipped:
            with ui.row().classes(
                "items-start gap-2 py-2 px-3 bg-amber-50 border-l-4 border-amber-400 rounded mt-2"
            ):
                ui.icon("info").classes("text-amber-700 pt-0.5")
                with ui.column().classes("gap-0 flex-1"):
                    ui.label(
                        f"{len(skipped)} section"
                        f"{'s' if len(skipped) != 1 else ''} "
                        f"not in this preview "
                        f"(undrafted or excluded):"
                    ).classes("text-xs font-medium text-amber-900")
                    skip_lines = [f"{s['section_id']} — {s['reason']}" for s in skipped]
                    ui.label(" · ".join(skip_lines)).classes("text-xs text-amber-800 font-mono")

        # ---- Table of contents ----------------------------------
        if len(sections) > 1:
            with ui.expansion(
                f"Table of contents ({len(sections)} sections)",
                icon="list",
                value=False,
            ).classes("w-full mt-2"):
                for s in sections:
                    with (
                        ui.row()
                        .classes("items-center gap-2 pt-1 cursor-pointer hover:bg-slate-50 px-2 rounded")
                        .on(
                            "click",
                            lambda sid=s["section_id"]: ui.run_javascript(
                                f'document.getElementById("cd-{sid}")'
                                f'.scrollIntoView({{behavior: "smooth", '
                                f'block: "start"}});'
                            ),
                        )
                    ):
                        ui.label(f"#{s['section_order']}").classes("text-xs font-mono opacity-50 w-8")
                        ui.label(s["section_id"]).classes(
                            "text-xs font-mono px-1.5 py-0.5 bg-slate-100 rounded"
                        )
                        ui.label(s["section_title"]).classes("text-xs flex-1")
                        if s["is_cost_deferred"]:
                            ui.chip(
                                "Cost section",
                                icon="payments",
                            ).props("color=purple-2 text-color=purple-9 size=sm dense")
                        ui.label(f"{s['char_count']:,} ch · rev {s['revision']}").classes(
                            "text-xs opacity-50 font-mono"
                        )

        # ---- Page-styled rendered preview ----------------------
        # Letter page width: 8.5" = 816px @ 96 DPI; 1" inner padding
        # mirrors the DOCX margins so on-screen layout matches the
        # printed deliverable. Slate-100 background outside the page
        # evokes a print-preview look. NOT scroll-area-wrapped here
        # (vs the modal version) — let the page itself scroll, which
        # gives the user a much taller reading window inside the tab.
        with (
            ui.element("div")
            .classes("mx-auto bg-white shadow-lg my-4")
            .style(
                "width: min(816px, 100%); "
                "padding: 96px 96px 96px 96px;"
                "font-family: Calibri, 'Segoe UI', Arial, sans-serif;"
            )
        ):
            for s in sections:
                with ui.element("div").props(f'id="cd-{s["section_id"]}"').classes("w-full pt-4 first:pt-0"):
                    with ui.row().classes("items-baseline gap-2 pb-1 border-b border-slate-200"):
                        ui.label(s["section_id"]).classes(
                            "text-xs font-mono px-1.5 py-0.5 bg-slate-100 rounded"
                        )
                        ui.label(s["section_title"]).classes("text-xl font-semibold").style("color: #1F3A5F;")
                        if s["is_cost_deferred"]:
                            ui.chip(
                                "Cost section",
                                icon="payments",
                            ).props("color=purple-2 text-color=purple-9 size=sm dense")
                        ui.label(f"rev {s['revision']} · {s['char_count']:,} chars").classes(
                            "text-xs opacity-50 font-mono ml-auto"
                        )
                    section_md = _extract_section_markdown(
                        md,
                        s["section_id"],
                        s["section_title"],
                    )
                    ui.markdown(section_md).classes("prose prose-sm max-w-none pt-2").style(
                        "font-size: 11pt; line-height: 1.5;"
                    )

    render()
