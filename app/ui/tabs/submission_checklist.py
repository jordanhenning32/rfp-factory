"""Proposal Review > Submission Checklist tab.

Interactive checklist of items the user needs to attach/submit for
the proposal package. Three sub-sections:

  1. System-verified readiness (auto-checked, no user clicking) —
     team approval, cost build, no deal-breakers, sections drafted, etc.
  2. User-tickable compliance items — sourced from compliance items
     where requirement_type=mandatory_form (the Compliance Matrix Agent
     flags these). Plus items in the 'certification' category.
  3. Drafting commitments — user-flagged 'we will deliver X' items
     captured from the Provide-value placeholder dialog.
"""

from __future__ import annotations

from nicegui import ui
from sqlalchemy import select

from app.db.session import SessionLocal, session_scope
from app.models import ComplianceMatrixItem
from app.ui._shared import _empty_state


def _render_system_verified_section(proposal_id: int) -> None:
    """System-verifiable readiness checks rendered ABOVE the user-
    tickable compliance items on the Submission Checklist tab.

    Each check is computed deterministically (no LLM, no user
    toggle) from the proposal's data — team approval, cost build,
    no deal-breakers, sections drafted, NEEDS_HUMAN resolved,
    findings actioned, etc. Verified rows render with a green
    check; unverified ones show the gap so the user knows what's
    blocking submission. Refreshes whenever the parent tab does."""
    from app.services.submission_commitments import (
        compute_system_verified_items,
    )

    items = compute_system_verified_items(proposal_id)
    if not items:
        return
    n_total = len(items)
    n_verified = sum(1 for i in items if i.get("verified"))

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center w-full gap-3 flex-wrap"):
            ui.label("System-verified readiness").classes("text-base font-medium")
            ui.chip(
                f"{n_verified}/{n_total} verified",
                icon=("check_circle" if n_verified == n_total else "pending"),
            ).props(f"color={'green-7' if n_verified == n_total else 'amber-7'} text-color=white")
        ui.label(
            "Auto-checked from proposal data — no clicking required. "
            "Verified rows are confirmed; unverified rows tell you "
            "what's still blocking submission."
        ).classes("text-xs opacity-70")

        for it in items:
            verified = bool(it.get("verified"))
            severity = it.get("severity", "info")
            if verified:
                icon, icon_cls = "check_circle", "text-green-600"
            elif severity == "critical":
                icon, icon_cls = "error", "text-red-600"
            elif severity == "warning":
                icon, icon_cls = "schedule", "text-amber-600"
            else:
                icon, icon_cls = "info", "text-slate-500"
            with ui.row().classes("items-center gap-2 w-full pt-1 pl-1"):
                ui.icon(icon).classes(f"{icon_cls} text-lg")
                ui.label(it.get("label") or "").classes("text-sm flex-1")
                detail = it.get("detail") or ""
                if detail:
                    ui.label(detail).classes("text-xs font-mono opacity-70")


def _render_submission_checklist_tab(
    proposal_id: int,
    *,
    on_state_change=None,
) -> None:
    """Submission Checklist tab — interactive checklist of items the user
    needs to attach/submit for the proposal package.

    Two sections:
      1. System-verified readiness (auto-checked, no user clicking) —
         team approval, cost build, no deal-breakers, sections
         drafted, etc.
      2. User-tickable compliance items — sourced from compliance
         items where requirement_type=mandatory_form (the Compliance
         Matrix Agent flags these). Plus items in the 'certification'
         category, which often map to documents like business-license
         proof, SAM registration confirmation, etc.
    """
    state: dict = {"hide_obtained": False}

    def _toggle_obtained(item_pk: int, value: bool) -> None:
        with session_scope() as db:
            ci = db.get(ComplianceMatrixItem, item_pk)
            if ci is not None:
                ci.submission_obtained = value

    def _after_change() -> None:
        render.refresh()
        if on_state_change is not None:
            on_state_change()

    @ui.refreshable
    def render() -> None:
        with SessionLocal() as db:
            items = (
                db.execute(
                    select(ComplianceMatrixItem)
                    .where(
                        ComplianceMatrixItem.proposal_id == proposal_id,
                        ComplianceMatrixItem.status == "active",
                    )
                    .where(
                        ComplianceMatrixItem.requirement_type.in_(["mandatory_form"])
                        | (ComplianceMatrixItem.category == "certification")
                    )
                    .order_by(ComplianceMatrixItem.id)
                )
                .scalars()
                .all()
            )
            snapshot = [
                {
                    "id": ci.id,
                    "req_id": ci.requirement_id,
                    "req_text": ci.requirement_text,
                    "type": ci.requirement_type.value
                    if hasattr(ci.requirement_type, "value")
                    else str(ci.requirement_type),
                    "category": ci.category.value if hasattr(ci.category, "value") else str(ci.category),
                    "source": (
                        f"{ci.source_doc}"
                        + (f" §{ci.source_section}" if ci.source_section else "")
                        + (f" p.{ci.source_page}" if ci.source_page else "")
                    ),
                    "obtained": bool(ci.submission_obtained),
                }
                for ci in items
            ]

        # System-verified readiness section — always rendered first
        # so the user sees the auto-checked items before the manual
        # ones. No clicking required; just status.
        _render_system_verified_section(proposal_id)

        if not snapshot:
            _empty_state(
                "No mandatory submission items detected by the Compliance Matrix Agent. "
                "If the RFP has required forms or certifications, they may be in other "
                "categories — check the Compliance tab.",
                icon="checklist",
            )
            return

        n_total = len(snapshot)
        n_obtained = sum(1 for s in snapshot if s["obtained"])
        n_outstanding = n_total - n_obtained

        with ui.row().classes("items-center w-full gap-3 pt-2 flex-wrap"):
            ui.chip(f"{n_total} item{'s' if n_total != 1 else ''}", icon="checklist").props(
                "color=primary text-color=white"
            )
            ui.chip(f"{n_obtained} obtained", icon="check_circle").props(
                f"color={'green-7' if n_obtained else 'blue-grey-6'} text-color=white"
            )
            ui.chip(f"{n_outstanding} outstanding", icon="pending_actions").props(
                f"color={'amber-7' if n_outstanding else 'blue-grey-6'} text-color=white"
            )
            ui.element("div").classes("flex-1")

            def _toggle_hide() -> None:
                state["hide_obtained"] = not state["hide_obtained"]
                render.refresh()

            ui.button(
                "Hide obtained" if not state["hide_obtained"] else "Show all",
                icon="visibility_off" if not state["hide_obtained"] else "visibility",
                on_click=_toggle_hide,
            ).props("flat dense size=sm")

        ui.label(
            "Tick each item once you've gathered the document or completed the "
            "registration. The Writer Team / Final Polish will need this when assembling "
            "the submission package."
        ).classes("text-xs opacity-60")

        rows = [s for s in snapshot if not s["obtained"]] if state["hide_obtained"] else snapshot
        if not rows:
            ui.label("All items obtained — checklist complete.").classes(
                "text-sm font-medium text-green-700 py-4"
            )
            return

        for s in rows:
            with ui.card().classes("w-full" + (" opacity-60" if s["obtained"] else "")):
                with ui.row().classes("items-start w-full gap-3"):
                    cb = ui.checkbox(
                        "",
                        value=s["obtained"],
                        on_change=lambda e, pk=s["id"]: (
                            _toggle_obtained(pk, e.value),
                            _after_change(),
                        ),
                    )
                    cb.props("size=md")
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(s["req_text"]).classes(
                            "text-sm font-medium" + (" line-through" if s["obtained"] else "")
                        )
                        meta = f"{s['req_id']}  ·  {s['type']}  ·  {s['category']}"
                        ui.label(meta).classes("text-xs font-mono opacity-60")
                        ui.label(f"Source: {s['source']}").classes("text-xs opacity-60")

        # ---- Drafting commitments ---------------------------------------
        # User-flagged "we will deliver X" items captured from the
        # Provide-value placeholder dialog (or future agent extraction).
        # Lives in the submission_commitments table, not the matrix.
        from app.services.submission_commitments import (
            delete_commitment,
            list_submission_commitments,
            set_commitment_obtained,
        )

        def _toggle_commitment(pk: int, value: bool) -> None:
            set_commitment_obtained(pk, value)

        def _remove_commitment(pk: int) -> None:
            if delete_commitment(pk):
                ui.notify("Commitment removed.", type="positive")
                render.refresh()
                if on_state_change is not None:
                    on_state_change()

        commits = list_submission_commitments(proposal_id)
        if commits:
            ui.label("Drafting commitments").classes("text-base font-medium pt-6")
            ui.label(
                "Artifacts the proposal volunteered to deliver, captured "
                "when you ticked 'Add to Submission Checklist' in a "
                "Provide-value dialog. Tick obtained as you gather each "
                "one; Remove if a commitment is no longer in the draft."
            ).classes("text-xs opacity-60 pb-2")
            visible_commits = [c for c in commits if not c["obtained"]] if state["hide_obtained"] else commits
            if not visible_commits:
                ui.label("All commitments obtained — fully gathered.").classes(
                    "text-sm font-medium text-green-700 py-4"
                )
            for c in visible_commits:
                with ui.card().classes(
                    "w-full bg-emerald-50/40 border-l-4 border-emerald-300"
                    + (" opacity-60" if c["obtained"] else "")
                ):
                    with ui.row().classes("items-start w-full gap-3"):
                        ui.checkbox(
                            "",
                            value=c["obtained"],
                            on_change=lambda e, pk=c["id"]: (
                                _toggle_commitment(pk, e.value),
                                _after_change(),
                            ),
                        ).props("size=md")
                        with ui.column().classes("gap-0 flex-1"):
                            ui.label(c["description"]).classes(
                                "text-sm font-medium" + (" line-through" if c["obtained"] else "")
                            )
                            src_label = {
                                "needs_human_apply": "from a placeholder resolution",
                                "manual": "manually added",
                                "draft_extraction": "auto-extracted from draft",
                            }.get(c["source"], c["source"])
                            ui.label(
                                f"Source: {src_label}"
                                + (
                                    f"  ·  Section pk #{c['source_section_id']}"
                                    if c["source_section_id"]
                                    else ""
                                )
                            ).classes("text-xs opacity-60")
                            if c["notes"]:
                                ui.label(f"Notes: {c['notes']}").classes("text-xs italic opacity-70 pt-1")
                        ui.button(
                            icon="close",
                            on_click=lambda pk=c["id"]: _remove_commitment(pk),
                        ).props("flat dense round size=sm color=red-7").tooltip(
                            "Remove this commitment from the checklist. (Doesn't change the draft text.)"
                        )

    render()
