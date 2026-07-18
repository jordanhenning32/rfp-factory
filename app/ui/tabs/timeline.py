"""Proposal Review > Timeline tab.

User-curated implementation timeline rendered as a horizontal Gantt
chart. Each phase is a colored bar positioned by its day offset and
sized by its duration. The chart is pure CSS — no external charting
library, so it deploys with the rest of the NiceGUI bundle and
renders predictably at any width.

Two empty-state paths:
  - No phases AND no PricingPackage with phase_breakdown_json:
    blank state with "Add Phase" CTA only.
  - No phases BUT cost analyst has produced phase_breakdown_json:
    blank state with "Import from cost build" CTA in addition to
    "Add Phase". One click seeds the timeline with the analyst's
    phases (start_month / duration_months × 30 → days), then the
    user refines.

Per-phase actions: Edit (dialog with all fields) · Delete (confirm
prompt). Anchor-date picker in the header card lets the user
optionally tie offsets to absolute calendar dates for a phase
("d0–d30" becomes "Jun 1 – Jun 30"). Government RFPs typically
specify schedule in offsets so the anchor is opt-in.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from nicegui import ui

from app.services.timeline import (
    DEFAULT_PHASE_COLOR,
    add_phase,
    delete_phase,
    get_timeline,
    has_importable_cost_phases,
    import_from_cost_phases,
    set_anchor_date,
    total_duration,
    update_phase,
)
from app.ui._theme import CYAN, NAVY

log = logging.getLogger(__name__)


# Curated palette for phase color picker. Quadratic Digital navy
# leads (default), then a graded set that reads cleanly on the
# slate-100 Gantt track and against each other when adjacent.
_PHASE_COLOR_PALETTE: list[tuple[str, str]] = [
    ("Navy", NAVY),
    ("Cyan", CYAN),
    ("Teal", "#0F766E"),
    ("Emerald", "#059669"),
    ("Amber", "#D97706"),
    ("Rose", "#BE123C"),
    ("Violet", "#7C3AED"),
    ("Slate", "#475569"),
]


def _render_timeline_tab(proposal_id: int, *, on_state_change=None) -> None:
    """Timeline tab — Gantt-style implementation schedule.

    `on_state_change` is the outer page's chrome refresh hook; we
    call it after every mutation so any future Timeline-tab badge
    (none today) stays in sync without manual page reload.
    """

    def _after_change() -> None:
        render.refresh()
        if on_state_change is not None:
            on_state_change()

    @ui.refreshable
    def render() -> None:
        doc = get_timeline(proposal_id)
        phases = doc["phases"]
        anchor_str = doc["anchor_date"]
        anchor = _parse_date(anchor_str)
        importable = has_importable_cost_phases(proposal_id)
        total = total_duration(phases)

        _render_header_card(
            proposal_id,
            phases,
            anchor,
            anchor_str,
            total,
            importable,
            _after_change,
        )

        if not phases:
            _render_empty_state(proposal_id, importable, _after_change)
            return

        _render_gantt_chart(phases, anchor, total)
        _render_phase_list(proposal_id, phases, anchor, _after_change)

    render()


# ---- Header card ---------------------------------------------------------


def _render_header_card(
    proposal_id: int,
    phases: list[dict],
    anchor: date | None,
    anchor_str: str | None,
    total: int,
    importable: bool,
    on_change,
) -> None:
    """Top card: title, summary stats, anchor-date picker, action
    buttons. Layout mirrors other-tab header cards (Cost, Findings,
    Submission Checklist) so the page feels consistent."""
    with ui.card().classes("w-full"):
        with ui.row().classes("items-start justify-between w-full gap-3 flex-wrap"):
            with ui.column().classes("gap-0 flex-1"):
                ui.label("Implementation Timeline").classes("text-xl font-semibold")
                ui.label(
                    "User-curated Gantt schedule attached to this "
                    "proposal. Phases use day-offsets from project "
                    "start (NTP); set an anchor date below to also "
                    "render absolute calendar dates."
                ).classes("text-sm opacity-80 pt-1")

                if phases:
                    summary_bits = [
                        f"{len(phases)} phase{'s' if len(phases) != 1 else ''}",
                        f"{total} day total span",
                    ]
                    if anchor:
                        end = anchor + timedelta(days=total)
                        summary_bits.append(f"{anchor.strftime('%b %d, %Y')} → {end.strftime('%b %d, %Y')}")
                    ui.label(" · ".join(summary_bits)).classes("text-xs opacity-60 pt-2 font-mono")

            # Right side: actions.
            with ui.column().classes("gap-2 items-end"):
                with ui.row().classes("gap-2"):
                    if importable and not phases:
                        ui.button(
                            "Import from cost build",
                            icon="auto_fix_high",
                            on_click=lambda: _do_import(
                                proposal_id,
                                on_change,
                            ),
                        ).props("flat color=primary").tooltip(
                            "Seed the timeline from the cost "
                            "analyst's phase_breakdown (MEDIUM "
                            "scenario). Months → days at 30/month. "
                            "You can refine durations after import."
                        )
                    ui.button(
                        "Add Phase",
                        icon="add",
                        on_click=lambda: _open_phase_dialog(
                            proposal_id,
                            phase=None,
                            on_saved=on_change,
                        ),
                    ).props("color=primary")

                # Anchor-date picker.
                with ui.row().classes("items-center gap-2 pt-1"):
                    ui.label("Anchor:").classes("text-xs uppercase opacity-60")
                    anchor_input = (
                        ui.input(
                            value=anchor_str or "",
                            placeholder="YYYY-MM-DD",
                        )
                        .props("dense outlined")
                        .classes("w-36")
                    )
                    anchor_input.props("type=date")

                    def _save_anchor() -> None:
                        v = (anchor_input.value or "").strip()
                        if v and not _parse_date(v):
                            ui.notify(
                                f"'{v}' is not a valid date (use YYYY-MM-DD).",
                                type="warning",
                            )
                            return
                        set_anchor_date(proposal_id, v or None)
                        ui.notify(
                            f"Anchor date {'set to ' + v if v else 'cleared'}.",
                            type="positive",
                            timeout=2000,
                        )
                        on_change()

                    ui.button(
                        icon="check",
                        on_click=_save_anchor,
                    ).props("flat dense round size=sm").tooltip(
                        "Apply the anchor date. Once set, phase "
                        "labels show absolute dates alongside the "
                        "day-offsets."
                    )
                    if anchor_str:

                        def _clear_anchor() -> None:
                            anchor_input.set_value("")
                            set_anchor_date(proposal_id, None)
                            ui.notify("Anchor date cleared.", type="positive")
                            on_change()

                        ui.button(
                            icon="close",
                            on_click=_clear_anchor,
                        ).props("flat dense round size=sm color=red-7").tooltip("Clear the anchor date.")


# ---- Empty state ---------------------------------------------------------


def _render_empty_state(
    proposal_id: int,
    importable: bool,
    on_change,
) -> None:
    """Empty-state card. When cost phases are available, surfaces the
    'Import from cost build' CTA prominently as the path of least
    resistance. Otherwise just the Add Phase prompt."""
    with ui.card().classes("w-full bg-slate-50 border-l-4 border-slate-300 mt-4"):
        with ui.column().classes("items-center justify-center w-full py-10 gap-3"):
            ui.icon("schedule", size="xl").classes("opacity-50")
            ui.label("No phases yet").classes("text-base font-medium opacity-80")
            if importable:
                ui.label(
                    "The cost analyst already produced a phase "
                    "breakdown for this proposal. Import it to "
                    "seed the timeline, then refine durations and "
                    "add deliverables."
                ).classes("text-sm opacity-60 text-center max-w-xl")
                with ui.row().classes("gap-2 pt-2"):
                    ui.button(
                        "Import from cost build",
                        icon="auto_fix_high",
                        on_click=lambda: _do_import(
                            proposal_id,
                            on_change,
                        ),
                    ).props("color=primary unelevated")
                    ui.button(
                        "Or add manually",
                        icon="add",
                        on_click=lambda: _open_phase_dialog(
                            proposal_id,
                            phase=None,
                            on_saved=on_change,
                        ),
                    ).props("flat color=primary")
            else:
                ui.label(
                    "Click Add Phase above to start building the "
                    "implementation schedule. Once the cost analyst "
                    "runs you'll also be able to import its phase "
                    "breakdown as a starting point."
                ).classes("text-sm opacity-60 text-center max-w-xl")


# ---- Gantt chart ---------------------------------------------------------


def _render_gantt_chart(
    phases: list[dict],
    anchor: date | None,
    total: int,
) -> None:
    """Pure-CSS Gantt: one row per phase, bars positioned by
    percentage of total duration. Absolute-positioned div inside a
    relative-positioned track, so the whole thing scales with the
    available width."""
    with ui.card().classes("w-full mt-4"):
        ui.label("Schedule").classes("text-base font-medium pb-1")
        ui.label(
            "Bar position = day-offset from project start. "
            "Bar width = duration. Hover a bar for the full label."
        ).classes("text-xs opacity-60 pb-3")

        # Day-axis tick labels above the chart.
        _render_day_axis(total, anchor)

        # One row per phase.
        for phase in phases:
            _render_gantt_row(phase, anchor, total)


def _render_day_axis(total: int, anchor: date | None) -> None:
    """Render a 5-tick day axis above the Gantt rows. Ticks at 0,
    25, 50, 75, 100% of total span. When anchor is set, tick labels
    show absolute dates alongside day offsets."""
    if total <= 0:
        return
    ticks = [0, 25, 50, 75, 100]
    with ui.row().classes("w-full pl-48 pr-32"):
        with ui.element("div").classes("flex-1 relative h-6 border-b border-slate-300"):
            for pct in ticks:
                day = round(total * pct / 100)
                if anchor:
                    label = (anchor + timedelta(days=day)).strftime("%b %d")
                else:
                    label = f"d{day}"
                with (
                    ui.element("div")
                    .classes("absolute text-[10px] opacity-60 font-mono")
                    .style(f"left: {pct}%; transform: translateX(-50%); top: 0;")
                ):
                    ui.label(label)


def _render_gantt_row(
    phase: dict,
    anchor: date | None,
    total: int,
) -> None:
    """One Gantt row: phase name on the left (fixed-width), the
    track + colored bar in the middle (flex), date label on the
    right (fixed-width). Bar tooltip carries the full phase detail
    (name + deliverable + owner) so the row stays scannable."""
    if total <= 0:
        return
    left_pct = phase["start_offset"] / total * 100
    width_pct = phase["duration"] / total * 100
    end_offset = phase["start_offset"] + phase["duration"]

    if anchor:
        start_date = anchor + timedelta(days=phase["start_offset"])
        end_date = anchor + timedelta(days=end_offset)
        date_label = f"{start_date.strftime('%b %d')} – {end_date.strftime('%b %d')}"
    else:
        date_label = f"d{phase['start_offset']}–d{end_offset}"

    tooltip = phase["phase_name"]
    if phase["deliverable"]:
        tooltip += f"\n\n{phase['deliverable']}"
    if phase["owner"]:
        tooltip += f"\n\nOwner: {phase['owner']}"

    with ui.row().classes("w-full items-center gap-2 py-1"):
        ui.label(phase["phase_name"]).classes("w-48 text-sm truncate").tooltip(phase["phase_name"])
        # Track.
        with ui.element("div").classes("flex-1 relative h-8 bg-slate-100 rounded overflow-hidden"):
            with (
                ui.element("div")
                .classes("absolute h-full rounded shadow-sm flex items-center justify-center cursor-default")
                .style(
                    f"left: {left_pct}%; width: {width_pct}%; "
                    f"min-width: 24px; "
                    f"background-color: {phase['color']};"
                )
                .tooltip(tooltip)
            ):
                # Inside-bar label: just the duration, omitted when
                # the bar is too narrow to read it.
                if width_pct >= 8:
                    ui.label(f"{phase['duration']}d").classes("text-[10px] font-mono").style(
                        "color: rgba(255,255,255,0.95);"
                    )
        ui.label(date_label).classes("w-32 text-xs font-mono opacity-70")


# ---- Phase list (edit / delete) ------------------------------------------


def _render_phase_list(
    proposal_id: int,
    phases: list[dict],
    anchor: date | None,
    on_change,
) -> None:
    """Below the Gantt: per-phase cards with edit / delete actions
    + the deliverable + owner detail that doesn't fit on the chart."""
    with ui.card().classes("w-full mt-4"):
        ui.label("Phase Details").classes("text-base font-medium pb-1")
        ui.label(
            "Click a phase to edit. Reorder by adjusting start "
            "offsets — the Gantt rows sort by start day automatically."
        ).classes("text-xs opacity-60 pb-2")

        for phase in phases:
            _render_phase_row(
                proposal_id,
                phase,
                anchor,
                on_change,
            )


def _render_phase_row(
    proposal_id: int,
    phase: dict,
    anchor: date | None,
    on_change,
) -> None:
    """One phase as an actionable row card."""
    end_offset = phase["start_offset"] + phase["duration"]
    if anchor:
        start_date = anchor + timedelta(days=phase["start_offset"])
        end_date = anchor + timedelta(days=end_offset)
        when = (
            f"{start_date.strftime('%b %d, %Y')} – "
            f"{end_date.strftime('%b %d, %Y')}  "
            f"(d{phase['start_offset']}–d{end_offset})"
        )
    else:
        when = f"day {phase['start_offset']} – day {end_offset}  ({phase['duration']} days)"

    with ui.card().classes("w-full bg-slate-50/40 border-l-4").style(f"border-left-color: {phase['color']};"):
        with ui.row().classes("items-start gap-3 w-full"):
            # Color swatch.
            ui.element("div").classes("w-3 h-12 rounded mt-1").style(f"background-color: {phase['color']};")

            with ui.column().classes("gap-0 flex-1"):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.label(phase["phase_name"]).classes("text-base font-medium")
                    if phase["owner"]:
                        ui.chip(phase["owner"], icon="person").props(
                            "dense color=blue-grey-2 text-color=blue-grey-9"
                        ).classes("text-xs")
                ui.label(when).classes("text-xs font-mono opacity-70")
                if phase["deliverable"]:
                    ui.label(phase["deliverable"]).classes("text-sm pt-1 opacity-90")

            with ui.column().classes("gap-1 items-stretch"):
                ui.button(
                    icon="edit",
                    on_click=lambda p=phase: _open_phase_dialog(
                        proposal_id,
                        phase=p,
                        on_saved=on_change,
                    ),
                ).props("flat dense round size=sm").tooltip("Edit")
                ui.button(
                    icon="delete",
                    on_click=lambda p=phase: _confirm_delete(
                        proposal_id,
                        p,
                        on_change,
                    ),
                ).props("flat dense round size=sm color=red-7").tooltip("Delete")


# ---- Dialogs -------------------------------------------------------------


def _open_phase_dialog(
    proposal_id: int,
    *,
    phase: dict | None,
    on_saved,
) -> None:
    """Add-or-edit phase dialog. `phase=None` for add, otherwise the
    existing phase dict (its `id` keys the update). All fields edited
    in one dialog so the user can refine a phase end-to-end without
    multiple clicks."""
    is_edit = phase is not None
    initial = phase or {
        "phase_name": "",
        "start_offset": 0,
        "duration": 30,
        "deliverable": "",
        "owner": "",
        "color": DEFAULT_PHASE_COLOR,
    }

    with ui.dialog() as dialog, ui.card().classes("min-w-[28rem] max-w-[40rem]"):
        ui.label("Edit Phase" if is_edit else "Add Phase").classes("text-base font-semibold")
        ui.label(
            "All fields editable. Day offsets are integers >= 0; "
            "duration is days >= 1. Deliverable is shown in the "
            "phase-detail card and exported in future DOCX runs."
        ).classes("text-xs opacity-70 pb-2")

        name_in = (
            ui.input(
                "Phase name",
                placeholder="Discovery & Planning",
                value=initial["phase_name"],
            )
            .classes("w-full")
            .props("outlined dense")
        )

        with ui.row().classes("w-full gap-2"):
            offset_in = (
                ui.number(
                    "Start offset (days)",
                    value=initial["start_offset"],
                    min=0,
                    step=1,
                )
                .classes("flex-1")
                .props("outlined dense")
            )
            duration_in = (
                ui.number(
                    "Duration (days)",
                    value=initial["duration"],
                    min=1,
                    step=1,
                )
                .classes("flex-1")
                .props("outlined dense")
            )

        deliverable_in = (
            ui.textarea(
                "Deliverable / output",
                placeholder=("e.g., Project charter, kickoff materials, baselined requirements document"),
                value=initial["deliverable"],
            )
            .classes("w-full")
            .props("outlined autogrow rows=2")
        )

        owner_in = (
            ui.input(
                "Owner / role (optional)",
                placeholder="Project Manager",
                value=initial["owner"],
            )
            .classes("w-full")
            .props("outlined dense")
        )

        # Color picker — palette of brand-aligned options.
        ui.label("Color").classes("text-xs uppercase opacity-60 pt-2")
        selected_color = {"value": initial["color"]}
        with ui.row().classes("flex-wrap gap-1 pt-1"):
            swatches: list = []
            for label, hex_code in _PHASE_COLOR_PALETTE:
                btn = (
                    ui.button(
                        label,
                        on_click=lambda c=hex_code, lbl=label: _select_color(
                            c,
                            lbl,
                            selected_color,
                            swatches,
                        ),
                    )
                    .props("dense unelevated")
                    .style(
                        f"background-color: {hex_code}; "
                        f"color: white; "
                        f"min-width: 70px; "
                        f"border: "
                        f"{'2px solid #000' if hex_code == selected_color['value'] else '2px solid transparent'};"
                    )
                )
                swatches.append((btn, hex_code))

        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            def _save() -> None:
                name = (name_in.value or "").strip()
                if not name:
                    ui.notify(
                        "Phase name is required.",
                        type="warning",
                    )
                    return
                offset = int(offset_in.value or 0)
                duration = int(duration_in.value or 1)
                if offset < 0:
                    ui.notify(
                        "Start offset must be >= 0.",
                        type="warning",
                    )
                    return
                if duration < 1:
                    ui.notify(
                        "Duration must be >= 1 day.",
                        type="warning",
                    )
                    return

                fields = {
                    "phase_name": name,
                    "start_offset": offset,
                    "duration": duration,
                    "deliverable": (deliverable_in.value or "").strip(),
                    "owner": (owner_in.value or "").strip(),
                    "color": selected_color["value"],
                }
                if is_edit:
                    update_phase(proposal_id, phase["id"], **fields)
                    ui.notify(f"Updated '{name}'.", type="positive")
                else:
                    add_phase(proposal_id, **fields)
                    ui.notify(f"Added '{name}'.", type="positive")
                dialog.close()
                on_saved()

            ui.button(
                "Save" if is_edit else "Add",
                icon="save" if is_edit else "add",
                on_click=_save,
            ).props("color=primary unelevated")

    dialog.open()


def _select_color(
    hex_code: str,
    label: str,
    selected: dict,
    swatches: list,
) -> None:
    """Color-picker click handler. Updates the in-dialog selection
    state and re-styles the swatches so the active color shows a
    black border ring."""
    selected["value"] = hex_code
    for btn, code in swatches:
        btn.style(
            f"background-color: {code}; "
            f"color: white; "
            f"min-width: 70px; "
            f"border: "
            f"{'2px solid #000' if code == hex_code else '2px solid transparent'};"
        )


def _confirm_delete(
    proposal_id: int,
    phase: dict,
    on_change,
) -> None:
    """Confirmation dialog for phase deletion. Single-step delete
    with no undo would feel risky; this gives the user one chance
    to back out."""
    with ui.dialog() as dialog, ui.card().classes("min-w-[24rem]"):
        ui.label("Delete phase?").classes("text-base font-semibold")
        ui.label(
            f"'{phase['phase_name']}' will be removed from the "
            "timeline. Other phases keep their offsets — adjust "
            "them manually if you want to fill the gap."
        ).classes("text-sm opacity-80 pt-1")
        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            def _do() -> None:
                ok = delete_phase(proposal_id, phase["id"])
                dialog.close()
                if ok:
                    ui.notify(
                        f"Deleted '{phase['phase_name']}'.",
                        type="positive",
                    )
                    on_change()
                else:
                    ui.notify(
                        "Delete failed — phase may have already been removed.",
                        type="warning",
                    )

            ui.button(
                "Delete",
                icon="delete",
                on_click=_do,
            ).props("color=red-7 unelevated")
    dialog.open()


def _do_import(proposal_id: int, on_change) -> None:
    """Import-from-cost-phases handler. Confirms before replacing
    any existing phases (the empty-state CTA only fires on an empty
    timeline, so the confirmation is informational on first import
    but guards against re-clicks if the user adds phases first then
    clicks Import again from the header)."""
    n = import_from_cost_phases(proposal_id)
    if n == 0:
        ui.notify(
            "No cost-analyst phase breakdown to import. Re-run the Cost Analyst from the Cost tab first.",
            type="warning",
            multi_line=True,
        )
        return
    ui.notify(
        f"Imported {n} phase{'s' if n != 1 else ''} from the cost "
        "build. Refine durations + add deliverables, then set an "
        "anchor date if you have one.",
        type="positive",
        multi_line=True,
        timeout=6000,
    )
    on_change()


# ---- Helpers -------------------------------------------------------------


def _parse_date(s: str | None) -> date | None:
    """Parse a YYYY-MM-DD string; return None on failure or empty."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
