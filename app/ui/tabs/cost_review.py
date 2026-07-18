"""Proposal Review > Cost Review tab.

Branches by service_line:

  - it_services: full findings list with Accept / Reject / per-finding
    actions, plus the Strategy + Strategy Implementer dialogs that read
    accepted findings and turn them into Cost-Writer directives.
  - payment_systems: per-finding triage (accept / reject / edit user_note
    / refine-with-AI) for the payment cost reviewer's output, rendered
    by `_render_payment_cost_review_panel`.

Includes the helpers that drive both branches:
  _render_one_cost_review_finding / _render_cost_review_finding_actions
  _open_strategy_dialog / _open_strategy_implementer_dialog
  _render_payment_finding_card + payment edit / refine dialogs
"""

from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from app.db.session import session_scope
from app.jobs.payment_cost_reviewer import spawn_payment_cost_reviewer
from app.models import Proposal
from app.services.payment_cost_review import (
    bulk_accept_pending_payment_findings,
    get_payment_cost_review_data,
    get_payment_cost_review_findings,
    get_payment_finding,
    update_payment_finding_action,
    update_payment_finding_user_note,
)
from app.services.service_line import (
    SERVICE_LINE_PAYMENT_SYSTEMS,
    get_service_line,
)
from app.ui._shared import _empty_state
from app.ui.tabs.cost import _render_run_cost_reviewer_button

log = logging.getLogger(__name__)


# Helpers that still live in pages.py — resolved lazily on first call
# so we don't hit a circular import at module-load time.
def _pages_helper(name: str):
    from app.ui import pages

    return getattr(pages, name)


def _open_finding_refine_dialog(*args, **kwargs):
    return _pages_helper("_open_finding_refine_dialog")(*args, **kwargs)


def _render_payment_bid_posture_callout(*args, **kwargs):
    return _pages_helper("_render_payment_bid_posture_callout")(*args, **kwargs)


def _payment_proposed_rate_one_liner(*args, **kwargs):
    return _pages_helper("_payment_proposed_rate_one_liner")(*args, **kwargs)


def _payment_market_comparison_one_liner(*args, **kwargs):
    return _pages_helper("_payment_market_comparison_one_liner")(*args, **kwargs)


def _render_payment_competitive_comparison(*args, **kwargs):
    return _pages_helper("_render_payment_competitive_comparison")(*args, **kwargs)


def _render_payment_volume_card(*args, **kwargs):
    return _pages_helper("_render_payment_volume_card")(*args, **kwargs)


def _render_payment_profit_card(*args, **kwargs):
    return _pages_helper("_render_payment_profit_card")(*args, **kwargs)


def _render_payment_awards_table(*args, **kwargs):
    return _pages_helper("_render_payment_awards_table")(*args, **kwargs)


def _render_payment_competitors_table(*args, **kwargs):
    return _pages_helper("_render_payment_competitors_table")(*args, **kwargs)


def _render_payment_rate_chip(*args, **kwargs):
    return _pages_helper("_render_payment_rate_chip")(*args, **kwargs)


def _render_payment_pricing_model_selector(*args, **kwargs):
    return _pages_helper("_render_payment_pricing_model_selector")(*args, **kwargs)


# Severity → display config for cost-review findings. Same palette
# as the existing Reviewer A/B findings tab so the UI is consistent.
_COST_REVIEW_SEVERITY_VISUAL = {
    "CRITICAL": {
        "icon": "error",
        "chip_color": "negative",
        "card_bg": "bg-red-50",
        "border": "border-red-300",
    },
    "MAJOR": {
        "icon": "warning",
        "chip_color": "warning",
        "card_bg": "bg-orange-50",
        "border": "border-orange-300",
    },
    "MINOR": {
        "icon": "info",
        "chip_color": "blue-grey-3",
        "card_bg": "bg-amber-50",
        "border": "border-amber-200",
    },
}

# Display order — CRITICAL first, then MAJOR, then MINOR.
_COST_REVIEW_SEVERITY_ORDER = ("CRITICAL", "MAJOR", "MINOR")


def _group_cost_review_findings(rows: list[dict]) -> list[dict]:
    """The persistence writes one row per (finding × affected
    scenario). Group rows back into logical findings using
    (severity, category, finding_text) as the identity tuple — same
    finding text means same finding regardless of which scenario
    it's attached to. Returns one entry per logical finding with
    'scenarios' populated as the merged list of affected scenarios.

    Also collects the persisted row IDs so Accept/Reject/Edit
    actions can update all rows of the logical finding atomically.
    user_action and user_note come from the first row in the group
    (rows for the same logical finding always share these values)."""
    groups: dict[tuple, dict] = {}
    for row in rows:
        key = (
            row.get("severity") or "MINOR",
            row.get("category") or "",
            row.get("finding_text") or "",
        )
        if key not in groups:
            groups[key] = {
                "severity": row.get("severity") or "MINOR",
                "category": row.get("category") or "",
                "finding_text": row.get("finding_text") or "",
                "recommended_change": row.get("recommended_change") or "",
                "user_action": row.get("user_action") or "pending",
                "user_note": row.get("user_note"),
                "auto_actioned": bool(row.get("auto_actioned")),
                "scenarios": [],
                "row_ids": [],
                "alternative_scenarios": list(row.get("alternative_scenarios") or []),
                "created_at": row.get("created_at"),
            }
        scenario = row.get("scenario")
        if scenario and scenario not in groups[key]["scenarios"]:
            groups[key]["scenarios"].append(scenario)
        if row.get("id") is not None:
            groups[key]["row_ids"].append(int(row["id"]))
    # Sort scenarios within each finding (LOW → MEDIUM → HIGH).
    scenario_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    for f in groups.values():
        f["scenarios"].sort(key=lambda s: scenario_rank.get(s, 99))
    # Sort findings: severity first, then category alphabetic.
    severity_rank = {s: i for i, s in enumerate(_COST_REVIEW_SEVERITY_ORDER)}
    return sorted(
        groups.values(),
        key=lambda f: (
            severity_rank.get(f["severity"], 99),
            f["category"],
        ),
    )


def _split_subject_from_finding_text(text: str) -> tuple[str, str]:
    """Persistence prepends '[subject] ' to the finding_text so the
    schema doesn't need a separate subject column. Parse it back
    out for display. Returns (subject, body) — empty subject when
    the prefix isn't present."""
    import re

    m = re.match(r"^\[([^\]]+)\]\s*(.*)$", text or "", flags=re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", (text or "").strip()


def _render_cost_review_tab(
    proposal_id: int,
    *,
    on_state_change=None,
    switch_tab=None,
) -> None:
    """Cost Review tab — full findings list with Accept / Reject /
    Edit / Refine-with-AI controls per finding.

    Refreshable so action handlers can re-render after mutating
    user_action / user_note state. `on_state_change` is the outer
    page's refresh hook — called after every action so the tab
    badge (red dot showing pending-finding count) stays in sync.
    """
    from app.jobs.strategy_implementer import (
        claim_strategy_apply_notification,
        get_strategy_apply_state,
    )
    from app.services.cost_reviewer import (
        get_cost_review_findings_snapshot,
        get_cost_review_strategy,
    )
    from app.services.pricing import get_pricing_packages_snapshot

    def _after_action() -> None:
        """Combined refresher — refreshes the tab content AND the
        outer chrome (tab badges, next-step banner)."""
        render.refresh()
        if on_state_change is not None:
            on_state_change()

    def _poll_strategy_apply_completion() -> None:
        """Surface a sticky toast when an Apply Strategy run
        finishes. Polled every 3s via the lifetime-scoped ui.timer at
        the bottom of this function. The claim_*_notification helper
        guarantees the toast fires exactly once per completion event
        in the rare case an old + new tab render race the same tick."""
        s = get_strategy_apply_state(proposal_id)
        if not s or s.get("status") != "completed":
            return
        c = s.get("completed_at")
        if c is None:
            return
        if not claim_strategy_apply_notification(proposal_id, c):
            return
        n_done = s.get("n_done", 0)
        n_total = s.get("n_total", 0)
        n_failed = s.get("n_failed", 0)
        if n_failed:
            ui.notify(
                f"Strategy applied: {n_done} of {n_total} section(s) "
                f"regenerated; {n_failed} failed (see Pipeline log). "
                f"Open the Draft tab to review the rest.",
                type="warning",
                multi_line=True,
                timeout=0,
                close_button="Dismiss",
            )
        else:
            ui.notify(
                f"Strategy applied: {n_done} of {n_total} section(s) "
                f"regenerated. Open the Draft tab to review.",
                type="positive",
                multi_line=True,
                timeout=0,
                close_button="Dismiss",
            )
        # Refresh so any stale "running" indicators clear.
        render.refresh()
        if on_state_change is not None:
            on_state_change()

    @ui.refreshable
    def render() -> None:
        # Service-line branch: payment_systems uses a separate
        # adversarial reviewer (Sonnet, single LLM) whose findings
        # live in proposals.payment_cost_review_findings_json instead
        # of the labor-flow cost_review_findings table. The labor-
        # flow strategy synthesizer + accept/reject/edit/refine UI
        # don't apply — payment findings are read-only review notes
        # the user acts on by editing the section directly in the
        # Draft tab.
        if get_service_line(proposal_id) == SERVICE_LINE_PAYMENT_SYSTEMS:
            _render_payment_cost_review_panel(
                proposal_id,
                on_change=_after_action,
            )
            return

        rows = get_cost_review_findings_snapshot(proposal_id)
        packages = get_pricing_packages_snapshot(proposal_id)
        cached_strategy = get_cost_review_strategy(proposal_id)
        has_packages = len(packages) > 0
        has_findings = len(rows) > 0
        has_strategy = cached_strategy is not None

        # Header card with run/re-run + view-or-generate-strategy buttons.
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full flex-wrap gap-3"):
                with ui.column().classes("gap-0 flex-1"):
                    ui.label("Cost Review").classes("text-base font-medium")
                    ui.label(
                        "Adversarial fact-check of the Cost "
                        "Analyst's H/M/L cost build. Two reviewers "
                        "(Gemini Pro + GPT-5.5) run in parallel; "
                        "only findings BOTH agree on persist. "
                        "Accept / Reject / Edit each one, or click "
                        "'Generate Strategy' for an integrated plan "
                        "that addresses them all together."
                    ).classes("text-xs opacity-70")
                with ui.row().classes("items-center gap-2"):
                    # Show the strategy button when there's either
                    # something to generate FROM (active findings) or
                    # something to view (cached strategy from a prior
                    # session). Either path opens the same dialog.
                    if has_findings or has_strategy:
                        if has_strategy:
                            btn_label = "View Strategy"
                            btn_icon = "article"
                            btn_tooltip = (
                                "Re-open the saved cost-review "
                                "strategy. Use Regenerate inside "
                                "the dialog to spend on a fresh "
                                "Sonnet pass when you want to "
                                "refresh it."
                            )
                        else:
                            btn_label = "Generate Strategy"
                            btn_icon = "auto_awesome_motion"
                            btn_tooltip = (
                                "Synthesize all findings into one "
                                "coherent strategic plan that "
                                "accounts for trade-offs (Sonnet "
                                "4.6, ~$0.02-0.08 per call). The "
                                "result is cached so you can "
                                "re-open it without paying again."
                            )
                        ui.button(
                            btn_label,
                            icon=btn_icon,
                            on_click=lambda: _open_strategy_dialog(
                                proposal_id=proposal_id,
                                on_change=render.refresh,
                            ),
                        ).props("color=primary").tooltip(btn_tooltip)
                    # Apply Strategy — translates the cached
                    # strategy into per-section USER DIRECTIVE
                    # strings the Writer Team can apply. Only
                    # surfaces when a strategy has been generated.
                    if has_strategy:
                        ui.button(
                            "Apply Strategy",
                            icon="play_circle",
                            on_click=lambda: _open_strategy_implementer_dialog(
                                proposal_id=proposal_id,
                                on_change=render.refresh,
                                switch_tab=switch_tab,
                            ),
                        ).props("color=primary outline").tooltip(
                            "Translate the saved strategy into "
                            "per-section directives, then "
                            "regenerate each affected section "
                            "with that directive (Writer Team, "
                            "~$0.50/section)."
                        )
                    _render_run_cost_reviewer_button(
                        proposal_id,
                        has_packages,
                        has_findings,
                    )

        if not has_packages:
            _empty_state(
                "Run Cost Analyst first — the reviewer needs the persisted cost build to fact-check.",
                icon="fact_check",
            )
            return

        if not has_findings:
            _empty_state(
                "Cost Reviewer hasn't run yet on the current cost "
                "build. Click 'Run Cost Reviewer' above — the "
                "review takes ~30-90s and surfaces specific "
                "findings about scope coverage, hour realism, "
                "margin pressure, and ceiling violations.",
                icon="fact_check",
            )
            return

        findings = _group_cost_review_findings(rows)

        # Action-state breakdown so user can see at a glance how
        # many findings still need their attention.
        sev_counts: dict[str, int] = {}
        action_counts: dict[str, int] = {}
        n_auto_accepted = 0
        for f in findings:
            sev_counts[f["severity"]] = sev_counts.get(f["severity"], 0) + 1
            ua = f.get("user_action") or "pending"
            action_counts[ua] = action_counts.get(ua, 0) + 1
            if f.get("auto_actioned") and ua == "accepted":
                n_auto_accepted += 1
        sev_summary = " · ".join(
            f"{sev_counts[s]} {s.lower()}" for s in _COST_REVIEW_SEVERITY_ORDER if sev_counts.get(s, 0) > 0
        )
        action_summary = " · ".join(
            f"{action_counts[a]} {a}"
            for a in ("pending", "accepted", "rejected")
            if action_counts.get(a, 0) > 0
        )
        with ui.card().classes("w-full bg-slate-50"):
            with ui.row().classes("items-center gap-3 w-full flex-wrap"):
                ui.label(f"{len(findings)} finding(s)").classes("text-sm font-medium")
                if sev_summary:
                    ui.label(sev_summary).classes("text-xs font-mono opacity-70")
                ui.element("div").classes("flex-1")
                if n_auto_accepted:
                    ui.chip(
                        f"{n_auto_accepted} AUTO-ACCEPTED · audit before drafting",
                        icon="bolt",
                    ).props("dense color=amber-7 text-color=white").tooltip(
                        "CRITICAL/MAJOR consensus findings the system "
                        "auto-accepted. Open each card to confirm — "
                        "or click Reject / Edit to override. The "
                        "AUTO tag clears once you've reviewed."
                    )
                if action_summary:
                    ui.label(action_summary).classes("text-xs font-mono opacity-70")

        for f in findings:
            _render_one_cost_review_finding(
                f,
                proposal_id=proposal_id,
                on_change=_after_action,
            )

    render()

    # Lifetime-scoped polling timer for Apply Strategy completion
    # toasts. Lives OUTSIDE the @ui.refreshable so each render.refresh()
    # doesn't accumulate a new timer alongside the old (NiceGUI's
    # ui.timer() registers, it doesn't replace). One timer per tab
    # mount, fires every 3s, callback short-circuits cheaply when no
    # apply is in progress. Mirrors the pattern in final_polish.py
    # and findings.py.
    ui.timer(3.0, _poll_strategy_apply_completion)


def _render_one_cost_review_finding(
    f: dict,
    *,
    proposal_id: int | None = None,
    on_change=None,
) -> None:
    """One finding card. Severity-colored border + chips for
    severity / category / scenarios affected. Subject as the row
    header, finding body as readable prose, recommended_change as
    a separate emphasized block, then Accept / Reject / Edit
    action buttons (skipped when on_change is None — read-only mode).

    Already-accepted findings render with an emerald confirmation
    chip; rejected with a slate chip. Both states still let the
    user change their mind via the action buttons."""
    severity = f.get("severity") or "MINOR"
    visual = _COST_REVIEW_SEVERITY_VISUAL.get(
        severity,
        _COST_REVIEW_SEVERITY_VISUAL["MINOR"],
    )
    subject, body = _split_subject_from_finding_text(f.get("finding_text") or "")
    user_action = f.get("user_action") or "pending"
    user_note = (f.get("user_note") or "").strip()
    recommended_change = (f.get("recommended_change") or "").strip()

    with ui.card().classes(f"w-full {visual['card_bg']} border {visual['border']}"):
        with ui.row().classes("items-center gap-2 w-full flex-wrap"):
            ui.chip(
                severity,
                icon=visual["icon"],
            ).props(f"dense color={visual['chip_color']} text-color=white")
            ui.chip(
                f.get("category") or "—",
                icon="label",
            ).props("dense color=blue-grey-3 text-color=black")
            scenarios = f.get("scenarios") or []
            scenarios_str = ", ".join(scenarios) if scenarios else "(no scenarios)"
            ui.chip(
                f"affects {scenarios_str}",
                icon="layers",
            ).props("dense color=blue-grey-2 text-color=black")
            # User-action chip — emerald for accepted, slate for
            # rejected, hidden for pending (the absence of a chip is
            # itself the "needs review" indicator). Auto-accepted
            # rows get an additional amber "AUTO" chip so the user
            # spots what to audit before drafting picks them up.
            if user_action == "accepted":
                ui.chip(
                    "ACCEPTED",
                    icon="check_circle",
                ).props("dense color=positive text-color=white")
                if f.get("auto_actioned"):
                    ui.chip(
                        "AUTO",
                        icon="bolt",
                    ).props("dense color=amber-7 text-color=white").tooltip(
                        "Auto-accepted by the system because this is "
                        "a CRITICAL/MAJOR consensus finding (BOTH "
                        "reviewers raised it). Click Accept / Reject "
                        "/ Edit to confirm or override — the AUTO "
                        "tag clears once you've reviewed."
                    )
            elif user_action == "rejected":
                ui.chip(
                    "REJECTED",
                    icon="block",
                ).props("dense color=blue-grey-7 text-color=white")
            ui.element("div").classes("flex-1")
            if f.get("created_at"):
                ui.label(f"{f['created_at']:%Y-%m-%d %H:%M}").classes("text-xs font-mono opacity-50")

        if subject:
            ui.label(subject).classes("text-sm font-medium pt-1")
        if body:
            ui.label(body).classes("text-sm whitespace-pre-wrap pt-1")

        # Recommended change — separated out and emphasized.
        # User_note (when set on an accepted finding) is the user's
        # edited version of the recommendation; show it in place of
        # the agent's text.
        with ui.card().classes("w-full bg-white border border-slate-300 mt-2"):
            ui.label("Recommended change").classes("text-xs font-medium uppercase opacity-70")
            if user_action == "accepted" and user_note:
                # User edited the recommendation. Show their version
                # prominently; collapse the original below.
                ui.label(user_note).classes("text-sm whitespace-pre-wrap")
                if recommended_change:
                    with ui.expansion(
                        "Original (from agent)",
                        icon="history",
                        value=False,
                    ).classes("w-full pt-1"):
                        ui.label(recommended_change).classes("text-sm opacity-70 whitespace-pre-wrap")
            elif recommended_change:
                ui.label(recommended_change).classes("text-sm whitespace-pre-wrap")
            else:
                ui.label(
                    "(no recommendation provided — re-run the Cost Reviewer with the updated schema)"
                ).classes("text-xs opacity-60 italic")

        # Rejection reason — only visible when user_action=rejected.
        if user_action == "rejected" and user_note:
            with ui.card().classes("w-full bg-slate-50 border border-slate-200 mt-1"):
                ui.label("Rejection reason").classes("text-xs font-medium uppercase opacity-70")
                ui.label(user_note).classes("text-sm whitespace-pre-wrap opacity-80")

        # Alternative scenarios — collapsible when present.
        alts = f.get("alternative_scenarios") or []
        if alts:
            with ui.expansion(
                f"Other alternatives ({len(alts)})",
                icon="alt_route",
                value=False,
            ).classes("w-full mt-1"):
                for alt in alts:
                    label = alt.get("label") or "(no label)"
                    rationale = alt.get("rationale") or ""
                    total_p = alt.get("total_price_usd")
                    margin_d = alt.get("margin_delta_usd")
                    with ui.row().classes("items-center gap-3 pt-1 flex-wrap"):
                        ui.label(label).classes("text-sm font-medium")
                        if total_p is not None:
                            ui.label(f"${float(total_p):,.0f}").classes("text-sm font-mono")
                        if margin_d is not None:
                            sign = "+" if float(margin_d) >= 0 else ""
                            ui.label(f"profit Δ {sign}${float(margin_d):,.0f}").classes(
                                "text-xs font-mono "
                                + ("text-emerald-700" if float(margin_d) >= 0 else "text-red-700")
                            )
                    if rationale:
                        ui.label(rationale).classes("text-xs opacity-80 pl-2")

        # Action buttons — only render when on_change provided
        # (the read-only mode skips them, e.g., if we ever embed
        # the finding card in a non-interactive view).
        if on_change is not None:
            _render_cost_review_finding_actions(
                f,
                proposal_id=proposal_id,
                on_change=on_change,
            )


def _render_cost_review_finding_actions(
    f: dict,
    *,
    proposal_id: int | None = None,
    on_change,
) -> None:
    """Accept / Reject / Edit / Refine-with-AI buttons for one
    finding. Bound to update_cost_review_finding_action against all
    rows belonging to the logical finding. The Refine button needs
    proposal_id for agent_run cost logging."""
    from app.services.cost_reviewer import (
        update_cost_review_finding_action,
    )

    row_ids = list(f.get("row_ids") or [])
    user_action = f.get("user_action") or "pending"

    def _accept() -> None:
        update_cost_review_finding_action(
            finding_ids=row_ids,
            user_action="accepted",
        )
        ui.notify(
            "Accepted. Apply the recommended change to your bid before re-running the analyst.",
            type="positive",
            timeout=4000,
        )
        on_change()

    def _reject() -> None:
        # Reject opens a small dialog so user can optionally
        # provide a reason. Reason persists as user_note.
        with ui.dialog() as dialog, ui.card().classes("w-[480px]"):
            ui.label("Reject finding").classes("text-base font-medium")
            ui.label("Optional reason — helps future-you remember why this finding was dismissed.").classes(
                "text-xs opacity-70 pb-2"
            )
            reason_input = (
                ui.textarea(
                    placeholder="e.g., 'SSP delivered by sub, not in-house'",
                )
                .props("outlined dense")
                .classes("w-full")
            )
            with ui.row().classes("justify-end gap-2 pt-2 w-full"):
                ui.button(
                    "Cancel",
                    on_click=dialog.close,
                ).props("flat")

                def _confirm() -> None:
                    update_cost_review_finding_action(
                        finding_ids=row_ids,
                        user_action="rejected",
                        user_note=str(reason_input.value or "").strip(),
                    )
                    dialog.close()
                    ui.notify(
                        "Rejected.",
                        type="info",
                        timeout=3000,
                    )
                    on_change()

                ui.button(
                    "Reject",
                    on_click=_confirm,
                ).props("color=negative")
        dialog.open()

    def _edit() -> None:
        # Edit opens a dialog with the recommended_change pre-filled
        # for the user to modify. Saving sets user_note to the new
        # text and user_action to accepted (edited == accepted-with-
        # custom-recommendation).
        current_text = (
            f.get("user_note")
            if (f.get("user_action") == "accepted" and f.get("user_note"))
            else f.get("recommended_change") or ""
        )
        with ui.dialog() as dialog, ui.card().classes("w-[640px]"):
            ui.label("Edit recommended change").classes("text-base font-medium")
            ui.label(
                "Modify the agent's recommendation before "
                "accepting. Your edited version replaces the "
                "agent's recommendation in the rendered finding; "
                "the original stays available in an expansion "
                "below."
            ).classes("text-xs opacity-70 pb-2")
            edited_input = (
                ui.textarea(
                    value=current_text,
                    placeholder="Specific actionable fix...",
                )
                .props("outlined dense autogrow")
                .classes("w-full")
            )
            with ui.row().classes("justify-end gap-2 pt-2 w-full"):
                ui.button(
                    "Cancel",
                    on_click=dialog.close,
                ).props("flat")

                def _confirm() -> None:
                    new_text = str(edited_input.value or "").strip()
                    if not new_text:
                        ui.notify(
                            "Edited recommendation cannot be empty.",
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
                        "Saved edited recommendation.",
                        type="positive",
                        timeout=3000,
                    )
                    on_change()

                ui.button(
                    "Save & Accept",
                    on_click=_confirm,
                ).props("color=primary")
        dialog.open()

    with ui.row().classes("items-center gap-2 pt-3 w-full flex-wrap"):
        # When already in an action state, show the action buttons
        # but the redundant one is dimmer (e.g., already-accepted
        # finding shows Accept as muted).
        accept_btn = ui.button(
            "Accept",
            icon="check",
            on_click=_accept,
        )
        accept_btn.props("color=positive" if user_action != "accepted" else "outline color=positive")

        reject_btn = ui.button(
            "Reject",
            icon="block",
            on_click=_reject,
        )
        reject_btn.props("color=negative" if user_action != "rejected" else "outline color=negative")

        ui.button(
            "Edit",
            icon="edit",
            on_click=_edit,
        ).props("outline color=primary")

        # Refine with AI — opens a dialog where the user provides
        # context the original reviewer didn't have, and Sonnet
        # rewrites the recommended_change to incorporate that
        # context. Saved via the same user_note path as Edit, so
        # accepting an AI-refined recommendation behaves identically
        # to accepting an edited one. Disabled when proposal_id
        # isn't available (defensive — should always be set in the
        # tab path, but read-only embeds won't have it).
        if proposal_id is None:
            ui.button(
                "Refine with AI",
                icon="auto_awesome",
            ).props("outline color=primary disable").tooltip(
                "Refine requires proposal context — disabled in read-only views."
            )
        else:
            ui.button(
                "Refine with AI",
                icon="auto_awesome",
                on_click=lambda: _open_finding_refine_dialog(
                    f,
                    proposal_id=proposal_id,
                    on_change=on_change,
                ),
            ).props("outline color=primary").tooltip(
                "Provide context the agent didn't have (e.g., 'SSP "
                "is delivered by a sub') and Sonnet 4.6 rewrites "
                "the recommendation."
            )


def _open_strategy_dialog(*, proposal_id: int, on_change=None) -> None:
    """Generate-Strategy dialog. Renders the cached strategy
    markdown immediately when one exists; otherwise calls Sonnet
    to synthesize one. On every successful synthesis the markdown
    is cached on the Proposal row so the user can re-open without
    paying again.

    `on_change` is an optional callback invoked after a fresh
    synthesis lands so the parent (Cost Review tab) can refresh
    its button label from "Generate Strategy" -> "View Strategy".
    """
    from app.agents.cost_review_strategy import synthesize_strategy
    from app.services.cost_reviewer import (
        get_cost_review_findings_snapshot,
        get_cost_review_strategy,
        save_cost_review_strategy,
    )
    from app.services.market_scan import get_market_scan_snapshot
    from app.services.pricing import get_pricing_packages_snapshot

    rows = get_cost_review_findings_snapshot(proposal_id)
    findings = _group_cost_review_findings(rows)
    # Skip rejected findings — user has dismissed them and they
    # shouldn't drive the strategy.
    active_findings = [f for f in findings if f.get("user_action") != "rejected"]

    cached = get_cost_review_strategy(proposal_id)
    if not active_findings and not cached:
        ui.notify(
            "No active findings to synthesize. Run the Cost Reviewer first, or un-reject some findings.",
            type="warning",
            timeout=4000,
        )
        return

    findings_block = _format_findings_for_strategy(active_findings)
    packages = get_pricing_packages_snapshot(proposal_id)
    cost_build_summary = _format_cost_build_summary_for_strategy(
        packages,
    )
    market_scan = get_market_scan_snapshot(proposal_id)
    market_summary = _format_market_summary_for_strategy(market_scan)

    with ui.dialog() as dialog, ui.card().classes("w-[800px] max-w-[90vw]"):
        ui.label("Cost Review Strategy").classes("text-base font-medium")

        # Initial state — if a cached strategy exists, render it
        # immediately and skip the auto-LLM call. Regenerate is
        # always available below.
        if cached:
            state = {
                "loading": False,
                "markdown": cached["markdown"],
                "error": "",
                "from_cache": True,
                "generated_at": cached.get("generated_at"),
                "findings_at_gen": cached.get("findings_count_at_gen"),
            }
        else:
            state = {
                "loading": True,
                "markdown": "",
                "error": "",
                "from_cache": False,
                "generated_at": None,
                "findings_at_gen": None,
            }

        # Sub-header shows freshness + finding count.
        @ui.refreshable
        def render_subheader() -> None:
            if state["loading"]:
                ui.label(
                    f"Synthesizing {len(active_findings)} active "
                    f"finding(s) into one integrated plan. Sonnet "
                    f"4.6, ~10-20s."
                ).classes("text-xs opacity-70 pb-2")
                return
            if state["error"]:
                return
            n_now = len(active_findings)
            n_at_gen = state.get("findings_at_gen")
            gen_at = state.get("generated_at")
            gen_str = ""
            if gen_at is not None:
                # gen_at is naive UTC. Render localised-friendly
                # "MMM D, HH:MM" — relative ages get fuzzy past a
                # day so absolute time is more useful.
                try:
                    gen_str = gen_at.strftime("%b %d, %H:%M UTC")
                except Exception:
                    gen_str = str(gen_at)
            bits: list[str] = []
            if gen_str:
                bits.append(f"Generated {gen_str}")
            if n_at_gen is not None:
                if n_at_gen == n_now:
                    bits.append(f"based on {n_at_gen} finding(s)")
                else:
                    bits.append(
                        f"based on {n_at_gen} finding(s) — current count is {n_now} (regenerate to refresh)"
                    )
            if bits:
                ui.label(" · ".join(bits)).classes("text-xs opacity-70 pb-2")

        render_subheader()

        @ui.refreshable
        def render_body() -> None:
            if state["loading"]:
                with ui.row().classes("items-center gap-2 py-6"):
                    ui.spinner(size="md").classes("text-primary")
                    ui.label("Synthesizing strategy…").classes("text-sm opacity-70")
                return
            if state["error"]:
                ui.label(f"Strategy generation failed: {state['error']}").classes("text-sm text-red-600")
                return
            ui.markdown(state["markdown"]).classes("w-full prose prose-sm max-w-none")

        render_body()

        with ui.row().classes("justify-end gap-2 pt-2 w-full"):
            ui.button(
                "Close",
                on_click=dialog.close,
            ).props("flat")

            async def _kick_off() -> None:
                # Sonnet 4.6 takes 10-20s; running it inline blocks
                # the NiceGUI event loop and the websocket heartbeat
                # misses, showing "Connection lost" in the browser.
                # Offload to a thread so the loop stays alive.
                if not active_findings:
                    state["error"] = (
                        "No active findings to synthesize. "
                        "Un-reject some findings or run the Cost "
                        "Reviewer first."
                    )
                    state["loading"] = False
                    regen_btn.set_visibility(True)
                    render_body.refresh()
                    return
                try:
                    md = await asyncio.to_thread(
                        synthesize_strategy,
                        proposal_id=proposal_id,
                        findings_block=findings_block,
                        cost_build_summary=cost_build_summary,
                        market_summary=market_summary,
                        n_findings=len(active_findings),
                    )
                    # Persist the fresh markdown so the user can
                    # re-open without paying for another Sonnet call.
                    try:
                        await asyncio.to_thread(
                            save_cost_review_strategy,
                            proposal_id,
                            md,
                            len(active_findings),
                        )
                    except Exception:
                        log.exception("save_cost_review_strategy failed; continuing with in-memory render")
                    from datetime import datetime as _dt

                    state["markdown"] = md
                    state["loading"] = False
                    state["from_cache"] = False
                    state["generated_at"] = _dt.utcnow()
                    state["findings_at_gen"] = len(active_findings)
                    regen_btn.set_visibility(True)
                    render_subheader.refresh()
                    render_body.refresh()
                    if on_change is not None:
                        try:
                            on_change()
                        except Exception:
                            log.exception("strategy-dialog on_change callback raised; ignoring")
                except Exception as exc:
                    state["error"] = f"{type(exc).__name__}: {exc}"
                    state["loading"] = False
                    regen_btn.set_visibility(True)
                    render_subheader.refresh()
                    render_body.refresh()

            async def _regenerate() -> None:
                state["loading"] = True
                state["error"] = ""
                state["markdown"] = ""
                state["from_cache"] = False
                render_subheader.refresh()
                render_body.refresh()
                await _kick_off()

            regen_btn = ui.button(
                "Regenerate",
                icon="refresh",
                on_click=_regenerate,
            )
            regen_btn.props("outline color=primary")
            # Always visible when we have a cached strategy or after
            # the auto-kickoff completes; hidden during the initial
            # spinner only.
            regen_btn.set_visibility(bool(cached))

        # Auto-trigger the LLM only when there is no cached strategy.
        # When cached, the user explicitly clicks Regenerate to spend.
        if not cached:
            ui.timer(0.1, _kick_off, once=True)

    dialog.open()


def _format_findings_for_strategy(findings: list[dict]) -> str:
    """Compact rendering of findings for the strategist's prompt.
    Includes severity / category / scenarios / subject / body /
    recommended_change so the strategist has everything to reason
    about trade-offs."""
    if not findings:
        return "(no findings)"
    rows: list[str] = []
    for i, f in enumerate(findings, 1):
        subject, body = _split_subject_from_finding_text(f.get("finding_text") or "")
        scenarios = ",".join(f.get("scenarios") or [])
        rows.append(f"  [{i}] {f.get('severity', 'MINOR')} · {f.get('category', '?')} · affects {scenarios}")
        if subject:
            rows.append(f"      subject: {subject}")
        if body:
            body_short = body if len(body) <= 500 else body[:497] + "..."
            rows.append(f"      body: {body_short}")
        rec = (
            f.get("user_note")
            if (f.get("user_action") == "accepted" and f.get("user_note"))
            else f.get("recommended_change") or ""
        )
        if rec:
            rec_short = rec if len(rec) <= 300 else rec[:297] + "..."
            rows.append(f"      recommended: {rec_short}")
        ua = f.get("user_action") or "pending"
        if ua != "pending":
            rows.append(f"      user_action: {ua}")
        rows.append("")
    return "\n".join(rows)


def _format_cost_build_summary_for_strategy(
    packages: list[dict],
) -> str:
    """One-line per scenario for the strategist's prompt."""
    if not packages:
        return "(no cost build persisted)"
    rows: list[str] = []
    for p in sorted(
        packages,
        key=lambda p: {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(
            p.get("scenario", ""),
            99,
        ),
    ):
        indirect = p.get("indirect_costs_json") or {}
        rows.append(
            f"  {p.get('scenario', '?')}: price "
            f"${float(p.get('total_proposed_price') or 0):,.0f} | "
            f"margin {float(indirect.get('profit_pct') or 0):.1%} | "
            f"position {p.get('vs_market_position') or '?'} | "
            f"recommendation {p.get('bid_recommendation') or '?'}"
        )
    return "\n".join(rows)


def _format_market_summary_for_strategy(
    scan: dict | None,
) -> str:
    """Compact band + top competitors."""
    if scan is None:
        return "(no market scan)"
    band_low = scan.get("market_band_low_usd") or 0
    band_mid = scan.get("market_band_mid_usd") or 0
    band_high = scan.get("market_band_high_usd") or 0
    competitors = (scan.get("competitors") or [])[:3]
    parts: list[str] = []
    parts.append(
        f"  band: low ${float(band_low):,.0f} / mid ${float(band_mid):,.0f} / high ${float(band_high):,.0f}"
    )
    for c in competitors:
        rl = c.get("estimated_rate_low_usd")
        rh = c.get("estimated_rate_high_usd")
        rl_s = f"${float(rl):.0f}/hr" if rl is not None else "$?"
        rh_s = f"${float(rh):.0f}/hr" if rh is not None else "$?"
        parts.append(
            f"  competitor: {c.get('competitor_name', '?')} ({c.get('likelihood_to_bid', '?')}) {rl_s}-{rh_s}"
        )
    return "\n".join(parts)


def _open_strategy_implementer_dialog(
    *,
    proposal_id: int,
    on_change=None,
    switch_tab=None,
) -> None:
    """Strategy Implementer dialog — translates the cached cost-
    review strategy into per-section USER DIRECTIVE strings that
    the user can review, edit, and apply. Each applied directive
    spawns a writer-section regenerate with the directive set.

    Flow:
      1. Open dialog with spinner.
      2. asyncio.to_thread → synthesize_strategy_directives.
      3. Render directives (priority + section + rationale +
         editable directive textarea + skip toggle per row).
      4. User clicks Apply N directives. We spawn the writer
         jobs (one per kept directive), close the dialog, surface
         a stage-banner refresh.
    """
    from app.jobs.strategy_implementer import (
        apply_strategy_directives,
        get_strategy_apply_state,
        synthesize_strategy_directives,
    )
    from app.services.cost_reviewer import get_cost_review_strategy

    # Sanity-gate before opening: there must be a cached strategy.
    # Cost Review tab button already guards this, but a direct call
    # could bypass; surface a clean message rather than spending
    # on a Sonnet call that has nothing to translate.
    cached = get_cost_review_strategy(proposal_id)
    if not cached or not cached.get("markdown"):
        ui.notify(
            "Generate a strategy first (the implementer needs something to translate).",
            type="warning",
            timeout=4000,
        )
        return

    # Approximate per-section regenerate cost (Sonnet, ~16K input
    # cached + ~6K output). Used only as a UI estimate.
    APPROX_COST_PER_SECTION_USD = 0.50

    # Dialog mode controls which section of render_body shows:
    #   preview    — directive cards + Apply button (initial view)
    #   applying   — progress bar + per-section status while writers run
    #   completed  — success summary + View Draft button
    # Synthesis errors show inline in preview mode via state["error"].
    state: dict = {
        "mode": "preview",
        "loading": True,
        "directives": [],  # list of mutable dicts (UI bound)
        "error": "",
        "n_eligible_sections": 0,
        "n_active_findings": 0,
        # apply progress (populated when mode transitions to applying)
        "n_total": 0,
        "n_done": 0,
        "n_failed": 0,
        "sections_progress": {},  # section_id -> pending/running/done/failed
        "section_titles": {},  # section_id -> title for display
    }

    with ui.dialog() as dialog, ui.card().classes("w-[860px] max-w-[95vw]"):
        ui.label("Apply Strategy to Document").classes("text-base font-medium")
        ui.label(
            "Translates the cached cost-review strategy into one "
            "directive per affected section. Review, edit, or skip "
            "each one before applying. Each applied directive "
            "regenerates that section with the directive forwarded "
            "to the Writer Team — your already-resolved "
            "[NEEDS_HUMAN] answers carry forward automatically."
        ).classes("text-xs opacity-70 pb-2")

        @ui.refreshable
        def render_body() -> None:
            mode = state.get("mode", "preview")

            # ---- APPLYING mode: progress bar + per-section status
            if mode == "applying" or mode == "completed":
                n_total = max(1, int(state.get("n_total") or 1))
                n_done = int(state.get("n_done") or 0)
                n_failed = int(state.get("n_failed") or 0)
                progress_pct = n_done / n_total

                if mode == "completed":
                    if n_failed:
                        with ui.row().classes("items-center gap-2 pb-2"):
                            ui.icon("warning").classes("text-amber-600 text-2xl")
                            ui.label(
                                f"Strategy applied with issues: "
                                f"{n_done}/{state['n_total']} "
                                f"section(s) regenerated, "
                                f"{n_failed} failed."
                            ).classes("text-sm font-medium")
                    else:
                        with ui.row().classes("items-center gap-2 pb-2"):
                            ui.icon("check_circle").classes("text-green-600 text-2xl")
                            ui.label(
                                f"Strategy applied: {n_done}/{state['n_total']} section(s) regenerated."
                            ).classes("text-sm font-medium")
                else:
                    ui.label(
                        f"Applying {state['n_total']} directive(s)… {n_done}/{state['n_total']} complete."
                    ).classes("text-sm pb-2")

                ui.linear_progress(
                    value=progress_pct,
                    show_value=False,
                ).props(
                    "instant-feedback "
                    + ("color=green" if mode == "completed" and not n_failed else "color=primary")
                ).classes("w-full")

                # Per-section status list with live icons
                section_status_icon = {
                    "pending": ("schedule", "text-slate-400"),
                    "running": ("autorenew", "text-blue-500"),
                    "done": ("check_circle", "text-green-600"),
                    "failed": ("error", "text-red-600"),
                }
                with ui.column().classes("w-full gap-1 pt-3"):
                    for sid, status in (state.get("sections_progress") or {}).items():
                        icon_name, icon_cls = section_status_icon.get(
                            status,
                            ("help", "text-slate-400"),
                        )
                        with ui.row().classes("items-center gap-2 w-full"):
                            icon_el = ui.icon(icon_name).classes(f"{icon_cls} text-lg")
                            if status == "running":
                                # Quasar spin animation utility class.
                                icon_el.classes(add="animate-spin")
                            ui.label(sid).classes(
                                "text-xs font-mono px-1.5 py-0.5 bg-slate-100 rounded border"
                            )
                            title = (state.get("section_titles") or {}).get(sid, "")
                            if title:
                                ui.label(title).classes("text-sm flex-1")
                            ui.label(status).classes("text-xs uppercase opacity-60")
                return

            # ---- PREVIEW mode (default) — synth + directive cards
            if state["loading"]:
                with ui.row().classes("items-center gap-2 py-6"):
                    ui.spinner(size="md").classes("text-primary")
                    ui.label("Synthesizing per-section directives… (Sonnet 4.6, ~10-20s)").classes(
                        "text-sm opacity-70"
                    )
                return
            if state["error"]:
                ui.label(f"Implementer failed: {state['error']}").classes("text-sm text-red-600")
                return
            if not state["directives"]:
                ui.label(
                    "The strategy is purely about cost-build "
                    "mutations (margin, hours, ODCs) with no "
                    "narrative implications — no section "
                    "directives needed. Apply cost changes via the "
                    "Cost tab, then regenerate the cost narrative "
                    "section directly."
                ).classes("text-sm opacity-80")
                return

            # Header summary
            ui.label(
                f"{len(state['directives'])} directive(s) generated "
                f"across {state['n_eligible_sections']} eligible "
                f"section(s); {state['n_active_findings']} active "
                f"finding(s) considered."
            ).classes("text-xs opacity-70 pb-2")

            # Per-directive cards
            priority_color = {
                "high": "border-red-300 bg-red-50",
                "medium": "border-amber-300 bg-amber-50",
                "low": "border-slate-200 bg-slate-50",
            }
            for d in state["directives"]:
                color_cls = priority_color.get(
                    d["priority"],
                    "border-slate-200 bg-slate-50",
                )
                with ui.card().classes(f"w-full border {color_cls} mb-2"):
                    with ui.row().classes("items-center justify-between w-full"):
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            ui.label(d["section_id"]).classes(
                                "text-xs font-mono px-2 py-1 bg-white rounded border"
                            )
                            ui.label(d["section_title"]).classes("text-sm font-medium")
                            ui.label(d["priority"].upper()).classes(
                                "text-xs uppercase font-medium opacity-70"
                            )
                            ui.label(d["estimated_changes"]).classes("text-xs opacity-60")

                        # Skip toggle — bound to the dict so it
                        # mutates in-place. The dict gets read by
                        # the Apply handler.
                        skip_switch = ui.switch(
                            "Apply",
                            value=not d.get("skip", False),
                        ).props("dense")

                        def _on_skip(
                            e,
                            _d=d,
                            _switch=skip_switch,
                        ) -> None:
                            _d["skip"] = not bool(_switch.value)
                            update_apply_button()

                        skip_switch.on_value_change(_on_skip)

                    if d.get("rationale"):
                        ui.label(f"Why: {d['rationale']}").classes("text-xs opacity-70 italic pt-1")

                    ui.label("Directive (editable)").classes("text-xs uppercase opacity-60 pt-2")
                    txt = (
                        ui.textarea(
                            value=d["directive"],
                        )
                        .props("outlined dense autogrow")
                        .classes("w-full")
                    )

                    # Bind textarea changes back to the dict so the
                    # Apply handler reads the user's edits.
                    def _on_dir_change(
                        e,
                        _d=d,
                        _txt=txt,
                    ) -> None:
                        _d["directive"] = _txt.value or ""

                    txt.on_value_change(_on_dir_change)

        render_body()

        @ui.refreshable
        def render_buttons() -> None:
            mode = state.get("mode", "preview")
            # Drop the stale button reference whenever we re-render
            # so update_apply_button() can no-op cleanly outside
            # preview mode.
            apply_btn_holder["btn"] = None

            with ui.row().classes("items-center justify-end gap-2 pt-2 w-full"):
                if mode == "completed":
                    ui.button(
                        "Close",
                        on_click=dialog.close,
                    ).props("flat")

                    def _go_to_draft() -> None:
                        # Prefer the in-page tab switcher (no reload);
                        # fall back to a hard reload if the dialog was
                        # opened without one wired up. Same-path hash
                        # nav is a no-op in NiceGUI 3.x SPA mode, so
                        # a bare ui.navigate.to('.../#draft') would
                        # leave the user stuck on the current tab.
                        dialog.close()
                        if switch_tab is not None:
                            switch_tab("draft")
                        else:
                            ui.navigate.reload()

                    ui.button(
                        "Open Draft tab",
                        icon="article",
                        on_click=_go_to_draft,
                    ).props("color=primary")
                    return

                if mode == "applying":
                    ui.label(
                        "You can close this dialog — the writer "
                        "jobs continue in the background. A toast "
                        "on the Cost Review tab will confirm when "
                        "complete."
                    ).classes("text-xs opacity-70 mr-auto")
                    ui.button(
                        "Close (continues in background)",
                        on_click=dialog.close,
                    ).props("flat color=primary")
                    return

                # mode == "preview"
                ui.button(
                    "Cancel",
                    on_click=dialog.close,
                ).props("flat")

                apply_btn_holder["btn"] = ui.button(
                    "Apply 0 directive(s)",
                ).props("color=positive disable")
                apply_btn_holder["btn"].on("click", _on_apply)
                update_apply_button()

        # apply_btn_holder is shared between render_buttons() and
        # update_apply_button() so the latter can find the live
        # button across rerenders.
        apply_btn_holder: dict = {"btn": None}

        def update_apply_button() -> None:
            btn = apply_btn_holder.get("btn")
            if btn is None:
                return
            if state["loading"] or state["error"]:
                return
            kept = [
                d for d in state["directives"] if not d.get("skip") and (d.get("directive") or "").strip()
            ]
            n = len(kept)
            est = n * APPROX_COST_PER_SECTION_USD
            btn.text = f"Apply {n} directive(s) (~${est:.2f} estimated)"
            if n == 0:
                btn.props("color=positive disable")
            else:
                btn.props(remove="disable")
                btn.props("color=positive")

        def _on_apply() -> None:
            kept = [
                {
                    "section_id": d["section_id"],
                    "section_pk": d["section_pk"],
                    "directive": (d.get("directive") or "").strip(),
                }
                for d in state["directives"]
                if not d.get("skip") and (d.get("directive") or "").strip()
            ]
            if not kept:
                ui.notify(
                    "No directives to apply.",
                    type="warning",
                    timeout=3000,
                )
                return
            try:
                n = apply_strategy_directives(
                    proposal_id=proposal_id,
                    directives=kept,
                )
            except Exception as exc:
                ui.notify(
                    f"Apply failed: {type(exc).__name__}: {exc}",
                    type="negative",
                    timeout=5000,
                )
                return
            # Transition to applying mode and seed local progress
            # state from the kept-directive list. Polling will fill
            # in real-time status.
            state["mode"] = "applying"
            state["n_total"] = n
            state["n_done"] = 0
            state["n_failed"] = 0
            state["sections_progress"] = {k["section_id"]: "pending" for k in kept}
            state["section_titles"] = {
                d["section_id"]: d["section_title"]
                for d in state["directives"]
                if d["section_id"] in {k["section_id"] for k in kept}
            }
            render_body.refresh()
            render_buttons.refresh()
            ui.notify(
                f"Applying {n} directive(s) — watch the progress "
                f"bar above. Sections regenerate in parallel "
                f"(~30-60s each).",
                type="positive",
                multi_line=True,
                timeout=4000,
            )
            if on_change is not None:
                try:
                    on_change()
                except Exception:
                    log.exception("strategy-implementer on_change raised; ignoring")

        # Live progress poll. Fires every 1.5s while the dialog is
        # open. When mode!=applying it's a cheap no-op; we leave it
        # running until dialog close so navigation between modes
        # doesn't lose the timer registration.
        def _poll_apply_progress() -> None:
            if state.get("mode") != "applying":
                return
            s = get_strategy_apply_state(proposal_id)
            if not s:
                return
            sp = s.get("sections_progress") or {}
            if sp:
                state["sections_progress"] = dict(sp)
            state["n_done"] = int(s.get("n_done") or 0)
            state["n_failed"] = int(s.get("n_failed") or 0)
            if s.get("status") == "completed":
                state["mode"] = "completed"
                render_buttons.refresh()
            render_body.refresh()

        ui.timer(1.5, _poll_apply_progress)

        render_buttons()

        async def _kick_off() -> None:
            try:
                result = await asyncio.to_thread(
                    synthesize_strategy_directives,
                    proposal_id,
                )
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
                state["loading"] = False
                render_body.refresh()
                update_apply_button()
                return
            if result is None:
                state["error"] = (
                    "Prerequisites missing — generate a strategy "
                    "and ensure at least one writer-eligible "
                    "section exists."
                )
                state["loading"] = False
                render_body.refresh()
                update_apply_button()
                return
            # Decorate each directive with a mutable 'skip' flag so
            # the textareas/switches can mutate it in place.
            decorated: list[dict] = []
            for d in result["directives"]:
                decorated.append({**d, "skip": False})
            state["directives"] = decorated
            state["n_eligible_sections"] = result["n_eligible_sections"]
            state["n_active_findings"] = result["n_active_findings"]
            state["loading"] = False
            render_body.refresh()
            update_apply_button()

        ui.timer(0.1, _kick_off, once=True)

    dialog.open()


def _render_payment_cost_review_panel(
    proposal_id: int,
    *,
    on_change=None,
) -> None:
    """Cost Review tab body for service_line=payment_systems
    proposals. Reads adversarial findings from proposals.
    payment_cost_review_findings_json and renders a header card
    (run / re-run + overall verdict) plus a per-finding list with
    Accept / Reject / Edit / Refine-with-AI controls. Mirrors the
    labor flow's Cost Review user-action lifecycle — the JSON-blob
    persistence is the only shape difference from cost_review_findings."""

    data = get_payment_cost_review_data(proposal_id)
    findings = get_payment_cost_review_findings(proposal_id)
    overall = (data.get("overall_assessment") or "").strip()
    bid_ready = bool(data.get("bid_ready"))
    has_review = bool(data)

    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        has_drafted_cost_section = bool(
            p
            and any(
                s.requires_cost_analysis and (s.draft_text_markdown or "").strip() for s in (p.sections or [])
            )
        )

    def _kick_off() -> None:
        spawn_payment_cost_reviewer(proposal_id)
        ui.notify(
            "Payment Cost Reviewer started — Sonnet will fact-check "
            "the drafted fee narrative against the market scan + "
            "compliance posture. Findings appear here when complete.",
            type="positive",
            multi_line=True,
            timeout=6000,
        )
        ui.navigate.to(f"/proposals/{proposal_id}/progress")

    # ----- Header card: title + Run/Re-run button -----
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full flex-wrap gap-3"):
            with ui.column().classes("gap-0 flex-1"):
                ui.label("Cost Review (Payment Systems)").classes("text-base font-medium")
                ui.label(
                    "Sonnet adversarial fact-check of the drafted fee "
                    "narrative against the persisted Payment Market "
                    "Scan, the company's PCI / compliance posture, "
                    "the brand framing rules, and the fit-risk "
                    "talking points. Catches rate drift, hallucinated "
                    "competitors, missing disclosures, brand-voice "
                    "drift, unaddressed risks, numeric drift, and "
                    "compliance overclaims before submission."
                ).classes("text-xs opacity-70")
            with ui.row().classes("items-center gap-2"):
                btn = ui.button(
                    "Re-run Cost Reviewer" if has_review else "Run Cost Reviewer",
                    icon="fact_check",
                    on_click=_kick_off,
                )
                if has_review:
                    btn.props("outline color=primary")
                else:
                    btn.props("color=primary" if has_drafted_cost_section else "color=grey-5")
                if not has_drafted_cost_section:
                    btn.props("disable").tooltip(
                        "Run Cost Volume Writer first — the reviewer needs the drafted fee narrative."
                    )

    # ----- Empty state when reviewer hasn't run yet -----
    if not has_review:
        if has_drafted_cost_section:
            _empty_state(
                "Cost narrative drafted. Click 'Run Cost Reviewer' "
                "above to adversarially fact-check it before "
                "submission. ~$0.05-0.10/run.",
                icon="fact_check",
            )
        else:
            _empty_state(
                "Run Cost Volume Writer first — the reviewer needs "
                "the drafted fee narrative to fact-check against the "
                "market scan + compliance posture.",
                icon="fact_check",
            )
        return

    # ----- Overall verdict banner -----
    if bid_ready:
        verdict_color = "emerald"
        verdict_icon = "check_circle"
        verdict_label = "Reviewer verdict: BID-READY"
    else:
        verdict_color = "amber"
        verdict_icon = "info"
        verdict_label = "Reviewer verdict: review before submission"
    with ui.row().classes(
        f"items-start gap-2 mt-3 px-3 py-2 rounded "
        f"bg-{verdict_color}-50 border-l-4 border-{verdict_color}-500 w-full"
    ):
        ui.icon(verdict_icon).classes(f"text-{verdict_color}-700")
        with ui.column().classes("gap-0 flex-1"):
            ui.label(verdict_label).classes(f"text-sm font-medium text-{verdict_color}-900")
            if overall:
                ui.label(overall).classes(f"text-sm text-{verdict_color}-800")

    # ----- Severity + triage summary chips + Accept-all button -----
    n_critical = sum(1 for f in findings if f.get("severity") == "CRITICAL")
    n_major = sum(1 for f in findings if f.get("severity") == "MAJOR")
    n_minor = sum(1 for f in findings if f.get("severity") == "MINOR")
    n_pending = sum(1 for f in findings if (f.get("user_action") or "pending") == "pending")
    n_accepted = sum(1 for f in findings if f.get("user_action") == "accepted")
    n_rejected = sum(1 for f in findings if f.get("user_action") == "rejected")

    def _accept_all_pending() -> None:
        try:
            counts = bulk_accept_pending_payment_findings(proposal_id)
        except Exception as exc:
            log.exception("bulk accept failed")
            ui.notify(f"Bulk accept failed: {exc}", type="negative")
            return
        n_acc = counts.get("accepted", 0)
        if n_acc:
            ui.notify(
                f"Accepted {n_acc} pending finding(s).",
                type="positive",
            )
        else:
            ui.notify(
                "No pending findings to accept.",
                type="info",
            )
        if on_change is not None:
            on_change()

    with ui.row().classes("items-center gap-2 pt-4 pb-1 flex-wrap w-full"):
        ui.label(f"{len(findings)} finding(s)").classes("text-sm font-medium")
        if n_critical:
            ui.chip(
                f"{n_critical} CRITICAL",
                icon="error",
            ).props("color=red-7 text-color=white size=sm")
        if n_major:
            ui.chip(
                f"{n_major} MAJOR",
                icon="warning",
            ).props("color=amber-7 text-color=white size=sm")
        if n_minor:
            ui.chip(
                f"{n_minor} MINOR",
                icon="info_outline",
            ).props("color=blue-grey-6 text-color=white size=sm")
        if not findings:
            ui.chip(
                "no drift found",
                icon="verified",
            ).props("color=positive text-color=white size=sm")
        ui.element("div").classes("flex-1")
        # Triage state chips (right-aligned)
        if n_pending:
            ui.chip(
                f"{n_pending} pending",
                icon="schedule",
            ).props("color=blue-grey-6 text-color=white outline size=sm")
        if n_accepted:
            ui.chip(
                f"{n_accepted} accepted",
                icon="check_circle",
            ).props("color=positive text-color=white outline size=sm")
        if n_rejected:
            ui.chip(
                f"{n_rejected} rejected",
                icon="cancel",
            ).props("color=blue-grey-5 text-color=white outline size=sm")
        # Accept-all button — only shown when there's something to act on
        if n_pending:
            ui.button(
                f"Accept all {n_pending} pending",
                icon="done_all",
                on_click=_accept_all_pending,
            ).props("color=positive unelevated size=sm").tooltip(
                "Mark every pending finding as accepted in one click. "
                "You can still re-open the Edit / Refine dialogs to "
                "tune the suggested fix per finding."
            )

    # ----- Per-finding cards (with Accept / Reject / Edit / Refine) -----
    # Wrap each card render in a try/except so a single malformed
    # finding can't kill the whole list. If one card explodes the
    # exception text shows in its own card body — easier to triage
    # than a silent blank page.
    for f in findings:
        try:
            _render_payment_finding_card(
                proposal_id,
                f,
                on_change=on_change,
            )
        except Exception as exc:
            log.exception(
                "payment_cost_review_panel: failed to render finding %s",
                f.get("finding_id"),
            )
            with ui.card().classes("w-full mt-2 bg-red-50 border-l-4 border-red-500"):
                ui.label(f"⚠ Failed to render finding {f.get('finding_id', '?')}: {exc}").classes(
                    "text-sm text-red-900"
                )


def _render_payment_finding_card(
    proposal_id: int,
    f: dict,
    *,
    on_change=None,
) -> None:
    """One finding card with severity styling, citation, suggested fix
    (or user-edited fix), and action buttons mirroring the labor flow's
    cost-review row controls. Visual state branches by user_action:
      - pending   — full severity color, all 4 action buttons enabled
      - accepted  — emerald border, ACCEPTED chip, user_note (if set)
                    overrides the agent's suggested_fix as the
                    canonical recommendation; Reject + Edit + Refine
                    still available
      - rejected  — greyed out, REJECTED chip, user_note (if set)
                    shown as rejection reason; Accept + Edit + Refine
                    still available
    """

    sev = (f.get("severity") or "MINOR").upper()
    action = (f.get("user_action") or "pending").lower()
    finding_id = f.get("finding_id", "?")

    # Severity → (background tint, left border, severity chip color)
    # Tints stay light to keep prose legible. The chip color carries
    # the visual weight of the severity, not the card background.
    severity_styles = {
        "CRITICAL": ("red-50", "red-500", "red-7"),
        "MAJOR": ("amber-50", "amber-500", "amber-7"),
        "MINOR": ("blue-50", "blue-500", "blue-grey-6"),
    }
    bg, border, sev_color = severity_styles.get(
        sev,
        ("slate-50", "slate-400", "blue-grey-6"),
    )

    # User-action visual override — wins over severity styling so the
    # triage state is immediately visible as you scroll the list.
    rejected = action == "rejected"
    accepted = action == "accepted"
    if accepted:
        bg, border = "emerald-50", "emerald-500"
    elif rejected:
        bg, border = "slate-100", "slate-400"

    # Section ID + title — render as a clean "SEC-005 · <title>" line,
    # falling back to whichever side has data. Some agent outputs
    # produce compound IDs like "SEC-005-REQ-036" and titles like
    # "REQ-036 — Pricing Structure" (redundant with the ID); strip
    # the redundancy for display so the header reads cleanly.
    section_id_raw = (f.get("section_id") or "").strip()
    section_title_raw = (f.get("section_title") or "").strip()
    section_label = _payment_finding_section_label(
        section_id_raw,
        section_title_raw,
    )

    def _do_accept() -> None:
        try:
            update_payment_finding_action(
                proposal_id,
                finding_id,
                action="accepted",
            )
            if on_change is not None:
                on_change()
        except Exception as exc:
            log.exception("accept finding failed")
            ui.notify(f"Failed: {exc}", type="negative")

    def _do_reject() -> None:
        try:
            update_payment_finding_action(
                proposal_id,
                finding_id,
                action="rejected",
            )
            if on_change is not None:
                on_change()
        except Exception as exc:
            log.exception("reject finding failed")
            ui.notify(f"Failed: {exc}", type="negative")

    def _do_unmark() -> None:
        try:
            update_payment_finding_action(
                proposal_id,
                finding_id,
                action="pending",
            )
            if on_change is not None:
                on_change()
        except Exception as exc:
            log.exception("unmark finding failed")
            ui.notify(f"Failed: {exc}", type="negative")

    card_classes = (
        f"w-full mt-2 bg-{bg} border-l-4 border-{border} {'opacity-70' if rejected else ''}"
    ).strip()
    with ui.card().classes(card_classes):
        # ----- Header row: ID · severity · category · section · state chip -----
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label(finding_id).classes("text-xs font-mono text-slate-500")
            ui.chip(sev).props(f"color={sev_color} text-color=white size=sm dense")
            ui.chip(f.get("category") or "OTHER").props(
                "color=blue-grey-7 text-color=white outline size=sm dense"
            )
            with ui.row().classes("items-center gap-1 flex-1 min-w-0"):
                if section_label["id"]:
                    ui.label(section_label["id"]).classes("text-xs font-mono text-slate-500")
                if section_label["title"]:
                    ui.label("·").classes("text-xs text-slate-400")
                    ui.label(section_label["title"]).classes("text-sm font-medium text-slate-800 truncate")
            if accepted:
                ui.chip("ACCEPTED", icon="check_circle").props(
                    "color=positive text-color=white size=sm dense"
                )
            elif rejected:
                ui.chip("REJECTED", icon="cancel").props("color=blue-grey-5 text-color=white size=sm dense")

        # ----- Finding text (the problem) -----
        ui.label(f.get("finding_text") or "").classes("text-sm text-slate-800 pt-2 leading-relaxed")

        # ----- Cited verbatim quote -----
        if f.get("cited_quote"):
            with ui.row().classes(
                "items-start gap-2 mt-2 px-3 py-2 rounded bg-white border-l-2 border-slate-300 w-full"
            ):
                ui.icon("format_quote").classes("text-slate-400 text-base")
                ui.label(f"“{f['cited_quote']}”").classes(
                    "text-xs italic text-slate-600 flex-1 leading-relaxed"
                )

        # ----- Suggested fix (or user-edited fix when accepted) -----
        edited = accepted and bool((f.get("user_note") or "").strip())
        canonical_fix = (
            (f.get("user_note") or "").strip() if edited else (f.get("suggested_fix") or "").strip()
        )
        if canonical_fix:
            with ui.row().classes("items-start gap-2 mt-2 w-full"):
                ui.icon("build").classes("text-slate-500 mt-px")
                with ui.column().classes("gap-0 flex-1"):
                    ui.label("Suggested fix (edited)" if edited else "Suggested fix").classes(
                        "text-xs font-semibold uppercase tracking-wide text-slate-600"
                    )
                    ui.label(canonical_fix).classes(
                        "text-sm text-slate-800 leading-relaxed whitespace-pre-wrap"
                    )
            if edited and (f.get("suggested_fix") or "").strip():
                with ui.expansion(
                    "View agent's original suggested fix",
                    icon="history",
                    value=False,
                ).classes("w-full"):
                    ui.label(f["suggested_fix"]).classes("text-xs whitespace-pre-wrap text-slate-600")

        # ----- Rejection reason -----
        if rejected and (f.get("user_note") or "").strip():
            with ui.row().classes("items-start gap-2 mt-2 w-full"):
                ui.icon("comment").classes("text-slate-500 mt-px")
                with ui.column().classes("gap-0 flex-1"):
                    ui.label("Rejection reason").classes(
                        "text-xs font-semibold uppercase tracking-wide text-slate-600"
                    )
                    ui.label(f["user_note"]).classes(
                        "text-sm text-slate-700 leading-relaxed whitespace-pre-wrap"
                    )

        # ----- Action buttons row -----
        with ui.row().classes("items-center gap-2 pt-3 mt-3 w-full flex-wrap border-t border-slate-200"):
            if action == "pending":
                ui.button(
                    "Accept",
                    icon="check",
                    on_click=_do_accept,
                ).props("color=positive unelevated size=sm dense")
                ui.button(
                    "Reject",
                    icon="close",
                    on_click=_do_reject,
                ).props("outline color=red-7 size=sm dense")
            else:
                ui.button(
                    "Reset to pending",
                    icon="undo",
                    on_click=_do_unmark,
                ).props("flat color=blue-grey-6 size=sm dense")
            ui.element("div").classes("flex-1")
            ui.button(
                "Edit",
                icon="edit",
                on_click=lambda fid=finding_id: _open_payment_finding_edit_dialog(
                    proposal_id,
                    fid,
                    on_change=on_change,
                ),
            ).props("flat color=primary size=sm dense")
            ui.button(
                "Refine with AI",
                icon="auto_awesome",
                on_click=lambda fid=finding_id: _open_payment_finding_refine_dialog(
                    proposal_id,
                    fid,
                    on_change=on_change,
                ),
            ).props("flat color=primary size=sm dense")


def _payment_finding_section_label(
    section_id: str,
    section_title: str,
) -> dict[str, str]:
    """Render a clean section reference for the per-finding header.
    Some agent outputs produce compound IDs like 'SEC-005-REQ-036' and
    titles like 'REQ-036 — Pricing Structure' (the REQ-### appears in
    both, redundantly). This helper trims the section_id back to its
    SEC-### root and strips a leading 'REQ-### —' from the title when
    it duplicates the ID's compliance-item portion. Returns
    {id, title} for the renderer to display side-by-side."""
    import re

    if not section_id and not section_title:
        return {"id": "", "title": ""}
    # Trim to the SEC-### root if the agent emitted a compound id.
    sec_match = re.match(r"^(SEC-\d+)", section_id)
    clean_id = sec_match.group(1) if sec_match else section_id
    # Strip a leading "REQ-### —" / "REQ-### -" from the title when
    # the original section_id already encoded that compliance item.
    clean_title = section_title
    if section_id != clean_id:
        clean_title = re.sub(
            r"^\s*REQ-\d+\s*[—\-:]\s*",
            "",
            clean_title,
        ).strip()
    return {"id": clean_id, "title": clean_title}


def _open_payment_finding_edit_dialog(
    proposal_id: int,
    finding_id: str,
    *,
    on_change=None,
) -> None:
    """Edit dialog for a payment Cost Review finding's suggested_fix.
    Mirrors the labor flow's per-row edit pattern. Save options:
      - Save & Accept: persist edited text to user_note + flip
        user_action=accepted in one shot.
      - Save without action: persist user_note only; user_action
        stays where it was (lets the user iterate before deciding).
      - Cancel: discard edits."""

    f = get_payment_finding(proposal_id, finding_id)
    if f is None:
        ui.notify(f"Finding {finding_id} not found.", type="negative")
        return

    current_fix = (f.get("user_note") or "").strip() or (f.get("suggested_fix") or "").strip()

    with ui.dialog() as dialog, ui.card().classes("w-[700px] max-w-full"):
        ui.label(f"Edit Finding {finding_id}").classes("text-lg font-semibold")
        ui.label(
            f"{f.get('severity', '')} · {f.get('category', '')} · "
            f"{f.get('section_id', '')} {f.get('section_title', '')}"
        ).classes("text-xs opacity-70")

        with ui.column().classes("gap-3 w-full pt-2"):
            ui.label("Finding").classes("text-xs font-medium opacity-70")
            ui.label(f.get("finding_text") or "").classes("text-sm whitespace-pre-wrap opacity-80")

            ui.label("Suggested fix (editable)").classes("text-xs font-medium opacity-70 pt-2")
            fix_input = (
                ui.textarea(
                    value=current_fix,
                )
                .props("outlined autogrow")
                .classes("w-full")
            )

        status = ui.label("").classes("text-sm italic")

        def _save(*, mark_accepted: bool) -> None:
            new_text = (fix_input.value or "").strip()
            try:
                if mark_accepted:
                    update_payment_finding_action(
                        proposal_id,
                        finding_id,
                        action="accepted",
                        user_note=new_text or None,
                    )
                    ui.notify(
                        f"Finding {finding_id} accepted with edited fix.",
                        type="positive",
                    )
                else:
                    update_payment_finding_user_note(
                        proposal_id,
                        finding_id,
                        user_note=new_text,
                    )
                    ui.notify(
                        f"Finding {finding_id} edits saved.",
                        type="positive",
                    )
                if on_change is not None:
                    on_change()
                dialog.close()
            except Exception as exc:
                log.exception("save edit failed")
                status.set_text(f"⚠ Save failed: {exc}")
                status.classes(replace="text-sm italic text-red-700")

        with ui.row().classes("items-center justify-end gap-2 w-full pt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button(
                "Save (don't change action)",
                icon="save",
                on_click=lambda: _save(mark_accepted=False),
            ).props("flat color=primary")
            ui.button(
                "Save & Accept",
                icon="check_circle",
                on_click=lambda: _save(mark_accepted=True),
            ).props("color=emerald-7 unelevated")

    dialog.open()


def _open_payment_finding_refine_dialog(
    proposal_id: int,
    finding_id: str,
    *,
    on_change=None,
) -> None:
    """Refine-with-AI dialog. Lets the user iteratively refine the
    suggested_fix with natural-language guidance — reuses the existing
    cost_review_refiner agent (service-line-agnostic). Mirrors the
    labor flow's _open_refine_dialog pattern."""
    from app.agents.cost_review_refiner import refine_recommendation

    f = get_payment_finding(proposal_id, finding_id)
    if f is None:
        ui.notify(f"Finding {finding_id} not found.", type="negative")
        return

    initial_fix = (f.get("user_note") or "").strip() or (f.get("suggested_fix") or "").strip()

    state = {"current_recommendation": initial_fix, "loading": False}

    with ui.dialog() as dialog, ui.card().classes("w-[750px] max-w-full"):
        ui.label(f"Refine Finding {finding_id} with AI").classes("text-lg font-semibold")
        ui.label(
            f"{f.get('severity', '')} · {f.get('category', '')} · "
            f"{f.get('section_id', '')} {f.get('section_title', '')}"
        ).classes("text-xs opacity-70")

        with ui.column().classes("gap-2 w-full pt-2"):
            ui.label("Finding").classes("text-xs font-medium opacity-70")
            ui.label(f.get("finding_text") or "").classes("text-sm whitespace-pre-wrap opacity-80")

        @ui.refreshable
        def render_current() -> None:
            ui.label("Current suggested fix").classes("text-xs font-medium opacity-70 pt-2")
            with ui.card().classes("w-full bg-slate-50"):
                ui.label(state["current_recommendation"] or "(empty)").classes("text-sm whitespace-pre-wrap")

        render_current()

        ui.label(
            "Your guidance — context the original reviewer didn't have, "
            "or the direction you want the recommendation to take "
            "(e.g., 'we already have a partner for hardware, so the fix "
            "should reference the partner not in-house equipment'):"
        ).classes("text-xs opacity-70 pt-3")
        guidance_input = (
            ui.textarea()
            .props(
                "outlined autogrow",
            )
            .classes("w-full")
        )

        status = ui.label("").classes("text-sm italic")

        async def _refine() -> None:
            guidance = (guidance_input.value or "").strip()
            if not guidance:
                status.set_text("⚠ Add some guidance before refining.")
                status.classes(replace="text-sm italic text-amber-700")
                return
            state["loading"] = True
            status.set_text("Refining recommendation (Sonnet)…")
            status.classes(replace="text-sm italic text-blue-700")
            try:
                refined = await asyncio.to_thread(
                    refine_recommendation,
                    proposal_id=proposal_id,
                    severity=f.get("severity") or "MINOR",
                    category=f.get("category") or "OTHER",
                    subject=(f"{f.get('section_id', '')} {f.get('section_title', '')}").strip(),
                    finding_text=f.get("finding_text") or "",
                    current_recommendation=state["current_recommendation"],
                    user_guidance=guidance,
                )
                state["current_recommendation"] = (refined or "").strip()
                state["loading"] = False
                guidance_input.value = ""
                render_current.refresh()
                status.set_text("✓ Refined. Iterate again or save below.")
                status.classes(replace="text-sm italic text-emerald-700")
            except Exception as exc:
                log.exception("refine recommendation failed")
                state["loading"] = False
                status.set_text(f"⚠ Refine failed: {exc}")
                status.classes(replace="text-sm italic text-red-700")

        with ui.row().classes("items-center gap-2 w-full pt-2"):
            ui.button(
                "Refine",
                icon="auto_awesome",
                on_click=_refine,
            ).props("color=primary outline")

        def _save(*, mark_accepted: bool) -> None:
            new_text = state["current_recommendation"]
            try:
                if mark_accepted:
                    update_payment_finding_action(
                        proposal_id,
                        finding_id,
                        action="accepted",
                        user_note=new_text or None,
                    )
                    ui.notify(
                        f"Finding {finding_id} accepted with refined fix.",
                        type="positive",
                    )
                else:
                    update_payment_finding_user_note(
                        proposal_id,
                        finding_id,
                        user_note=new_text,
                    )
                    ui.notify(
                        f"Finding {finding_id} refined fix saved.",
                        type="positive",
                    )
                if on_change is not None:
                    on_change()
                dialog.close()
            except Exception as exc:
                log.exception("save refined finding failed")
                status.set_text(f"⚠ Save failed: {exc}")
                status.classes(replace="text-sm italic text-red-700")

        with ui.row().classes("items-center justify-end gap-2 w-full pt-3"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button(
                "Save (don't change action)",
                icon="save",
                on_click=lambda: _save(mark_accepted=False),
            ).props("flat color=primary")
            ui.button(
                "Save & Accept",
                icon="check_circle",
                on_click=lambda: _save(mark_accepted=True),
            ).props("color=emerald-7 unelevated")

    dialog.open()
