"""Proposal Review > Evaluation Criteria tab.

Renders the structured Section M evaluation criteria extracted from the
RFP — evaluation method, factor cards (with weights, scoring scales,
subfactors, and compliance-item mapping), verbatim trade-off / LPTA
language, and a collapsible extraction-notes footer.

Read-only display. The "Re-extract evaluation criteria" button fires a
daemon thread (spawn_section_m_only) when on_rerun is provided.
"""

from __future__ import annotations

import json

from nicegui import ui

from app.ui._shared import _empty_state

# Method-badge colors — Quasar palette tokens.
_METHOD_BADGE_COLOR = {
    "best_value": "amber-3",
    "lpta": "purple-3",
    "trade_off": "teal-3",
    "unknown": "blue-grey-3",
}


def _render_evaluation_criteria_tab(
    criteria_json_raw: str | None,
    *,
    on_rerun=None,
) -> None:
    """Render the Evaluation Criteria tab content.

    Args:
        criteria_json_raw: The raw JSON string from proposals.evaluation_criteria_json.
            None / empty → shows empty state.
        on_rerun: Optional callable fired when the user clicks the re-extract button.
    """
    if not criteria_json_raw:
        _empty_state(
            "Evaluation criteria not yet extracted. Re-run intake to pull Section M.",
            icon="rule",
        )
        if on_rerun is not None:
            ui.button(
                "Re-extract evaluation criteria",
                icon="refresh",
                on_click=on_rerun,
            ).classes("mt-4")
        return

    try:
        criteria = json.loads(criteria_json_raw)
    except json.JSONDecodeError as exc:
        _empty_state(
            f"Evaluation criteria JSON is malformed: {exc}",
            icon="error",
        )
        return

    if not isinstance(criteria, dict):
        _empty_state("Evaluation criteria data is invalid.", icon="error")
        return

    method = criteria.get("evaluation_method", "unknown")
    factors = criteria.get("factors") or []
    sl_map = criteria.get("section_l_to_m_map") or {}
    trade_off = criteria.get("trade_off_language")
    lpta = criteria.get("lowest_price_clause")
    notes = criteria.get("extraction_notes")

    # ── Header row ───────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2 pt-2"):
        badge_color = _METHOD_BADGE_COLOR.get(method, "blue-grey-3")
        ui.chip(
            f"Method: {method.replace('_', ' ').title()}",
            icon="scoreboard",
        ).props(f"color={badge_color} text-color=black")
        if on_rerun is not None:
            ui.button(
                "Re-extract",
                icon="refresh",
                on_click=on_rerun,
            ).props("flat dense")

    # ── Factor cards ─────────────────────────────────────────────────────
    if not factors:
        ui.label("No evaluation factors enumerated by the RFP.").classes("text-sm opacity-60 mt-4")
    else:
        # Sort: known weight desc, null-weight factors in original order at end
        def _sort_key(f: dict):
            w = f.get("weight_pct")
            return (1 if w is None else 0, -(w or 0))

        sorted_factors = sorted(factors, key=_sort_key)

        # Build inverse map: factor_id → list of REQ-IDs that target it
        factor_to_reqs: dict[str, list[str]] = {}
        for req_id, fids in sl_map.items():
            for fid in fids:
                factor_to_reqs.setdefault(fid, []).append(req_id)

        ui.label("Evaluation Factors").classes("text-base font-semibold mt-4")
        for f in sorted_factors:
            fid = f.get("factor_id", "?")
            fname = f.get("factor_name", "?")
            wpct = f.get("weight_pct")
            wdesc = f.get("weight_descriptive")
            scale = f.get("scoring_scale")
            evidence = f.get("evidence_required")
            subfactors = f.get("subfactors") or []

            if wpct is not None:
                weight_label = f"{wpct}%"
            elif wdesc:
                weight_label = wdesc
            else:
                weight_label = "(undisclosed)"

            with ui.card().classes("w-full mb-2"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"{fid}  {fname}").classes("text-lg font-semibold")
                    ui.chip(weight_label).props("color=grey-3 text-color=black dense")

                if scale:
                    ui.label(f"Scoring scale: {scale}").classes("text-sm")
                if evidence:
                    ui.label(f"Evidence required: {evidence}").classes("text-sm")

                # Subfactors table
                if subfactors:
                    cols = [
                        {"name": "name", "label": "Sub-factor", "field": "name", "align": "left"},
                        {"name": "weight", "label": "Weight", "field": "weight", "align": "left"},
                        {"name": "notes", "label": "Notes", "field": "notes", "align": "left"},
                    ]
                    rows = []
                    for sf in subfactors:
                        sf_wpct = sf.get("weight_pct")
                        rows.append(
                            {
                                "name": sf.get("name", ""),
                                "weight": f"{sf_wpct}%" if sf_wpct is not None else "—",
                                "notes": sf.get("notes") or "",
                            }
                        )
                    ui.table(columns=cols, rows=rows, row_key="name").classes("w-full text-sm").props(
                        "dense flat"
                    )

                # Compliance items targeting this factor
                targeting_reqs = factor_to_reqs.get(fid, [])
                if targeting_reqs:
                    with ui.row().classes("flex-wrap gap-1 mt-1"):
                        ui.label("Compliance items:").classes("text-xs opacity-60 self-center")
                        for req_id in targeting_reqs:
                            ui.chip(req_id).props("color=blue-2 text-color=black dense")
                else:
                    ui.label("(no compliance items mapped to this factor)").classes("text-xs opacity-40 mt-1")

    # ── Trade-off / LPTA verbatim quotes ─────────────────────────────────
    if trade_off:
        ui.label("Trade-off language (verbatim)").classes("text-base font-semibold mt-4")
        with ui.card().classes("w-full bg-amber-50"):
            ui.label(trade_off).classes("text-sm italic")

    if lpta:
        ui.label("Lowest-price clause (verbatim)").classes("text-base font-semibold mt-4")
        with ui.card().classes("w-full bg-purple-50"):
            ui.label(lpta).classes("text-sm italic")

    # ── Extraction notes (collapsible) ───────────────────────────────────
    if notes:
        with ui.expansion("Agent notes", icon="info", value=False).classes("w-full mt-4"):
            ui.label(notes).classes("text-sm")
