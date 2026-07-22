"""Proposal Review > Outline tab.

Shows the section structure proposed by the Outline Agent. Per-section
controls let the user toggle "cost-deferred" (Reviewer + Writer Team
skip the section) or "excluded from draft" (Writer Team skips entirely
— no draft generated).

Empty states branch by phase:
  - intaking / awaiting_scope_signoff: "sign off scope first"
  - drafting (post-signoff, no outline yet): "Generate Draft Outline" CTA
  - awaiting_outline_approval: outline rendered + approve/regenerate buttons
  - draft_in_progress / draft_ready: outline rendered, read-only,
    regenerate warns about wiping drafts
"""

from __future__ import annotations

import logging

from nicegui import ui
from nicegui.elements.switch import Switch
from sqlalchemy import select

from app.core.enums import ProposalStatus
from app.db.session import SessionLocal, session_scope
from app.jobs.outline import _is_outline_relevant, spawn_outline_generation
from app.models import (
    ComplianceMatrixItem,
    Proposal,
    ProposalSection,
)
from app.services.sections import (
    assign_compliance_item_to_section,
    mark_compliance_item_outline_excluded,
    set_section_cost_deferred,
    set_section_excluded_from_draft,
)
from app.ui._shared import _empty_state

log = logging.getLogger(__name__)


class _ImmediateSwitch(Switch):
    """A switch whose visual value changes before the server round trip.

    NiceGUI switches use server loopback by default.  A user can therefore
    click the same apparent value twice while the first change is being
    persisted, which loses a quick reversal.  This outline control is kept
    mounted and owns its value locally while the server persists each ordered
    change.
    """

    LOOPBACK = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # NiceGUI's generic non-loopback handler assigns the complete event
        # argument array to ``model-value``.  QToggle expects the scalar bool,
        # so finish the same browser event by assigning that scalar locally.
        self.on(
            "update:model-value",
            js_handler="(value) => { element.props['model-value'] = value; }",
        )


def _generate_outline(proposal_id: int) -> None:
    """Kick off the Outline Agent in a background thread. Status will move
    to AWAITING_OUTLINE_APPROVAL when it completes."""
    spawn_outline_generation(proposal_id)
    ui.notify(
        "Outline Agent running — watch Run Progress for live status. "
        "Outline appears on the Outline tab when it finishes (~30-60s).",
        type="positive",
        multi_line=True,
        timeout=6000,
    )
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _approve_outline(proposal_id: int) -> None:
    """User-approves the outline. Phase 2B: this no longer kicks
    off the Writer Team — it transitions to AWAITING_TEAM_APPROVAL,
    routing the user to the Team tab next. Drafting only starts
    after Team + Cost are in place (see _begin_drafting)."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            ui.notify("Proposal not found.", type="negative")
            return
        # Only advance when we're at the outline-approval gate;
        # avoid clobbering later statuses if the user re-clicks.
        if p.status == ProposalStatus.AWAITING_OUTLINE_APPROVAL:
            p.status = ProposalStatus.AWAITING_TEAM_APPROVAL
    ui.notify(
        "Outline approved. Open the Team tab next — assign people "
        "and time allocations BEFORE the cost build and the draft, "
        "so the writer has real names and numbers from the start.",
        type="positive",
        multi_line=True,
        timeout=6000,
    )
    # Hard reload — NiceGUI same-path nav is a no-op (hash-only change
    # doesn't re-render), so the green "Outline approved" chip and the
    # next-step banner stay stuck on the pre-approval state otherwise.
    ui.navigate.reload()


def _regenerate_outline(proposal_id: int) -> None:
    """Re-run the Outline Agent. Wipes any current sections (drafts included)."""
    spawn_outline_generation(proposal_id)
    ui.notify(
        "Outline regenerating — any prior section drafts will be discarded.",
        type="warning",
        multi_line=True,
        timeout=6000,
    )
    ui.navigate.to(f"/proposals/{proposal_id}/progress")


def _render_outline_tab(
    proposal_id: int,
    status_val: str,
    *,
    on_state_change=None,
) -> None:
    """Outline tab — shows the section structure proposed by the Outline Agent.

    Empty states by phase:
      - intaking / awaiting_scope_signoff: "sign off scope first"
      - drafting (post-signoff, no outline yet): "Generate Draft Outline" CTA
      - awaiting_outline_approval: outline rendered + approve/regenerate buttons
      - draft_in_progress / draft_ready: outline rendered, read-only, regenerate
        warns about wiping drafts

    Per-section: Cost-deferred toggle. The Outline Agent's auto-detection is
    imperfect (it missed SEC-016 'Total Cost Considerations' on a real run);
    this lets the user mark a section cost-deferred manually so Reviewer +
    Writer Team skip it. Mid-loop toggles take effect on the next pass.
    """

    def _after_change() -> None:
        render.refresh()
        if on_state_change is not None:
            on_state_change()

    @ui.refreshable
    def render() -> None:
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
                    "section_brief": s.section_brief or "",
                    "page_limit": s.page_limit,
                    "word_limit": s.word_limit,
                    "requires_cost_analysis": bool(s.requires_cost_analysis),
                    "excluded_from_draft": bool(s.excluded_from_draft),
                    "compliance_items_addressed": list(s.compliance_items_addressed_json or []),
                    "has_draft": bool(s.draft_text_markdown),
                    "revision": s.current_revision_number or 0,
                }
                for s in sec_rows
            ]
            all_req_ids: list[str] = []
            outline_eligible_ids: set[str] = set()
            req_text_by_id: dict[str, str] = {}
            for ci in (
                db.execute(
                    select(ComplianceMatrixItem).where(
                        ComplianceMatrixItem.proposal_id == proposal_id,
                        ComplianceMatrixItem.status == "active",
                    )
                )
                .scalars()
                .all()
            ):
                all_req_ids.append(ci.requirement_id)
                rt = (
                    ci.requirement_type.value
                    if hasattr(ci.requirement_type, "value")
                    else str(ci.requirement_type)
                )
                cat = ci.category.value if hasattr(ci.category, "value") else str(ci.category)
                # User-marked-N/A items are filtered out of outline
                # eligibility so they don't keep showing up in the
                # unassigned-items warning.
                if ci.excluded_from_outline:
                    continue
                if _is_outline_relevant(rt, cat):
                    outline_eligible_ids.add(ci.requirement_id)
                    req_text_by_id[ci.requirement_id] = ci.requirement_text or ""
            checklist_handled_ids = set(all_req_ids) - outline_eligible_ids

        if not sections:
            if status_val in ("intaking", "awaiting_scope_signoff"):
                _empty_state(
                    "Outline not available yet — sign off scope on the Gaps tab "
                    "to enable outline generation.",
                    icon="rule_folder",
                )
            elif status_val == "drafting":
                with ui.column().classes("items-center justify-center w-full py-12 gap-3"):
                    ui.icon("rule_folder", size="xl").classes("opacity-60")
                    ui.label(
                        "No outline yet. Click 'Generate Draft Outline' on the banner "
                        "above to run the Outline Agent."
                    ).classes("text-base opacity-80")
                    ui.button(
                        "Generate Draft Outline",
                        icon="rule_folder",
                        on_click=lambda: _generate_outline(proposal_id),
                    ).props("color=primary")
            else:
                _empty_state(f"No outline data available (status: {status_val}).")
            return

        # Coverage check — what's not assigned anywhere?
        # Only count outline-eligible items as "unassigned." Items
        # handled by the Submission Checklist (mandatory_form, cert,
        # submission_format) are intentionally absent from the outline
        # by design and shouldn't show up in the warning.
        assigned_ids = {rid for s in sections for rid in s["compliance_items_addressed"]}
        unassigned = sorted(outline_eligible_ids - assigned_ids)
        duplicated: dict[str, list[str]] = {}
        for s in sections:
            for rid in s["compliance_items_addressed"]:
                duplicated.setdefault(rid, []).append(s["section_id"])
        duplicated = {k: v for k, v in duplicated.items() if len(v) > 1}

        n_cost_deferred = sum(1 for s in sections if s["requires_cost_analysis"])
        n_eligible = len(outline_eligible_ids)
        n_eligible_assigned = len(assigned_ids & outline_eligible_ids)
        n_checklist = len(checklist_handled_ids)

        def _outline_stats_text() -> str:
            """Build the summary from the current in-memory control state."""
            stat_parts = [
                f"{n_eligible_assigned} of {n_eligible} narrative items mapped",
            ]
            if unassigned:
                stat_parts.append(f"{len(unassigned)} unassigned")
            if duplicated:
                stat_parts.append(f"{len(duplicated)} duplicated")
            if n_checklist:
                stat_parts.append(f"{n_checklist} on Submission Checklist")
            if n_cost_deferred:
                stat_parts.append(f"{n_cost_deferred} cost-deferred")
            n_excluded = sum(
                1 for section in sections if section["excluded_from_draft"]
            )
            if n_excluded:
                stat_parts.append(f"{n_excluded} excluded from draft")
            return " · ".join(stat_parts)

        # Action row at top — varies by status.
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.column().classes("gap-0"):
                    ui.label(
                        f"{len(sections)} section{'s' if len(sections) != 1 else ''}"
                    ).classes("text-base font-semibold")
                    outline_stats_label = ui.label(
                        _outline_stats_text()
                    ).classes("text-xs opacity-70")

                with ui.row().classes("gap-2 items-center"):
                    # Status past awaiting_outline_approval = outline has
                    # been approved. Show a persistent green chip so the
                    # user can see the approval state on return visits to
                    # this tab.
                    _OUTLINE_APPROVED_STATUSES = (
                        "awaiting_team_approval",
                        "awaiting_cost_build",
                        "awaiting_draft",
                        "draft_in_progress",
                        "draft_ready",
                        "reviewing",
                        "approved",
                        "submitted",
                    )
                    if status_val in _OUTLINE_APPROVED_STATUSES:
                        ui.chip(
                            "Outline approved",
                            icon="check_circle",
                        ).props("color=green-7 text-color=white").tooltip(
                            "This outline has been approved. Regenerating "
                            "after drafting starts will wipe drafts."
                        )
                    if status_val == "awaiting_outline_approval":
                        ui.button(
                            "Approve Outline",
                            icon="rule_folder",
                            on_click=lambda: _approve_outline(proposal_id),
                        ).props("color=primary").tooltip(
                            "Approving the outline moves you to the Team tab. "
                            "Drafting starts after the team is approved and "
                            "the cost build is ready."
                        )
                    if status_val in (
                        "drafting",
                        "awaiting_outline_approval",
                        "draft_in_progress",
                        "draft_ready",
                    ):
                        label = "Regenerate Outline"
                        if status_val in ("draft_in_progress", "draft_ready"):
                            label += " (wipes drafts)"
                        ui.button(
                            label,
                            icon="refresh",
                            on_click=lambda: _regenerate_outline(proposal_id),
                        ).props("flat color=warning")

        if unassigned:
            with ui.card().classes("w-full bg-amber-50 border-l-4 border-amber-500"):
                ui.label(f"⚠ {len(unassigned)} narrative item(s) unassigned by the Outline Agent").classes(
                    "text-sm font-medium text-amber-900"
                )
                ui.label(
                    "Pick a section to assign each item to (saves immediately), "
                    "or click 'Regenerate Outline' above to let the Outline "
                    "Agent re-map. Form-fill / certification / submission-format "
                    "items are handled by the Submission Checklist tab and are "
                    "NOT counted here."
                ).classes("text-xs text-amber-700 pt-1 pb-2")

                # Sentinel value for the "Mark N/A" choice — distinct
                # from any real section pk (always positive ints).
                _NA_VALUE = "__na__"
                dropdown_options: dict = {
                    _NA_VALUE: "Mark N/A — not a narrative item",
                }
                for s in sections:
                    dropdown_options[s["pk"]] = f"{s['section_id']} — {s['section_title']}"

                def _do_assign(req_id: str, value) -> None:
                    if value is None:
                        return
                    if value == _NA_VALUE:
                        if mark_compliance_item_outline_excluded(
                            proposal_id=proposal_id,
                            req_id=req_id,
                            excluded=True,
                        ):
                            ui.notify(
                                f"Marked {req_id} as N/A — removed from the unassigned warning.",
                                type="positive",
                            )
                            _after_change()
                        else:
                            ui.notify(
                                f"Could not mark {req_id} as N/A (item not found).",
                                type="negative",
                            )
                        return
                    section_pk = value
                    if assign_compliance_item_to_section(
                        req_id=req_id,
                        section_pk=section_pk,
                    ):
                        ui.notify(
                            f"Assigned {req_id} to {dropdown_options.get(section_pk, '?')}",
                            type="positive",
                        )
                        _after_change()
                    else:
                        ui.notify(
                            f"{req_id} is already on that section.",
                            type="warning",
                        )

                for req_id in unassigned:
                    req_text = req_text_by_id.get(req_id, "") or ("(no requirement text available)")
                    with ui.row().classes("w-full items-start gap-3 pt-2 pb-2 border-t border-amber-200"):
                        ui.label(req_id).classes(
                            "text-xs font-mono text-amber-900 font-medium w-20 shrink-0 pt-1"
                        )
                        ui.label(req_text).classes(
                            "text-xs text-amber-800 flex-1 whitespace-normal leading-snug"
                        )
                        ui.select(
                            options=dropdown_options,
                            label="Assign to section…",
                            on_change=(lambda e, rid=req_id: _do_assign(rid, e.value)),
                        ).classes("min-w-80 shrink-0").props("dense outlined")

        # Section cards
        for s in sections:
            with ui.card().classes("w-full"):
                with ui.row().classes("items-start justify-between w-full"):
                    with ui.column().classes("gap-0 flex-1"):
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            ui.label(f"#{s['section_order']}").classes("text-xs font-mono opacity-60")
                            ui.label(s["section_id"]).classes(
                                "text-xs font-mono px-1.5 py-0.5 bg-slate-100 rounded"
                            )
                            ui.label(s["section_title"]).classes("text-base font-semibold")
                            if s["has_draft"]:
                                ui.chip(
                                    f"drafted (rev {s['revision']})",
                                    icon="check_circle",
                                ).props("color=green-3 text-color=green-9 dense")
                            if s["requires_cost_analysis"]:
                                ui.chip(
                                    "Cost section",
                                    icon="payments",
                                ).props("color=purple-2 text-color=purple-9 dense").tooltip(
                                    "Drafted by the Cost Writer after "
                                    "pricing is built — not by the regular "
                                    "Writer Team."
                                )
                            excluded_chip = ui.chip(
                                "Excluded from draft",
                                icon="block",
                            ).props(
                                "color=amber-3 text-color=amber-9 dense"
                            ).tooltip(
                                "Writer Team will skip this section "
                                "entirely — no draft is generated."
                            )
                            excluded_chip.set_visibility(
                                s["excluded_from_draft"]
                            )
                        if s["section_brief"]:
                            ui.label(s["section_brief"]).classes("text-sm opacity-80 pt-1")
                    with ui.column().classes("gap-1 items-end"):
                        if s["page_limit"]:
                            ui.label(f"{s['page_limit']} pp").classes("text-xs opacity-70")
                        if s["word_limit"]:
                            ui.label(f"{s['word_limit']:,} words").classes("text-xs opacity-70")
                        # Cost-deferred is now auto-detected by the Outline
                        # Agent only — manual toggle removed. The chip above
                        # still surfaces the state. If a section needs
                        # forcing, set it via the DB or the service helper
                        # `set_section_cost_deferred`.
                        # Excluded-from-draft toggle. Use case: Outline
                        # Agent created a wrapper section for a form /
                        # attachment / instructions item that doesn't
                        # need narrative response and slipped past the
                        # auto-filter (e.g., "Attachment C — Description
                        # of Offeror" wrapper).
                        control_state = {
                            "value": s["excluded_from_draft"],
                            "switch": None,
                            "reverting": False,
                        }

                        def _handle_excluded_change(
                            e,
                            *,
                            section=s,
                            chip=excluded_chip,
                            state=control_state,
                        ) -> None:
                            if state["reverting"]:
                                return
                            value = bool(e.value)
                            previous = bool(state["value"])
                            if value == previous:
                                return

                            def update_in_place() -> None:
                                state["value"] = value
                                section["excluded_from_draft"] = value
                                chip.set_visibility(value)
                                outline_stats_label.set_text(
                                    _outline_stats_text()
                                )
                                if on_state_change is not None:
                                    on_state_change()

                            if _toggle_excluded_from_draft(
                                section["pk"], value, update_in_place,
                            ):
                                return

                            # Client-owned switches do not wait for loopback,
                            # so explicitly restore the last persisted value
                            # when the write was rejected.
                            state["reverting"] = True
                            try:
                                switch = state["switch"]
                                switch.set_value(previous)
                                switch.update()
                            finally:
                                state["reverting"] = False

                        excluded_switch = _ImmediateSwitch(
                            "Exclude from draft",
                            value=s["excluded_from_draft"],
                            on_change=_handle_excluded_change,
                        )
                        control_state["switch"] = excluded_switch
                        excluded_switch.props("dense").tooltip(
                            "Toggle ON to make the Writer Team skip this "
                            "section completely — no draft will be "
                            "generated. Use for wrapper sections the "
                            "Outline Agent created for forms, "
                            "attachments, or instructions that don't "
                            "need narrative response. Resets if you "
                            "regenerate the outline."
                        )
                # Compliance items addressed
                if s["compliance_items_addressed"]:
                    with ui.row().classes("flex-wrap gap-1 pt-1"):
                        for rid in s["compliance_items_addressed"][:30]:
                            ui.chip(rid).props("dense color=blue-1 text-color=blue-9").classes("text-xs")
                        if len(s["compliance_items_addressed"]) > 30:
                            ui.chip(f"+{len(s['compliance_items_addressed']) - 30} more").props(
                                "dense color=blue-1 text-color=blue-9"
                            ).classes("text-xs")

    render()


def _toggle_cost_deferred(section_pk: int, value: bool, on_change) -> None:
    """Mark a section cost-deferred (or clear). Future Reviewer + Writer
    Team runs skip cost-deferred sections. The auto-loop checks the flag
    each pass, so toggling mid-loop takes effect on the next pass."""
    if not set_section_cost_deferred(section_pk, value):
        ui.notify("Could not update section.", type="negative")
        return
    if value:
        ui.notify(
            "Marked cost-deferred. Reviewer + Writer Team will skip this "
            "section. The Cost Analysis Agent will draft it later.",
            type="positive",
            multi_line=True,
            timeout=5000,
        )
    else:
        ui.notify(
            "Cleared. Reviewer + Writer Team will process this section.",
            type="positive",
        )
    on_change()


def _toggle_excluded_from_draft(section_pk: int, value: bool, on_change) -> bool:
    """Mark a section excluded from drafting (or clear). The Writer Team
    skips excluded sections entirely — no draft is generated. Use case:
    Outline Agent produced a wrapper section for a form / attachment /
    instructions item that slipped past the auto-filter."""
    if not set_section_excluded_from_draft(section_pk, value):
        ui.notify("Could not update section.", type="negative")
        return False
    if value:
        ui.notify(
            "Excluded from draft. Writer Team will skip this section entirely.",
            type="positive",
            multi_line=True,
            timeout=5000,
        )
    else:
        ui.notify(
            "Cleared. Writer Team will draft this section.",
            type="positive",
        )
    on_change()
    return True
