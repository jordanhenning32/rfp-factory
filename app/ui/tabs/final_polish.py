"""Proposal Review > Final Polish tab.

Cross-section consistency cleanup. Reads every drafted section as one
corpus, detects drift (FTE mismatches, terminology, naming,
commitments, voice), then auto-applies each fix surgically — no
per-issue triage clicks.

Includes the "View final draft" modal preview (`_open_final_draft_dialog`)
that renders every drafted section concatenated into a letter-page-
styled scroll area, with Download DOCX + Copy Markdown shortcuts.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from nicegui import ui
from sqlalchemy import select

from app.db.session import SessionLocal
from app.jobs.final_polish import spawn_final_polish
from app.models import Proposal, ProposalSection
from app.ui._shared import _extract_section_markdown

log = logging.getLogger(__name__)


# Issue types the detector reports — used to drive the "what this
# catches" guidance. Mirrors `_ISSUE_TYPES` in
# app/agents/final_polish_detector.py.
_POLISH_ISSUE_TYPE_LABELS = {
    "numerical_drift": (
        "Numerical drift",
        "FTE / hours / dollars / percentages that disagree across sections.",
    ),
    "terminology_drift": (
        "Terminology drift",
        'different terms for the same concept used inconsistently ("platform" vs "system" vs "solution").',
    ),
    "voice_drift": (
        "Voice drift",
        "one section's tone clashes with the rest (formal/passive vs "
        "informal/active, marketing fluff vs measured prose).",
    ),
    "commitment_conflict": (
        "Commitment conflict",
        "conflicting promises across sections — SLAs, response times, deliverable counts, reporting cadence.",
    ),
    "redundant_repetition": (
        "Redundant repetition",
        "the same point made in 3+ sections in similar wording.",
    ),
    "naming_inconsistency": (
        "Naming inconsistency",
        "company / product / role / personnel names spelled or rendered differently across sections.",
    ),
}


def _open_final_draft_dialog(proposal_id: int) -> None:
    """Modal: show every drafted section as one scrollable rendered-
    markdown document — the polished proposal as an evaluator would
    read it. Pulls fresh from DB on open, so the latest polish edits
    are always reflected.

    Header has copy-to-clipboard (full markdown source) and Download
    DOCX (full proposal as Word doc) buttons so the user can move
    the rendered output into review tooling without leaving the page.

    Content area is constrained to letter-page width (8.5" with 1"
    margins → ~6.5" content ≈ 720px at 96 DPI) inside a wider modal
    frame, so the on-screen preview matches the printed deliverable's
    line breaks and reading rhythm. The white "page" sits on a slate
    background to evoke a print-preview look.
    """
    from app.services.export import compile_proposal_to_docx
    from app.services.sections import compile_proposal_markdown

    result = compile_proposal_markdown(proposal_id)
    md = result["markdown"]
    sections = result["sections_included"]
    skipped = result["sections_skipped"]
    total_chars = result["total_chars"]

    # Pull the proposal's title so the DOCX export filename and
    # cover-line both match what the user sees in headers elsewhere.
    proposal_title = ""
    try:
        with SessionLocal() as db:
            p = db.get(Proposal, proposal_id)
            if p:
                proposal_title = (p.title or "").strip()
    except Exception:
        log.exception("final draft preview: failed to read proposal title")

    with ui.dialog() as dialog, ui.card().classes("w-[min(95vw,1000px)] max-h-[90vh] bg-slate-100"):
        # Sticky header — title + sub-summary + close + copy.
        with ui.row().classes("items-center justify-between w-full pb-2 border-b border-slate-200"):
            with ui.column().classes("gap-0"):
                ui.label("Final Draft Preview").classes("text-lg font-semibold")
                ui.label(
                    f"{len(sections)} section"
                    f"{'s' if len(sections) != 1 else ''} · "
                    f"{total_chars:,} chars · "
                    "reflects the latest polish edits."
                ).classes("text-xs opacity-70")
            with ui.row().classes("gap-2 items-center"):
                # Download DOCX — primary export action. Compiled
                # fresh on click so it always reflects the latest
                # draft state (polish edits, manual edits, regens).
                def _download_docx() -> None:
                    try:
                        data, filename, summary = compile_proposal_to_docx(
                            proposal_id,
                            proposal_title=proposal_title or None,
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
                    filename_note = (
                        f" Filename follows RFP convention: '{filename}'."
                        if summary.get("filename_source") == "rfp"
                        else ""
                    )
                    ui.notify(
                        f"DOCX export ({summary['byte_count']:,} bytes, "
                        f"{summary['total_sections']} section"
                        f"{'s' if summary['total_sections'] != 1 else ''}) "
                        f"sent — check your browser's downloads."
                        f"{filename_note}",
                        type="positive",
                        multi_line=True,
                        timeout=5000,
                    )

                ui.button(
                    "Download DOCX",
                    icon="download",
                    on_click=_download_docx,
                ).props("color=primary dense size=sm").tooltip(
                    "Compiles every drafted section into a Word "
                    "document (.docx) with proper headings, lists, "
                    "and tables. Reflects the latest polish edits + "
                    "any manual revisions. If the RFP specifies a "
                    "filename convention, that's used instead of the "
                    "default slug."
                )

                # Copy-markdown — uses NiceGUI's clipboard helper if
                # present; falls back to a JS one-liner.
                def _copy_markdown() -> None:
                    # Quasar's $q.copyToClipboard via run_javascript is
                    # the simplest cross-platform path; ui.run_javascript
                    # is available in NiceGUI 3.x.
                    safe = (
                        md.replace("\\", "\\\\")
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

                ui.button(
                    "Copy markdown",
                    icon="content_copy",
                    on_click=_copy_markdown,
                ).props("flat dense size=sm")
                ui.button(
                    icon="close",
                    on_click=dialog.close,
                ).props("flat round dense size=sm")

        if skipped:
            with ui.row().classes(
                "items-start gap-2 py-2 px-3 bg-amber-50 border-l-4 border-amber-400 rounded mt-2"
            ):
                ui.icon("info").classes("text-amber-700 pt-0.5")
                with ui.column().classes("gap-0 flex-1"):
                    ui.label(
                        f"{len(skipped)} section"
                        f"{'s' if len(skipped) != 1 else ''} skipped "
                        f"(not yet drafted or excluded):"
                    ).classes("text-xs font-medium text-amber-900")
                    skip_lines = [f"{s['section_id']} — {s['reason']}" for s in skipped]
                    ui.label(" · ".join(skip_lines)).classes("text-xs text-amber-800 font-mono")

        # In-modal table of contents — clickable scroll-to-section
        # because 99K chars is a lot to scroll blind. Anchors target
        # `id="sec-{section_id}"` on each rendered section heading.
        if len(sections) > 1:
            with ui.expansion(
                f"Table of contents ({len(sections)} sections)",
                icon="list",
                value=False,
            ).classes("w-full pt-2"):
                for s in sections:
                    with (
                        ui.row()
                        .classes("items-center gap-2 pt-1 cursor-pointer hover:bg-slate-50 px-2 rounded")
                        .on(
                            "click",
                            lambda sid=s["section_id"]: ui.run_javascript(
                                f'document.getElementById("sec-{sid}")'
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
                            ui.chip("Cost section", icon="payments").props(
                                "color=purple-2 text-color=purple-9 size=sm dense"
                            )
                        ui.label(f"{s['char_count']:,} ch · rev {s['revision']}").classes(
                            "text-xs opacity-50 font-mono"
                        )

        # Body — scrollable area with a constrained letter-page-width
        # white "page" centered inside. The slate-100 modal background
        # bleeds through on either side, evoking a print-preview view.
        # Page width: 8.5" - 2×1" margins ≈ 6.5" content ≈ 720px @ 96 DPI.
        # We size the page itself at 8.5" (816px) and use 1" inner
        # padding so the visual matches what a Word printout would.
        with ui.scroll_area().classes("w-full mt-2").style("height: calc(90vh - 220px);"):
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
                    # Anchor wrapper for the TOC scroll-to.
                    with (
                        ui.element("div")
                        .props(f'id="sec-{s["section_id"]}"')
                        .classes("w-full pt-4 first:pt-0")
                    ):
                        with ui.row().classes("items-baseline gap-2 pb-1 border-b border-slate-200"):
                            ui.label(s["section_id"]).classes(
                                "text-xs font-mono px-1.5 py-0.5 bg-slate-100 rounded"
                            )
                            ui.label(s["section_title"]).classes("text-xl font-semibold").style(
                                "color: #1F3A5F;"  # navy — matches
                                # docx Heading 1 color
                            )
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

    dialog.open()


def _render_final_polish_tab(proposal_id: int) -> None:
    """Final Polish tab — surfaces the auto-apply cross-section
    consistency pass as its own surface (instead of being a button
    buried in the Draft tab header).

    Layout:
      - Status card: how many sections are drafted, whether a polish
        run is in flight, when the last run completed.
      - Primary CTA: "Run Final Polish" (disabled when drafts aren't
        ready or a polish run is currently active).
      - Recent runs: most recent detector + applier agent_runs rows
        from the agent_runs table — counts, cost, timing.
      - "What this catches" reference list of issue types so the user
        knows what kind of edits to expect.
    """
    from app.models import AgentRun
    from app.services.polish import list_recent_polish_edits_grouped

    # Closure state that survives @ui.refreshable rebuilds — needed so
    # the running→done transition is detectable across timer ticks.
    # `prev_running` starts at None (unknown) so the first observation
    # never triggers a false-positive completion toast.
    poll_state: dict = {"prev_running": None, "completion_notified": False}

    @ui.refreshable
    def render() -> None:
        # Section snapshot drives the gating logic: button is only
        # active when every writer-eligible section has a draft.
        with SessionLocal() as db:
            sec_rows = (
                db.execute(
                    select(ProposalSection)
                    .where(ProposalSection.proposal_id == proposal_id)
                    .order_by(ProposalSection.section_order)
                )
                .scalars()
                .all()
            )
            total = len(sec_rows)
            cost_deferred = sum(1 for s in sec_rows if s.requires_cost_analysis)
            excluded = sum(1 for s in sec_rows if s.excluded_from_draft)
            polishable = [s for s in sec_rows if (s.draft_text_markdown or "") and not s.excluded_from_draft]
            n_polishable = len(polishable)

            # Live status from the proposal row.
            p = db.get(Proposal, proposal_id)
            live_status = (
                p.status.value if p and hasattr(p.status, "value") else (str(p.status) if p else "unknown")
            )

            # Most recent polish runs from agent_runs — surfaces last
            # detector + applier activity so the user can see what
            # happened on prior passes without leaving the tab.
            recent_runs = (
                db.execute(
                    select(AgentRun)
                    .where(AgentRun.proposal_id == proposal_id)
                    .where(
                        AgentRun.agent_name.in_(
                            [
                                "final_polish_detector",
                                "final_polish_applier",
                            ]
                        )
                    )
                    .order_by(AgentRun.completed_at.desc())
                    .limit(50)
                )
                .scalars()
                .all()
            )
            recent_snapshots = [
                {
                    "agent_name": r.agent_name,
                    "model": r.model_used,
                    "status": (r.status.value if hasattr(r.status, "value") else str(r.status)),
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                    "input_tokens": r.input_tokens or 0,
                    "output_tokens": r.output_tokens or 0,
                    "cost_usd": float(r.cost_usd or 0.0),
                    "error_text": r.error_text or "",
                }
                for r in recent_runs
            ]

        polish_running = any(
            t.is_alive() and t.name == f"final-polish-{proposal_id}" for t in threading.enumerate()
        )

        # Most recent applier run = the last time anything was actually
        # polished. Detector runs alone don't change state.
        last_applier_at = None
        for r in recent_snapshots:
            if r["agent_name"] == "final_polish_applier" and r["completed_at"]:
                last_applier_at = r["completed_at"]
                break

        # Header card.
        with ui.card().classes("w-full"):
            with ui.row().classes("items-start justify-between w-full gap-3"):
                with ui.column().classes("gap-0 flex-1"):
                    ui.label("Final Polish").classes("text-xl font-semibold")
                    ui.label(
                        "Cross-section consistency cleanup. Reads every "
                        "drafted section as one corpus, detects drift "
                        "(FTE mismatches, terminology, naming, "
                        "commitments, voice), then auto-applies each "
                        "fix surgically — no per-issue triage clicks."
                    ).classes("text-sm opacity-80 pt-1")

                    # State summary line.
                    summary_parts = [
                        f"{n_polishable} of {total} section{'s' if total != 1 else ''} drafted",
                    ]
                    if cost_deferred:
                        summary_parts.append(f"{cost_deferred} cost-deferred")
                    if excluded:
                        summary_parts.append(f"{excluded} excluded")
                    if last_applier_at:
                        # Local-time-ish; matches the pattern other
                        # tabs use for "last X" timestamps.
                        summary_parts.append(
                            f"last polish run: {last_applier_at.strftime('%Y-%m-%d %H:%M UTC')}"
                        )
                    ui.label(" · ".join(summary_parts)).classes("text-xs opacity-60 pt-2")

                # Right-side action area.
                with ui.column().classes("gap-2 items-end"):
                    # "View final draft" — visible whenever there's
                    # something to read (any drafted section). Lets
                    # the user pull up the full polished proposal in
                    # a single scrollable modal without navigating
                    # away from this tab.
                    if n_polishable > 0:
                        ui.button(
                            "View final draft",
                            icon="article",
                            on_click=lambda: _open_final_draft_dialog(
                                proposal_id,
                            ),
                        ).props("flat color=primary").tooltip(
                            "Opens a modal showing every drafted "
                            "section concatenated in order — the same "
                            "view an evaluator would read. Reflects "
                            "the latest polish edits."
                        )
                    if polish_running:
                        with ui.row().classes("items-center gap-2"):
                            ui.spinner("dots", color="primary")
                            ui.label("Polish in progress — watch Run Progress").classes("text-sm opacity-70")
                        ui.button(
                            "Watch Run Progress",
                            icon="autorenew",
                            on_click=lambda: ui.navigate.to(
                                f"/proposals/{proposal_id}/progress",
                            ),
                        ).props("flat color=primary size=sm")
                    else:
                        # Visible "complete" status — when the most
                        # recent applier wave finished within the last
                        # 24 hours, surface a green ✓ chip so the
                        # user has an unambiguous "yes it's done"
                        # signal at the top of the tab. Counts the
                        # appliers that ran in that wave (everything
                        # after the most recent detector run).
                        if last_applier_at:
                            n_recent_edits = 0
                            for r in recent_snapshots:
                                if r["agent_name"] == "final_polish_detector":
                                    break  # earlier wave
                                if r["agent_name"] == "final_polish_applier" and r["status"] == "completed":
                                    n_recent_edits += 1
                            recent_window = datetime.utcnow() - last_applier_at < timedelta(hours=24)
                            if recent_window:
                                ui.chip(
                                    f"✓ Polish complete · "
                                    f"{n_recent_edits} fix"
                                    f"{'es' if n_recent_edits != 1 else ''} "
                                    f"applied in last run",
                                    icon="check_circle",
                                ).props("color=green-7 text-color=white").tooltip(
                                    "Most recent polish pass finished "
                                    f"at {last_applier_at.strftime('%Y-%m-%d %H:%M UTC')}. "
                                    "Each applied edit bumped its "
                                    "section's revision number — "
                                    "review the diff via the per-"
                                    "section history."
                                )

                        def _kick_off_polish() -> None:
                            spawn_final_polish(proposal_id)
                            ui.notify(
                                "Final Polish started — Gemini scans "
                                "the full corpus, Sonnet auto-applies "
                                "fixes section-by-section. Watch Run "
                                "Progress for the live summary.",
                                type="positive",
                                multi_line=True,
                                timeout=6000,
                            )
                            render.refresh()

                        btn = ui.button(
                            "Run Final Polish",
                            icon="auto_awesome",
                            on_click=_kick_off_polish,
                        ).props("color=primary")
                        if n_polishable == 0:
                            btn.props("disable").tooltip(
                                "No drafted sections yet. Polish reads "
                                "the whole proposal corpus — there's "
                                "nothing to compare across sections "
                                "until the Writer Team has drafted "
                                "at least 2 sections."
                            )
                        elif n_polishable == 1:
                            btn.tooltip(
                                "Only 1 section drafted — polish "
                                "looks for CROSS-section drift, so "
                                "you'll likely get 0 issues until "
                                "more sections land."
                            )
                        elif live_status not in (
                            "draft_ready",
                            "reviewing",
                            "draft_in_progress",
                            "approved",
                        ):
                            btn.tooltip(
                                "You can run polish at any time after "
                                "drafting starts. Re-running is safe — "
                                "it'll only edit sections where new "
                                "drift was found since last pass."
                            )

        # What this catches — reference list of issue types.
        with ui.expansion(
            "What Final Polish catches",
            icon="info",
            value=False,
        ).classes("w-full"):
            with ui.column().classes("gap-1 pt-1"):
                for label, desc in _POLISH_ISSUE_TYPE_LABELS.values():
                    with ui.row().classes("items-start gap-2 pt-1"):
                        ui.icon("check_circle").classes("text-blue-700 text-sm pt-0.5")
                        with ui.column().classes("gap-0 flex-1"):
                            ui.label(label).classes("text-sm font-medium")
                            ui.label(desc).classes("text-xs opacity-70")

        # Recent EDITS — the human-readable "what got polished" log.
        # Distinct from the agent_runs table below (which is a token /
        # cost ledger). This card answers the question "what did
        # polish actually change?" — grouped by polish run so the user
        # sees the latest wave first, expanded by default, and earlier
        # waves collapsed for audit-trail browsing.
        edit_runs = list_recent_polish_edits_grouped(
            proposal_id,
            run_limit=10,
        )
        if edit_runs:
            with ui.card().classes("w-full"):
                ui.label(
                    f"What got polished ({len(edit_runs)} run{'s' if len(edit_runs) != 1 else ''})"
                ).classes("text-base font-medium")
                ui.label(
                    "Each edit below was auto-applied by the Polish "
                    "Applier. The section's revision was bumped — "
                    "use the per-section regenerate path to revert "
                    "any edit that's wrong."
                ).classes("text-xs opacity-60 pb-2")

                _SEV_VISUAL = {
                    "CRITICAL": ("error", "red-7"),
                    "MAJOR": ("warning", "orange-7"),
                    "MINOR": ("info", "amber-7"),
                }
                _ISSUE_VISUAL = {
                    "numerical_drift": ("calculate", "blue-grey-6"),
                    "terminology_drift": ("translate", "blue-grey-6"),
                    "voice_drift": ("record_voice_over", "blue-grey-6"),
                    "commitment_conflict": ("gavel", "blue-grey-6"),
                    "redundant_repetition": ("content_copy", "blue-grey-6"),
                    "naming_inconsistency": ("badge", "blue-grey-6"),
                }

                for idx, run in enumerate(edit_runs):
                    n = run["n_edits"]
                    sev = run["by_severity"]
                    sev_chips_text = " · ".join(
                        f"{count} {label}"
                        for label, count in (
                            ("CRITICAL", sev["CRITICAL"]),
                            ("MAJOR", sev["MAJOR"]),
                            ("MINOR", sev["MINOR"]),
                        )
                        if count
                    )
                    run_label = (
                        f"Run @ "
                        f"{run['run_at'].strftime('%Y-%m-%d %H:%M UTC')}"
                        f"  ·  {n} edit{'s' if n != 1 else ''}"
                        + (f"  ·  {sev_chips_text}" if sev_chips_text else "")
                        + f"  ·  ${run['total_cost_usd']:.2f}"
                    )
                    # Most recent run open by default — that's the one
                    # the user just clicked Run on. Older runs collapsed.
                    with ui.expansion(
                        run_label,
                        icon="auto_awesome",
                        value=(idx == 0),
                    ).classes("w-full"):
                        for edit in run["edits"]:
                            sev_icon, sev_color = _SEV_VISUAL.get(
                                edit["severity"],
                                ("info", "slate-6"),
                            )
                            issue_icon, _ = _ISSUE_VISUAL.get(
                                edit["issue_type"],
                                ("edit", "blue-grey-6"),
                            )
                            issue_label = _POLISH_ISSUE_TYPE_LABELS.get(
                                edit["issue_type"],
                                (edit["issue_type"], ""),
                            )[0]
                            with ui.card().classes(f"w-full bg-slate-50 border-l-4 border-{sev_color}"):
                                with ui.row().classes("items-center gap-2 flex-wrap"):
                                    ui.chip(
                                        edit["section_id_label"],
                                    ).props("color=slate-2 text-color=slate-9 size=sm")
                                    ui.chip(
                                        edit["severity"],
                                        icon=sev_icon,
                                    ).props(f"color={sev_color} text-color=white size=sm")
                                    ui.chip(
                                        issue_label,
                                        icon=issue_icon,
                                    ).props("color=blue-grey-2 text-color=blue-grey-9 size=sm")
                                    ui.label(
                                        edit["applied_at"].strftime(
                                            "%H:%M:%S",
                                        )
                                    ).classes("text-xs opacity-50 ml-auto font-mono")
                                ui.label(edit["edit_summary"]).classes("text-sm pt-1")
                                # Rationale + before/after — collapsible
                                # because most users will only want to
                                # see the summary; the detail is for
                                # spot-audit.
                                if (
                                    edit.get("rationale")
                                    or edit.get("problematic_text")
                                    or edit.get("suggested_fix")
                                ):
                                    with ui.expansion(
                                        "Why + before/after",
                                        icon="unfold_more",
                                        value=False,
                                    ).classes("w-full pt-1"):
                                        if edit.get("rationale"):
                                            ui.label("Why:").classes("text-xs font-medium opacity-70 pt-1")
                                            ui.label(edit["rationale"]).classes("text-xs opacity-80")
                                        if edit.get("problematic_text"):
                                            ui.label("Before:").classes("text-xs font-medium opacity-70 pt-1")
                                            ui.label(edit["problematic_text"]).classes(
                                                "text-xs italic whitespace-pre-wrap bg-red-50 p-2 rounded"
                                            )
                                        if edit.get("suggested_fix"):
                                            ui.label("After:").classes("text-xs font-medium opacity-70 pt-1")
                                            ui.label(edit["suggested_fix"]).classes(
                                                "text-xs italic whitespace-pre-wrap bg-green-50 p-2 rounded"
                                            )

        # Recent runs — surface what happened so the user can see
        # whether the previous pass found anything.
        if recent_snapshots:
            with ui.card().classes("w-full"):
                ui.label(f"Recent Polish runs ({len(recent_snapshots)})").classes("text-base font-medium")
                ui.label(
                    "Most recent first. Detector entries scan the "
                    "corpus; Applier entries each represent one "
                    "auto-applied edit on a section."
                ).classes("text-xs opacity-60 pb-1")

                # Aggregate costs per "wave" — not perfect, but
                # gives a feel for budget spent. Group by date+hour.
                total_cost = sum(r["cost_usd"] for r in recent_snapshots)
                n_detector = sum(1 for r in recent_snapshots if r["agent_name"] == "final_polish_detector")
                n_applier = sum(1 for r in recent_snapshots if r["agent_name"] == "final_polish_applier")
                with ui.row().classes("flex-wrap gap-2 pt-1 pb-2"):
                    ui.chip(
                        f"{n_detector} detector run{'s' if n_detector != 1 else ''}",
                        icon="search",
                    ).props("color=blue-2 text-color=blue-9 size=sm")
                    ui.chip(
                        f"{n_applier} applied edit{'s' if n_applier != 1 else ''}",
                        icon="auto_fix_high",
                    ).props("color=green-2 text-color=green-9 size=sm")
                    ui.chip(
                        f"${total_cost:.2f} total",
                        icon="attach_money",
                    ).props("color=slate-2 text-color=slate-9 size=sm")

                columns = [
                    {"name": "when", "label": "When (UTC)", "field": "when", "align": "left"},
                    {"name": "agent", "label": "Agent", "field": "agent", "align": "left"},
                    {"name": "model", "label": "Model", "field": "model", "align": "left"},
                    {"name": "status", "label": "Status", "field": "status", "align": "left"},
                    {"name": "tokens", "label": "Tokens", "field": "tokens", "align": "right"},
                    {"name": "cost", "label": "Cost", "field": "cost", "align": "right"},
                ]
                rows = [
                    {
                        "when": (
                            r["completed_at"].strftime("%Y-%m-%d %H:%M:%S") if r["completed_at"] else "—"
                        ),
                        "agent": ("Detector" if r["agent_name"] == "final_polish_detector" else "Applier"),
                        "model": r["model"] or "—",
                        "status": r["status"],
                        "tokens": (
                            f"{r['input_tokens']:,}/{r['output_tokens']:,}"
                            if (r["input_tokens"] or r["output_tokens"])
                            else "—"
                        ),
                        "cost": f"${r['cost_usd']:.4f}",
                    }
                    for r in recent_snapshots
                ]
                ui.table(
                    columns=columns,
                    rows=rows,
                    row_key="when",
                ).classes("w-full")

    render()

    # Polling timer placed OUTSIDE @ui.refreshable so it doesn't get
    # duplicated on every refresh. Two responsibilities:
    #   1. While a polish thread is alive, refresh every 5s so any
    #      newly-completed agent_run shows up.
    #   2. On the running→done transition, refresh ONE more time AND
    #      fire a completion toast — without this, the UI stays stuck
    #      on "Polish in progress" because the timer's normal refresh
    #      condition (running_now) is false right after completion.
    def maybe_refresh() -> None:
        running_now = any(
            t.is_alive() and t.name == f"final-polish-{proposal_id}" for t in threading.enumerate()
        )
        prev = poll_state["prev_running"]

        # Transition: was running, now done. Fire completion toast +
        # one final refresh so the "Polish in progress" indicator
        # flips to the Run button and the recent-runs table picks up
        # the final applier rows. Guarded by completion_notified so a
        # subsequent re-render doesn't re-fire the toast every 5s.
        if prev is True and not running_now and not poll_state["completion_notified"]:
            render.refresh()
            ui.notify(
                "Final Polish complete — see the 'Recent Polish "
                "runs' table below for what was applied. Each "
                "applied edit bumped its section's revision; revert "
                "via the per-section regenerate path if any fix "
                "was wrong.",
                type="positive",
                multi_line=True,
                timeout=8000,
            )
            poll_state["completion_notified"] = True
        elif running_now:
            # Reset the notified-once guard on the next run so a
            # second polish pass triggers a second toast.
            poll_state["completion_notified"] = False
            render.refresh()

        poll_state["prev_running"] = running_now

    ui.timer(5.0, maybe_refresh)
