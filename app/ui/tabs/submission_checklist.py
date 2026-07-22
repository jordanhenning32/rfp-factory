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

from app.db.session import SessionLocal
from app.models import ComplianceMatrixItem
from app.services.submission_commitments import set_rfp_required_item_obtained
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


def _render_drafting_commitments(
    proposal_id: int,
    *,
    hide_obtained: bool,
    on_change,
) -> None:
    """Render user-captured drafting commitments independently of matrix rows.

    A proposal can legitimately have commitments without any compliance items
    classified as ``mandatory_form`` or ``certification``.  Keeping this block
    independent prevents the manual-checklist empty state from hiding those
    commitments.
    """
    from app.services.submission_commitments import (
        add_submission_commitment,
        delete_commitment,
        list_submission_commitments,
        set_commitment_obtained,
        update_commitment,
    )

    def _toggle_commitment(pk: int, value: bool) -> None:
        set_commitment_obtained(pk, value)
        on_change()

    def _open_commitment_editor(commitment: dict | None = None) -> None:
        is_edit = commitment is not None
        current = commitment or {}
        with ui.dialog() as dialog, ui.card().classes(
            "w-full max-w-xl"
        ):
            ui.label(
                "Edit commitment" if is_edit else "Add commitment"
            ).classes("text-base font-medium")
            ui.label(
                "Track an artifact or deliverable that must be ready with "
                "the final submission package."
            ).classes("text-xs opacity-60")
            description_input = ui.textarea(
                "Commitment",
                value=current.get("description") or "",
                placeholder=(
                    "e.g., Export the approved transition evidence matrix"
                ),
            ).classes("w-full").props("autogrow rows=3")
            notes_input = ui.textarea(
                "Notes (optional)",
                value=current.get("notes") or "",
                placeholder="Owner, location, due date, or final export step",
            ).classes("w-full").props("autogrow rows=2")

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")

                def _save() -> None:
                    description = (
                        description_input.value or ""
                    ).strip()
                    if not description:
                        ui.notify(
                            "Commitment is required.", type="warning"
                        )
                        return
                    notes = (notes_input.value or "").strip()
                    if is_edit:
                        ok = update_commitment(
                            int(current["id"]),
                            description=description,
                            notes=notes,
                        )
                        if not ok:
                            ui.notify(
                                "Could not update commitment.",
                                type="negative",
                            )
                            return
                        message = "Commitment updated."
                    else:
                        add_submission_commitment(
                            proposal_id=proposal_id,
                            description=description,
                            source="manual",
                            notes=notes or None,
                        )
                        message = "Commitment added."
                    dialog.close()
                    ui.notify(message, type="positive")
                    on_change()

                ui.button(
                    "Save", icon="save", on_click=_save
                ).props("color=primary")
        dialog.open()

    def _confirm_remove(commitment: dict) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
            ui.label("Remove commitment?").classes(
                "text-base font-medium"
            )
            ui.label(commitment["description"]).classes(
                "text-sm opacity-80"
            )
            ui.label(
                "This removes only the checklist record; it does not "
                "change proposal draft text."
            ).classes("text-xs opacity-60")
            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")

                def _remove() -> None:
                    ok = delete_commitment(int(commitment["id"]))
                    dialog.close()
                    if ok:
                        ui.notify("Commitment removed.", type="positive")
                        on_change()
                    else:
                        ui.notify(
                            "Could not remove commitment.", type="negative"
                        )

                ui.button(
                    "Remove", icon="delete", on_click=_remove
                ).props("color=negative")
        dialog.open()

    commits = list_submission_commitments(proposal_id)
    with ui.row().classes("items-center w-full gap-2 pt-6"):
        ui.label("Drafting commitments").classes(
            "text-base font-medium"
        )
        ui.element("div").classes("flex-1")
        ui.button(
            "Add commitment",
            icon="add",
            on_click=lambda: _open_commitment_editor(),
        ).props("flat dense color=primary")
    ui.label(
        "Artifacts the proposal volunteered to deliver. Add one here "
        "or capture it from a Provide-value dialog, then tick obtained "
        "as you gather it."
    ).classes("text-xs opacity-60 pb-2")
    if not commits:
        ui.label("No user-tracked commitments yet.").classes(
            "text-sm opacity-60 py-3"
        )
        return

    visible_commits = (
        [c for c in commits if not c["obtained"]]
        if hide_obtained
        else commits
    )
    if not visible_commits:
        ui.label(
            "All commitments obtained — fully gathered."
        ).classes("text-sm font-medium text-green-700 py-4")
    for c in visible_commits:
        with ui.card().classes(
            "w-full bg-emerald-50/40 border-l-4 border-emerald-300"
            + (" opacity-60" if c["obtained"] else "")
        ):
            with ui.row().classes("items-start w-full gap-3"):
                commitment_checkbox = ui.checkbox(
                    "", value=c["obtained"],
                    on_change=lambda e, pk=c["id"]: _toggle_commitment(
                        pk, e.value,
                    ),
                ).props("size=md")
                commitment_checkbox.props["aria-label"] = (
                    f"Obtained status for drafting commitment {c['id']}: "
                    f"{c['description']}"
                )
                with ui.column().classes("gap-0 flex-1"):
                    ui.label(c["description"]).classes(
                        "text-sm font-medium"
                        + (" line-through" if c["obtained"] else "")
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
                            if c["source_section_id"] else ""
                        )
                    ).classes("text-xs opacity-60")
                    if c["notes"]:
                        ui.label(f"Notes: {c['notes']}").classes(
                            "text-xs italic opacity-70 pt-1"
                        )
                with ui.column().classes("gap-1 items-stretch"):
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=lambda item=c: _open_commitment_editor(item),
                    ).props("flat dense size=sm color=blue-grey-7")
                    ui.button(
                        "Remove",
                        icon="delete_outline",
                        on_click=lambda item=c: _confirm_remove(item),
                    ).props("flat dense size=sm color=red-7")


def _render_submission_checklist_tab(
    proposal_id: int, *, on_state_change=None,
):
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
        set_rfp_required_item_obtained(item_pk, value)

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
            _render_drafting_commitments(
                proposal_id,
                hide_obtained=state["hide_obtained"],
                on_change=_after_change,
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
            ui.label(
                "All items obtained — checklist complete."
            ).classes("text-sm font-medium text-green-700 py-4")
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

        # Drafting commitments are independent of matrix-derived checklist
        # rows and therefore render whether or not this proposal has any.
        _render_drafting_commitments(
            proposal_id,
            hide_obtained=state["hide_obtained"],
            on_change=_after_change,
        )

    render()
    # The proposal page uses this callback to keep this tab's deterministic
    # readiness snapshot current when another tab mutates proposal state
    # (for example, resolving a Draft placeholder). ``getattr`` preserves the
    # plain-function test double used by unit tests.
    return getattr(render, "refresh", render)
