"""Proposal Review > Reviewer Findings tab.

Surfaces every finding from Reviewer A + Reviewer B with per-finding
Accept / Dismiss / Unmark actions and a "Apply N accepted findings"
button per section that triggers a writer regenerate. Top of tab
includes the "I've applied the findings" green banner with auto-clear,
the manual-vs-auto-loop branching, and the per-section Apply state
machine.

Includes the supporting symbols only this tab uses:
  - _FINDING_SEVERITY_VISUAL / _FINDING_SEVERITY_ORDER /
    _FINDING_CATEGORY_LABELS constants
  - _render_finding_card (per-finding row with action buttons)
  - _accept_finding_action / _unmark_finding_action / _open_dismiss_dialog
"""

from __future__ import annotations

import logging

from nicegui import ui
from sqlalchemy import select

from app.db.session import SessionLocal
from app.jobs.writer import spawn_writer_for_section
from app.models import ProposalSection, ReviewerFinding
from app.services.findings import (
    accept_finding,
    build_directive_from_findings,
    bulk_accept_pending_findings,
    dismiss_finding,
    get_accepted_findings_for_section,
    unmark_finding,
)
from app.services.lessons import (
    schedule_extract_on_accept,
    schedule_extract_on_dismiss,
)

log = logging.getLogger(__name__)


# Helpers that still live in pages.py (called from both the next-step
# banner and the Findings tab) — resolved lazily on first call so we
# don't hit a circular import at module-load time.
def _pages_helper(name: str):
    from app.ui import pages

    return getattr(pages, name)


def _run_reviewer_loop(*args, **kwargs):
    return _pages_helper("_run_reviewer_loop")(*args, **kwargs)


def _run_reviewer_only(*args, **kwargs):
    return _pages_helper("_run_reviewer_only")(*args, **kwargs)


def _apply_accepted_findings(*args, **kwargs):
    return _pages_helper("_apply_accepted_findings")(*args, **kwargs)


def _re_review_section(*args, **kwargs):
    return _pages_helper("_re_review_section")(*args, **kwargs)


_FINDING_SEVERITY_VISUAL = {
    "CRITICAL": ("error", "red-700", "bg-red-50", "border-red-500"),
    "MAJOR": ("warning", "orange-700", "bg-orange-50", "border-orange-400"),
    "MINOR": ("info", "amber-700", "bg-amber-50", "border-amber-300"),
}
_FINDING_SEVERITY_ORDER = ("CRITICAL", "MAJOR", "MINOR")

_FINDING_CATEGORY_LABELS = {
    "compliance_gap": "Compliance gap",
    "uncited_claim": "Uncited claim",
    "hallucination": "Hallucination",
    "overcommitment": "Overcommitment",
    "format_violation": "Format violation",
    "shortfall_overreach": "Shortfall overreach",
    "weak_persuasion": "Weak persuasion",
    "voice_inconsistency": "Voice inconsistency",
    "evaluator_misalignment": "Evaluator misalignment",
    "cross_section_inconsistency": "Cross-section conflict",
}


def _render_findings_tab(
    proposal_id: int,
    status_val: str,
    *,
    on_state_change=None,
) -> None:
    """Reviewer Findings tab — Reviewer A (Opus, compliance) + Reviewer B
    (Gemini, persuasion) findings grouped by section. Each finding has
    Accept / Dismiss / Unmark actions. Per-section: Apply accepted findings
    (regenerates with findings as a directive), Re-run reviewer.

    While the auto-loop is running:
    - Polls every 5s so findings appear live as the loop processes sections.
    - Marks the section currently in-flight with a spinner badge and
      hides the "Apply accepted findings" button on that section
      (concurrent regenerates would race on draft_text_markdown).
    - Other completed sections are fully actionable.
    """
    from app.services.cancellation import (
        JOB_AUTO_REVIEW,
        get_active_sections,
    )
    from app.services.cancellation import (
        is_running as _job_is_running,
    )

    # Default-hide accepted / dismissed / auto-resolved findings — they're
    # already addressed and the user just needs to act on the pending ones.
    # The badge shows pending count; the rendered list now matches it.
    state = {"show_all": False}

    def _after_change() -> None:
        render.refresh()
        if on_state_change is not None:
            on_state_change()

    @ui.refreshable
    def render() -> None:
        # Active sections + loop-running snapshot — read once per render so
        # the in-flight badges and the safety check on Apply use a
        # consistent view of "what's currently regenerating?". The
        # active-section registry is populated by ANY in-flight writer
        # (auto-loop OR manual per-section / "Apply all" spawns), so we
        # always read it — gating on loop_running would hide manual
        # regens from the in-flight indicator and leave the user with
        # no signal that their Apply click did anything.
        _job_is_running(JOB_AUTO_REVIEW, proposal_id)
        active_section_pks = get_active_sections(proposal_id)

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
            section_meta = {
                s.id: {
                    "pk": s.id,
                    "section_id": s.section_id,
                    "section_title": s.section_title,
                    "section_order": s.section_order,
                    "has_draft": bool(s.draft_text_markdown),
                    "requires_cost_analysis": bool(s.requires_cost_analysis),
                }
                for s in sec_rows
            }

            finding_rows = (
                db.execute(
                    select(ReviewerFinding)
                    .where(ReviewerFinding.proposal_section_id.in_(section_meta.keys()))
                    .order_by(
                        ReviewerFinding.proposal_section_id,
                        ReviewerFinding.severity,
                        ReviewerFinding.id,
                    )
                )
                .scalars()
                .all()
            )
            findings = [
                {
                    "id": f.id,
                    "section_pk": f.proposal_section_id,
                    "reviewer_agent": (
                        f.reviewer_agent.value
                        if hasattr(f.reviewer_agent, "value")
                        else str(f.reviewer_agent)
                    ),
                    "pass_number": f.pass_number,
                    "severity": (f.severity.value if hasattr(f.severity, "value") else str(f.severity)),
                    "category": (f.category.value if hasattr(f.category, "value") else str(f.category)),
                    "finding_text": f.finding_text,
                    "suggested_fix": f.suggested_fix or "",
                    "resolved_in_pass_number": f.resolved_in_pass_number,
                    "accepted_at": f.accepted_at,
                    "dismissed_at": f.dismissed_at,
                    "dismissed_reason": f.dismissed_reason or "",
                }
                for f in finding_rows
            ]

        if not findings:
            with ui.column().classes("items-center justify-center w-full py-12 gap-3"):
                ui.icon("rate_review", size="xl").classes("opacity-60")
                if status_val == "reviewing":
                    ui.label(
                        "Reviewer Loop running. Findings appear here as each section completes."
                    ).classes("text-base opacity-80 text-center")
                else:
                    ui.label("No reviewer findings yet.").classes("text-base opacity-80")
                    has_drafts = any(
                        m["has_draft"] and not m["requires_cost_analysis"] for m in section_meta.values()
                    )
                    if has_drafts:
                        with ui.row().classes("gap-2 pt-2"):
                            ui.button(
                                "Run Auto Review-Revise Loop",
                                icon="all_inclusive",
                                on_click=lambda: _run_reviewer_loop(proposal_id),
                            ).props("color=primary")
                            ui.button(
                                "Single review pass (no revisions)",
                                icon="rate_review",
                                on_click=lambda: _run_reviewer_only(proposal_id),
                            ).props("flat dense size=sm")
                    else:
                        ui.label("Draft sections first (Outline → Approve → Writer Team).").classes(
                            "text-sm opacity-60"
                        )
            return

        # Aggregate counts.
        n_total = len(findings)
        n_pending = sum(
            1
            for f in findings
            if not f["accepted_at"] and not f["dismissed_at"] and f["resolved_in_pass_number"] is None
        )
        n_accepted = sum(1 for f in findings if f["accepted_at"])
        n_dismissed = sum(1 for f in findings if f["dismissed_at"])
        n_resolved = sum(1 for f in findings if f["resolved_in_pass_number"] is not None)
        n_critical_pending = sum(
            1
            for f in findings
            if f["severity"] == "CRITICAL"
            and not f["accepted_at"]
            and not f["dismissed_at"]
            and f["resolved_in_pass_number"] is None
        )

        # Header summary
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.column().classes("gap-0"):
                    if state["show_all"]:
                        title_text = f"{n_total} finding{'s' if n_total != 1 else ''}"
                    else:
                        title_text = f"{n_pending} pending finding{'s' if n_pending != 1 else ''}"
                    ui.label(title_text).classes("text-base font-semibold")
                    parts = [
                        f"{n_pending} pending",
                        f"{n_accepted} accepted",
                        f"{n_dismissed} dismissed",
                    ]
                    if n_resolved:
                        parts.append(f"{n_resolved} resolved")
                    ui.label(" · ".join(parts)).classes("text-xs opacity-70")
                with ui.row().classes("gap-2 items-center"):
                    # Show-all toggle. Default OFF — only pending cards
                    # render below, matching the badge count.
                    ui.switch(
                        "Show all",
                        value=state["show_all"],
                        on_change=lambda e: (
                            state.update(show_all=bool(e.value)),
                            render.refresh(),
                        ),
                    ).tooltip(
                        "Off (default) — show pending + accepted "
                        "findings (everything still actionable). "
                        "On — also show dismissed and auto-resolved "
                        "findings for the audit trail."
                    )
                    # Bulk-accept button — accepts every visible pending
                    # finding in one click. Same operation the reviewer
                    # pipeline now runs automatically post-review; this
                    # is the manual escape hatch for findings already in
                    # the DB at pending.
                    if n_pending:

                        def _bulk_accept_handler() -> None:
                            counts = bulk_accept_pending_findings(
                                proposal_id,
                                severity_floor=None,
                            )
                            ui.notify(
                                f"Auto-accepted {counts['accepted']} "
                                f"pending finding"
                                f"{'s' if counts['accepted'] != 1 else ''} "
                                f"(C={counts['by_severity']['CRITICAL']} "
                                f"M={counts['by_severity']['MAJOR']} "
                                f"m={counts['by_severity']['MINOR']}). "
                                f"Click 'Apply N accepted findings' on "
                                f"each section to regenerate.",
                                type="positive",
                                multi_line=True,
                                timeout=5000,
                            )
                            _after_change()

                        ui.button(
                            f"Accept all {n_pending} pending",
                            icon="done_all",
                            on_click=_bulk_accept_handler,
                        ).props("color=positive dense").tooltip(
                            "Bulk-accepts every pending finding. You "
                            "can still dismiss any you disagree with "
                            "before clicking Apply on a section."
                        )
                    ui.button(
                        "Run Auto Loop",
                        icon="all_inclusive",
                        on_click=lambda: _run_reviewer_loop(proposal_id),
                    ).props("color=primary dense")
                    ui.button(
                        "Review only",
                        icon="rate_review",
                        on_click=lambda: _run_reviewer_only(proposal_id),
                    ).props("flat dense size=sm")

            if n_critical_pending:
                with ui.row().classes("items-center gap-2 pt-2"):
                    ui.icon("error").classes("text-red-700")
                    ui.label(
                        f"{n_critical_pending} pending CRITICAL finding"
                        f"{'s' if n_critical_pending != 1 else ''} — review these first."
                    ).classes("text-sm font-medium text-red-900")

        # Next-step guidance — surfaces only when there are accepted
        # findings the user still has to apply (regenerate the section
        # using the findings as a directive). Without this banner the
        # workflow after auto-accept feels ambiguous: "OK they're
        # accepted, now what?" Three clear paths.
        from app.services.cancellation import (
            JOB_AUTO_REVIEW as _JOB_AUTO_REVIEW,
        )
        from app.services.cancellation import (
            is_running as _job_is_running_fn,
        )

        sections_with_accepted = [
            (pk, sec)
            for pk, sec in section_meta.items()
            if any(
                f["accepted_at"] and f["resolved_in_pass_number"] is None
                for f in findings
                if f["section_pk"] == pk
            )
        ]
        len(sections_with_accepted)
        n_apply_ready = sum(1 for f in findings if f["accepted_at"] and f["resolved_in_pass_number"] is None)
        loop_in_progress = _job_is_running_fn(_JOB_AUTO_REVIEW, proposal_id)

        # Split accepted-finding sections by whether a writer is
        # currently regenerating them. After "Apply all → Regenerate"
        # is clicked, every accepted-finding section ends up in the
        # active set; the banner needs to show that work is in flight
        # rather than continuing to invite the user to click Apply.
        sections_in_flight = [(pk, sec) for pk, sec in sections_with_accepted if pk in active_section_pks]
        sections_pending_apply = [
            (pk, sec) for pk, sec in sections_with_accepted if pk not in active_section_pks
        ]
        n_in_flight = len(sections_in_flight)
        n_pending_apply = len(sections_pending_apply)
        n_findings_in_flight = sum(
            1
            for f in findings
            if f["accepted_at"]
            and f["resolved_in_pass_number"] is None
            and f["section_pk"] in active_section_pks
        )
        n_findings_pending_apply = n_apply_ready - n_findings_in_flight

        if n_in_flight and not loop_in_progress:
            # Manual regen is running. Show a green confirmation banner
            # so the user knows their click actually kicked something
            # off — and surface any sections still pending Apply (rare
            # — only when a section was already in flight when the user
            # clicked, so it got skipped).
            with ui.card().classes("w-full bg-green-50 border-l-4 border-green-600"):
                with ui.row().classes("items-start gap-3 w-full"):
                    ui.spinner("dots", size="lg", color="positive")
                    with ui.column().classes("gap-1 flex-1"):
                        ui.label(
                            f"✓ Regenerating {n_in_flight} section"
                            f"{'s' if n_in_flight != 1 else ''} now — "
                            f"applying {n_findings_in_flight} accepted "
                            f"finding"
                            f"{'s' if n_findings_in_flight != 1 else ''}"
                        ).classes("text-base font-semibold text-green-900")
                        with ui.row().classes("flex-wrap gap-1 pt-1"):
                            for _pk, sec in sections_in_flight:
                                ui.chip(
                                    f"{sec['section_id']} regenerating…",
                                    icon="autorenew",
                                ).props("dense color=green-2 text-color=green-9").classes("text-xs")
                        ui.label(
                            "Each section's writer runs ~30-60s. Watch "
                            "live progress on Run Progress; this banner "
                            "and the per-section indicators clear "
                            "automatically when each writer finishes."
                        ).classes("text-xs text-green-900 opacity-80 pt-1")
                        if n_pending_apply:
                            ui.label(
                                f"{n_pending_apply} section"
                                f"{'s' if n_pending_apply != 1 else ''} "
                                f"still pending Apply — those were "
                                f"already in flight when you clicked, "
                                f"so they were skipped. Click Apply "
                                f"again on those sections after the "
                                f"current writers finish."
                            ).classes("text-xs text-amber-800 pt-1 italic")
                    ui.button(
                        "Open Run Progress",
                        icon="open_in_new",
                        on_click=lambda: ui.navigate.to(
                            f"/proposals/{proposal_id}/progress",
                        ),
                    ).props("flat color=positive size=sm")

        elif n_findings_pending_apply and not loop_in_progress:
            with ui.card().classes("w-full bg-blue-50 border-l-4 border-blue-500"):
                with ui.row().classes("items-start justify-between w-full gap-3"):
                    with ui.column().classes("gap-1 flex-1"):
                        ui.label(
                            f"Next step — regenerate "
                            f"{n_pending_apply} section"
                            f"{'s' if n_pending_apply != 1 else ''} "
                            f"to apply {n_findings_pending_apply} "
                            f"accepted finding"
                            f"{'s' if n_findings_pending_apply != 1 else ''}"
                        ).classes("text-base font-semibold text-blue-900")
                        ui.label(
                            "Accepting a finding doesn't change the draft "
                            "yet — the writer needs to regenerate the "
                            "section using the accepted findings as a "
                            "directive. You have three options:"
                        ).classes("text-sm text-blue-900 opacity-80")
                        ui.label(
                            "  • Apply all sections at once (button below) "
                            "— spawns one writer per section in parallel."
                        ).classes("text-xs text-blue-900 opacity-80")
                        ui.label(
                            "  • Click 'Apply N accepted findings → "
                            "regenerate' on each section card individually."
                        ).classes("text-xs text-blue-900 opacity-80")
                        ui.label(
                            "  • Click 'Run Auto Loop' (top right) to "
                            "apply + re-review automatically until clean "
                            "or stuck."
                        ).classes("text-xs text-blue-900 opacity-80")

                    def _apply_all_sections() -> None:
                        from app.services.cancellation import get_active_sections

                        active = get_active_sections(proposal_id)
                        applied = 0
                        skipped_active = 0
                        for pk, _sec in sections_pending_apply:
                            if pk in active:
                                skipped_active += 1
                                continue
                            sec_findings_pk = get_accepted_findings_for_section(pk)
                            if not sec_findings_pk:
                                continue
                            directive = build_directive_from_findings(
                                sec_findings_pk,
                            )
                            spawn_writer_for_section(
                                proposal_id,
                                pk,
                                user_directive=directive,
                            )
                            applied += 1
                        msg_parts = [
                            f"Spawned writer regen on {applied} section{'s' if applied != 1 else ''}"
                        ]
                        if skipped_active:
                            msg_parts.append(f"{skipped_active} skipped (already regenerating)")
                        ui.notify(
                            "; ".join(msg_parts) + ". Watch Run Progress for live updates.",
                            type="positive",
                            multi_line=True,
                            timeout=6000,
                        )
                        # Re-render this tab BEFORE navigating so if
                        # the user comes back (or the navigate is
                        # delayed by the toast), the green
                        # "regenerating now" banner is already up.
                        _after_change()
                        ui.navigate.to(
                            f"/proposals/{proposal_id}/progress",
                        )

                    ui.button(
                        f"Apply all {n_pending_apply} section"
                        f"{'s' if n_pending_apply != 1 else ''} "
                        f"→ regenerate",
                        icon="auto_fix_high",
                        on_click=_apply_all_sections,
                    ).props("color=primary").tooltip(
                        "Spawns the Writer Team on every section that "
                        "has accepted findings, in parallel. Each "
                        "section regenerates with its own accepted "
                        "findings as a directive."
                    )

        # Group by section. Order by what the USER still needs to do:
        # sections with pending findings come first (immediate attention),
        # then sections with accepted findings ready for Apply, then
        # everything else (dismissed/resolved-only sections — informational
        # only, kept for audit trail). Within each bucket, fall back to
        # the natural section_order so neighboring sections in the proposal
        # stay near each other in the UI.
        def _section_priority(pk_sec: tuple) -> tuple:
            pk, _sec = pk_sec
            sec_findings_for_pk = [f for f in findings if f["section_pk"] == pk]
            n_pend = sum(
                1
                for f in sec_findings_for_pk
                if not f["accepted_at"] and not f["dismissed_at"] and f["resolved_in_pass_number"] is None
            )
            n_acc = sum(1 for f in sec_findings_for_pk if f["accepted_at"])
            # Bucket: 0 = needs user attention (pending), 1 = needs Apply
            # click (accepted, no pending), 2 = fully addressed.
            if n_pend > 0:
                bucket = 0
            elif n_acc > 0:
                bucket = 1
            else:
                bucket = 2
            return (bucket, _sec["section_order"], pk)

        ordered_sections = sorted(section_meta.items(), key=_section_priority)
        for sec_pk, sec in ordered_sections:
            sec_findings = [f for f in findings if f["section_pk"] == sec_pk]
            if not sec_findings:
                continue

            n_sec_pending = sum(
                1
                for f in sec_findings
                if not f["accepted_at"] and not f["dismissed_at"] and f["resolved_in_pass_number"] is None
            )
            n_sec_accepted = sum(1 for f in sec_findings if f["accepted_at"])

            # Default view: hide sections that have nothing to act on.
            # "Actions remaining" = pending findings to triage OR accepted
            # findings the user can Apply to regenerate. Sections that are
            # purely accepted-but-resolved or dismissed-only stay hidden
            # unless the user flips the Show-all switch.
            if not state["show_all"]:
                n_sec_apply_ready = sum(
                    1 for f in sec_findings if f["accepted_at"] and f["resolved_in_pass_number"] is None
                )
                if n_sec_pending == 0 and n_sec_apply_ready == 0:
                    continue

            is_in_flight = sec_pk in active_section_pks
            in_flight_tag = " · ⏳ in flight" if is_in_flight else ""
            label = (
                f"#{sec['section_order']} {sec['section_id']} — "
                f"{sec['section_title']}  "
                f"({len(sec_findings)} finding"
                f"{'s' if len(sec_findings) != 1 else ''}"
                f"{f', {n_sec_pending} pending' if n_sec_pending else ''}"
                f"{f', {n_sec_accepted} accepted' if n_sec_accepted else ''}"
                f"{in_flight_tag})"
            )
            # Default-open if there are pending or accepted findings.
            with ui.expansion(
                label,
                icon=("autorenew" if is_in_flight else "article"),
                value=bool(n_sec_pending or n_sec_accepted),
            ).classes("w-full"):
                if is_in_flight:
                    with ui.row().classes(
                        "items-center gap-2 pb-2 px-2 py-1 bg-blue-50 border-l-4 border-blue-400 rounded"
                    ):
                        ui.spinner("dots", color="primary", size="sm")
                        ui.label(
                            "Auto-loop is processing this section now — "
                            "wait until it moves on before Applying. "
                            "Accepting / dismissing findings is still safe."
                        ).classes("text-xs text-blue-900")

                # Section-level action row
                with ui.row().classes("items-center gap-2 pb-2"):
                    # Apply button hidden on the in-flight section to
                    # prevent two concurrent writer threads racing on the
                    # same draft. User can still Accept findings; they
                    # apply on the next manual click after the loop
                    # moves on.
                    if n_sec_accepted and not is_in_flight:
                        ui.button(
                            f"Apply {n_sec_accepted} accepted finding"
                            f"{'s' if n_sec_accepted != 1 else ''} → regenerate",
                            icon="auto_fix_high",
                            on_click=(lambda spk=sec_pk: _apply_accepted_findings(proposal_id, spk)),
                        ).props("color=primary dense")
                    ui.button(
                        "Re-run reviewer on this section",
                        icon="refresh",
                        on_click=(lambda spk=sec_pk: _re_review_section(proposal_id, spk)),
                    ).props("flat dense size=sm")

                # Within a section, order the cards the same way: actionable
                # first, addressed last. Status bucket dominates so the user
                # never has to scroll past a wall of resolved findings to
                # reach a CRITICAL pending one. Severity is the secondary
                # key inside each bucket.
                def _finding_priority(f: dict) -> tuple:
                    if f["resolved_in_pass_number"] is not None:
                        bucket = 3  # resolved — addressed by the loop
                    elif f["dismissed_at"]:
                        bucket = 2  # dismissed — explicitly won't-fix
                    elif f["accepted_at"]:
                        bucket = 1  # accepted — needs Apply click
                    else:
                        bucket = 0  # pending — needs user judgment
                    sev_idx = (
                        _FINDING_SEVERITY_ORDER.index(f["severity"])
                        if f["severity"] in _FINDING_SEVERITY_ORDER
                        else 99
                    )
                    return (bucket, sev_idx, f["id"])

                sec_findings.sort(key=_finding_priority)
                # Default view: pending + accepted cards (everything still
                # actionable — pending needs triage, accepted needs Apply).
                # Dismissed and auto-resolved cards are hidden as audit-
                # trail only; the "Show all" toggle reveals them.
                cards_to_render = (
                    sec_findings
                    if state["show_all"]
                    else [
                        f
                        for f in sec_findings
                        if not f["dismissed_at"] and f["resolved_in_pass_number"] is None
                    ]
                )
                for f in cards_to_render:
                    _render_finding_card(f, on_change=_after_change)

    render()

    # Live polling while ANY writer is in flight on this proposal —
    # auto-loop OR manual per-section regen ("Apply N → regenerate" /
    # "Apply all"). 5s cadence matches Run Progress; idle otherwise so
    # we don't burn CPU/DB queries when nothing's happening.
    #
    # The active-section signal is what makes the green
    # "regenerating now" banner and the per-section in-flight tags
    # auto-clear as each writer finishes — without it, the banner
    # would stay up until the user manually navigated.
    def maybe_refresh() -> None:
        if _job_is_running(JOB_AUTO_REVIEW, proposal_id) or get_active_sections(proposal_id):
            render.refresh()

    ui.timer(5.0, maybe_refresh)


def _render_finding_card(f: dict, on_change) -> None:
    """One finding card with Accept / Dismiss / Unmark actions."""
    sev = f["severity"]
    icon, accent, bg, border = _FINDING_SEVERITY_VISUAL.get(
        sev, ("info", "slate-700", "bg-slate-50", "border-slate-300")
    )
    is_accepted = bool(f["accepted_at"])
    is_dismissed = bool(f["dismissed_at"])
    is_resolved = f["resolved_in_pass_number"] is not None

    if is_resolved:
        bg = "bg-green-50"
        border = "border-green-400"
    elif is_dismissed:
        bg = "bg-slate-50"
        border = "border-slate-300"
    elif is_accepted:
        bg = "bg-blue-50"
        border = "border-blue-400"

    accent_color = accent.split("-")[0]

    with ui.card().classes(f"w-full {bg} border-l-4 {border}"):
        with ui.row().classes("items-start gap-3 w-full"):
            ui.icon(icon).classes(f"text-{accent} text-xl pt-0.5")
            with ui.column().classes("gap-0 flex-1"):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.chip(sev).props(f"dense color={accent_color}-2 text-color={accent_color}-9")
                    ui.chip(_FINDING_CATEGORY_LABELS.get(f["category"], f["category"])).props(
                        "dense color=slate-2 text-color=slate-9"
                    ).classes("text-xs")
                    ui.label(f"Reviewer {f['reviewer_agent']} · pass {f['pass_number']}").classes(
                        "text-xs opacity-60 font-mono"
                    )
                    if is_resolved:
                        ui.chip(
                            f"resolved (pass {f['resolved_in_pass_number']})",
                            icon="check_circle",
                        ).props("dense color=green-2 text-color=green-9")
                    elif is_accepted:
                        ui.chip("accepted", icon="check").props("dense color=blue-2 text-color=blue-9")
                    elif is_dismissed:
                        ui.chip("dismissed", icon="block").props("dense color=slate-3 text-color=slate-8")
                ui.label(f["finding_text"]).classes("text-sm pt-1 whitespace-pre-wrap")
                if f.get("suggested_fix"):
                    with ui.row().classes("items-start gap-1 pt-1"):
                        ui.icon("lightbulb").classes("text-amber-700 text-sm pt-0.5")
                        ui.label(f["suggested_fix"]).classes(
                            "text-xs italic opacity-80 whitespace-pre-wrap flex-1"
                        )
                if is_dismissed and f.get("dismissed_reason"):
                    ui.label(f"Reason: {f['dismissed_reason']}").classes("text-xs opacity-70 pt-1")

            if not is_resolved:
                with ui.column().classes("gap-1 items-stretch"):
                    if not is_accepted:
                        ui.button(
                            "Accept",
                            icon="check",
                            on_click=(lambda fid=f["id"]: _accept_finding_action(fid, on_change)),
                        ).props("color=primary dense size=sm")
                    if not is_dismissed:
                        ui.button(
                            "Dismiss",
                            icon="block",
                            on_click=(lambda fid=f["id"]: _open_dismiss_dialog(fid, on_change)),
                        ).props("flat dense size=sm color=slate-7")
                    if is_accepted or is_dismissed:
                        ui.button(
                            "Unmark",
                            icon="undo",
                            on_click=(lambda fid=f["id"]: _unmark_finding_action(fid, on_change)),
                        ).props("flat dense size=sm")


def _accept_finding_action(finding_id: int, on_change) -> None:
    if accept_finding(finding_id):
        # User-driven accept = signal worth learning from. Spawn an async
        # rule extraction; it'll show up as a draft entry on the Learned
        # Guidance tab for the user to approve. Never blocks the UI.
        # NOTE: the auto-loop also calls accept_finding(), but it does so
        # directly via the service — not through this UI helper — so loop
        # accepts do not trigger extraction.
        schedule_extract_on_accept(finding_id)
        ui.notify(
            "Finding accepted — click 'Apply accepted findings' on the section to regenerate with it.",
            type="positive",
        )
        on_change()
    else:
        ui.notify("Could not accept finding.", type="negative")


def _unmark_finding_action(finding_id: int, on_change) -> None:
    if unmark_finding(finding_id):
        ui.notify("Cleared.", type="positive")
        on_change()


def _open_dismiss_dialog(finding_id: int, on_change) -> None:
    """Confirmation + optional reason dialog for dismissing a finding."""
    with ui.dialog() as dlg, ui.card().classes("min-w-[28rem]"):
        ui.label("Dismiss finding?").classes("text-base font-semibold")
        ui.label(
            "Dismissed findings stay in the audit trail but won't be applied by the next regenerate."
        ).classes("text-sm opacity-80 pt-1")
        reason_input = (
            ui.textarea(
                "Reason (optional)",
                placeholder="e.g., 'Not applicable to this RFP', 'Already covered in §X'",
            )
            .classes("w-full pt-2")
            .props("autogrow rounded outlined")
        )
        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Cancel", on_click=dlg.close).props("flat")

            def apply() -> None:
                ok = dismiss_finding(finding_id, reason=reason_input.value or "")
                dlg.close()
                if ok:
                    # Dismiss-with-reason is the strongest signal we get
                    # for reviewer-calibration rules. The extractor only
                    # runs when a reason was provided; reason-less
                    # dismissals are skipped (no signal to generalize).
                    schedule_extract_on_dismiss(finding_id)
                    ui.notify("Finding dismissed.", type="positive")
                    on_change()
                else:
                    ui.notify("Could not dismiss finding.", type="negative")

            ui.button("Dismiss", icon="block", on_click=apply).props("color=red-7")
    dlg.open()
