"""Proposal Review > Outcome panel.

Surfaces the current outcome chip + Edit dialog that calls
`upsert_outcome`. Rendered inline on the Proposal Review page (NOT as a
tab in `_PROPOSAL_REVIEW_TABS`) — sits below the header card / banners
when the proposal is in a terminal status (submitted / approved /
archived). The Edit dialog renders a per-factor scoring table inline
when the proposal's `evaluation_criteria_json` has factors so the user
doesn't have to re-type factor IDs and names already pulled from
Section M during intake.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from nicegui import ui
from sqlalchemy import select

from app.core.enums import ProposalOutcomeStatus
from app.db.session import session_scope
from app.models import Proposal, ProposalOutcome
from app.services.proposal_outcomes import upsert_outcome

log = logging.getLogger(__name__)


# Outcome → Quasar palette color for chips. Mirrors the
# `_OUTCOME_CHIP_COLOR` constant on app/ui/pages.py — duplicated here
# so this module is self-contained and doesn't reach back into pages.py.
_OUTCOME_CHIP_COLOR = {
    "won": "green-3",
    "lost": "red-3",
    "no_award": "blue-grey-3",
    "withdrawn": "amber-3",
    "pending": "blue-3",
}


def _render_outcome_panel(
    proposal_id: int,
    *,
    on_state_change: Any = None,
    read_only: bool = False,
) -> None:
    """Render the Outcome panel for a proposal.

    Args:
        proposal_id: the proposal whose outcome is surfaced.
        on_state_change: optional callable to refresh outer chrome (tab
            badges, banners) when the outcome row changes.
        read_only: render the outcome record without an edit action.

    The panel is a single ui.card with:
      - Current outcome chip (color per `_OUTCOME_CHIP_COLOR`).
      - Summary line: awarded firm + prices when known.
      - "Edit outcome" button → opens the upsert dialog.
    """
    # Snapshot everything we need inside one session_scope so the UI
    # builder doesn't touch detached ORM attributes.
    with session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        if proposal is None:
            return
        existing = db.execute(
            select(ProposalOutcome).where(ProposalOutcome.proposal_id == proposal_id)
        ).scalar_one_or_none()

        evaluation_criteria_raw = proposal.evaluation_criteria_json
        snapshot = {
            "outcome": (
                existing.outcome.value
                if existing is not None and hasattr(existing.outcome, "value")
                else (str(existing.outcome) if existing is not None else "pending")
            ),
            "submitted_at": existing.submitted_at if existing else None,
            "decided_at": existing.decided_at if existing else None,
            "our_proposed_price_usd": (
                float(existing.our_proposed_price_usd)
                if existing and existing.our_proposed_price_usd is not None
                else None
            ),
            "awarded_price_usd": (
                float(existing.awarded_price_usd)
                if existing and existing.awarded_price_usd is not None
                else None
            ),
            "awarded_to": existing.awarded_to if existing else None,
            "debrief_received": bool(existing.debrief_received) if existing else False,
            "our_total_score": (
                float(existing.our_total_score) if existing and existing.our_total_score is not None else None
            ),
            "winning_total_score": (
                float(existing.winning_total_score)
                if existing and existing.winning_total_score is not None
                else None
            ),
            "debrief_notes": existing.debrief_notes if existing else None,
            "factor_scores": existing.factor_scores_json if existing else None,
        }

    # Parse evaluation criteria factors if present so the dialog can
    # pre-populate the per-factor scoring table.
    factor_seed: list[dict] = []
    if evaluation_criteria_raw:
        try:
            crit = json.loads(evaluation_criteria_raw)
            for f in crit.get("factors") or []:
                factor_seed.append(
                    {
                        "factor_id": str(f.get("factor_id") or ""),
                        "factor_name": str(f.get("factor_name") or ""),
                    }
                )
        except (json.JSONDecodeError, AttributeError, TypeError):
            factor_seed = []

    # ── Outcome panel card ──────────────────────────────────────────
    outcome_val = snapshot["outcome"]
    chip_color = _OUTCOME_CHIP_COLOR.get(outcome_val, "blue-grey-3")

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center w-full gap-3"):
            ui.label("Outcome").classes("text-base font-semibold")
            ui.chip(outcome_val).props(f"color={chip_color} text-color=black")
            # Summary line — prices / awarded firm when known.
            summary_bits: list[str] = []
            if snapshot["our_proposed_price_usd"] is not None:
                summary_bits.append(f"our proposed: ${snapshot['our_proposed_price_usd']:,.2f}")
            if snapshot["awarded_price_usd"] is not None:
                summary_bits.append(f"awarded: ${snapshot['awarded_price_usd']:,.2f}")
            if snapshot["awarded_to"]:
                summary_bits.append(f"to {snapshot['awarded_to']}")
            if summary_bits:
                ui.label(" · ".join(summary_bits)).classes("text-sm opacity-70")

            edit_button = ui.button(
                "Edit outcome",
                icon="edit",
                on_click=lambda: _open_edit_dialog(
                    proposal_id=proposal_id,
                    snapshot=snapshot,
                    factor_seed=factor_seed,
                    on_state_change=on_state_change,
                ),
            ).props("flat color=primary").classes("ml-auto")
            if read_only:
                edit_button.disable()

        # Per-factor scoring breakdown (read-only summary when set).
        scores = snapshot["factor_scores"] or []
        if scores:
            ui.label("Per-factor scoring").classes("text-sm font-semibold mt-2")
            with ui.row().classes("flex-wrap gap-2"):
                for s in scores:
                    fid = s.get("factor_id") or "?"
                    fname = s.get("factor_name") or ""
                    our = s.get("our_score")
                    winning = s.get("winning_score")
                    maxv = s.get("max_score")
                    bits = []
                    if our is not None:
                        bits.append(f"us: {our}")
                    if winning is not None:
                        bits.append(f"win: {winning}")
                    if maxv is not None:
                        bits.append(f"max: {maxv}")
                    label = f"{fid} {fname} — " + " / ".join(bits) if bits else f"{fid} {fname}"
                    ui.chip(label).props("color=blue-grey-3 text-color=black dense")


def _open_edit_dialog(
    *,
    proposal_id: int,
    snapshot: dict,
    factor_seed: list[dict],
    on_state_change: Any,
) -> None:
    """Open the Edit Outcome dialog and wire its submit handler.

    The dialog mirrors the upsert_outcome kwargs:
      - outcome dropdown (required)
      - submitted_at + decided_at date strings
      - prices + scores as ui.number
      - awarded_to as ui.input
      - debrief_received toggle
      - debrief_notes as ui.textarea
      - factor scores table (rows pre-populated from
        evaluation_criteria_json factors when present)
    """
    # Stage the dialog's mutable state in a dict so the submit handler
    # can read it cleanly.
    state: dict = {
        "outcome": snapshot.get("outcome") or "pending",
        "submitted_at_str": (
            snapshot["submitted_at"].strftime("%Y-%m-%d") if snapshot.get("submitted_at") else ""
        ),
        "decided_at_str": (snapshot["decided_at"].strftime("%Y-%m-%d") if snapshot.get("decided_at") else ""),
        "our_proposed_price_usd": snapshot.get("our_proposed_price_usd"),
        "awarded_price_usd": snapshot.get("awarded_price_usd"),
        "awarded_to": snapshot.get("awarded_to") or "",
        "debrief_received": bool(snapshot.get("debrief_received")),
        "our_total_score": snapshot.get("our_total_score"),
        "winning_total_score": snapshot.get("winning_total_score"),
        "debrief_notes": snapshot.get("debrief_notes") or "",
    }

    # Build the factor-scores table seed: prefer the existing
    # factor_scores_json if set, otherwise fall back to the seed pulled
    # from evaluation_criteria_json (factor_id + factor_name only).
    existing_scores = snapshot.get("factor_scores") or []
    if existing_scores:
        factor_rows = [dict(s) for s in existing_scores]
    else:
        factor_rows = [
            {
                "factor_id": s["factor_id"],
                "factor_name": s["factor_name"],
                "our_score": None,
                "winning_score": None,
                "max_score": None,
                "notes": "",
            }
            for s in factor_seed
        ]

    outcome_options = {v.value: v.value for v in ProposalOutcomeStatus}

    with ui.dialog() as dlg, ui.card().classes("max-w-3xl"):
        ui.label("Edit Outcome").classes("text-lg font-semibold")
        ui.label(
            "Captures what happened after submission. None-blank fields preserve the existing value on save."
        ).classes("text-xs opacity-60")

        with ui.row().classes("w-full gap-3 mt-2"):
            ui.select(
                options=outcome_options,
                value=state["outcome"],
                label="Outcome",
                on_change=lambda e: state.update(outcome=e.value),
            ).classes("w-48")
            ui.input(
                "Submitted (YYYY-MM-DD)",
                value=state["submitted_at_str"],
                on_change=lambda e: state.update(submitted_at_str=e.value or ""),
            ).classes("w-48")
            ui.input(
                "Decided (YYYY-MM-DD)",
                value=state["decided_at_str"],
                on_change=lambda e: state.update(decided_at_str=e.value or ""),
            ).classes("w-48")

        with ui.row().classes("w-full gap-3 mt-2"):
            ui.number(
                "Our proposed price (USD)",
                value=state["our_proposed_price_usd"],
                format="%.2f",
                on_change=lambda e: state.update(our_proposed_price_usd=e.value),
            ).classes("w-56")
            ui.number(
                "Awarded price (USD)",
                value=state["awarded_price_usd"],
                format="%.2f",
                on_change=lambda e: state.update(awarded_price_usd=e.value),
            ).classes("w-56")
            ui.input(
                "Awarded to",
                value=state["awarded_to"],
                on_change=lambda e: state.update(awarded_to=e.value or ""),
            ).classes("flex-1")

        with ui.row().classes("w-full gap-3 mt-2"):
            ui.number(
                "Our total score",
                value=state["our_total_score"],
                format="%.2f",
                on_change=lambda e: state.update(our_total_score=e.value),
            ).classes("w-48")
            ui.number(
                "Winning total score",
                value=state["winning_total_score"],
                format="%.2f",
                on_change=lambda e: state.update(winning_total_score=e.value),
            ).classes("w-48")
            ui.checkbox(
                "Debrief received",
                value=state["debrief_received"],
                on_change=lambda e: state.update(debrief_received=bool(e.value)),
            )

        ui.textarea(
            "Debrief notes",
            value=state["debrief_notes"],
            on_change=lambda e: state.update(debrief_notes=e.value or ""),
        ).classes("w-full mt-2")

        # ── Per-factor scoring table ───────────────────────────────
        if factor_rows:
            ui.label("Per-factor scoring").classes("text-sm font-semibold mt-3")
            ui.label(
                "Pre-populated from this proposal's Section M evaluation criteria when available."
            ).classes("text-xs opacity-60")

            for row in factor_rows:
                with ui.row().classes("w-full gap-2 items-center"):
                    ui.label(f"{row.get('factor_id', '?')} {row.get('factor_name', '')}").classes(
                        "w-56 text-sm"
                    )
                    ui.number(
                        "our",
                        value=row.get("our_score"),
                        on_change=lambda e, r=row: r.__setitem__("our_score", e.value),
                    ).classes("w-24")
                    ui.number(
                        "winning",
                        value=row.get("winning_score"),
                        on_change=lambda e, r=row: r.__setitem__("winning_score", e.value),
                    ).classes("w-24")
                    ui.number(
                        "max",
                        value=row.get("max_score"),
                        on_change=lambda e, r=row: r.__setitem__("max_score", e.value),
                    ).classes("w-24")
                    ui.input(
                        "notes",
                        value=row.get("notes") or "",
                        on_change=lambda e, r=row: r.__setitem__("notes", e.value or ""),
                    ).classes("flex-1")

        def _parse_date(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return None

        def on_save() -> None:
            try:
                kwargs = {
                    "proposal_id": proposal_id,
                    "outcome": ProposalOutcomeStatus(state["outcome"]),
                    "submitted_at": _parse_date(state.get("submitted_at_str")),
                    "decided_at": _parse_date(state.get("decided_at_str")),
                    "our_proposed_price_usd": state.get("our_proposed_price_usd"),
                    "awarded_price_usd": state.get("awarded_price_usd"),
                    "awarded_to": state.get("awarded_to") or None,
                    "debrief_received": state.get("debrief_received"),
                    "our_total_score": state.get("our_total_score"),
                    "winning_total_score": state.get("winning_total_score"),
                    "debrief_notes": state.get("debrief_notes") or None,
                    "factor_scores": factor_rows if factor_rows else None,
                }
                upsert_outcome(**kwargs)
                ui.notify("Outcome saved", type="positive")
                dlg.close()
                if on_state_change is not None:
                    on_state_change()
                ui.timer(0.5, lambda: ui.navigate.reload(), once=True)
            except Exception as exc:
                log.exception(
                    "outcome panel: save failed for proposal=%d",
                    proposal_id,
                )
                ui.notify(f"Save failed: {exc}", type="negative")

        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Cancel", on_click=dlg.close).props("flat")
            ui.button("Save", icon="save", on_click=on_save).props("color=primary")

    dlg.open()
