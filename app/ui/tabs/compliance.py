"""Proposal Review > Compliance tab.

Renders the verbatim compliance matrix the Compliance Matrix Agent
extracted from the RFP — every requirement, submission instruction,
and evaluation criterion with its source page and (when stated)
evaluation weight. Read-only display; edits happen elsewhere
(Outline tab assigns items to sections; Outline-exclusion toggles
live on the Outline tab).

When amendment ingestion runs, rows pick up `amendment_origin` /
`status` / `superseded_by_id`. The tab defaults to "Active only" so
the user sees the current state of every requirement; the toggle
switches to "All statuses" for an audit-style view that includes
superseded + removed rows.
"""

from __future__ import annotations

from nicegui import ui

from app.ui._shared import _empty_state

# Compliance-matrix row colors per requirement type — visual scan aid.
# Quasar palette tokens; safe across the app's Tailwind play CDN.
_REQ_TYPE_COLOR = {
    "shall": "red-3",
    "must": "red-3",
    "should": "amber-3",
    "submission_format": "blue-3",
    "evaluation_criterion": "purple-3",
    "mandatory_form": "teal-3",
}


def _status_badge(row: dict, all_rows: list[dict]) -> tuple[str, str]:
    """Return (label, color-prop-fragment) for the per-row status badge.

    - active + amendment_origin → blue "amended: <filename>"
    - superseded → red "superseded" (best-effort hover: not rendered in the
      table because Quasar's table cell renderer doesn't support tooltips
      directly — we accept this v1 trade-off; the user filters to "All
      statuses" to see the new row right next to it).
    - removed → grey "removed"
    - active + no amendment_origin → empty
    """
    status = row.get("status") or "active"
    origin = row.get("amendment_origin")
    if status == "superseded":
        return ("superseded", "color=red-3 text-color=black")
    if status == "removed":
        return ("removed", "color=blue-grey-3 text-color=black")
    if status == "active" and origin:
        return (f"amended: {origin}", "color=blue-3 text-color=black")
    return ("", "")


def _render_compliance_tab(matrix_rows: list[dict]) -> None:
    if not matrix_rows:
        _empty_state(
            "No compliance items yet. They populate as the Compliance Matrix Agent runs.",
            icon="rule",
        )
        return

    # Default to active-only for the most useful summary; the toggle
    # below switches to All for audit / amendment-history viewing.
    status_filter = ui.toggle(
        ["Active only", "All statuses"],
        value="Active only",
    ).classes("mb-2")

    def _filter_rows(rows: list[dict], mode: str) -> list[dict]:
        if mode == "Active only":
            return [r for r in rows if (r.get("status") or "active") == "active"]
        return rows

    # Aggregate counts by type / category for the summary chips
    # (compute against ACTIVE rows so the counts are meaningful by default;
    # All-statuses keeps the same chips — they're informational).
    active_rows = _filter_rows(matrix_rows, "Active only")
    type_counts: dict[str, int] = {}
    cat_counts: dict[str, int] = {}
    docs: set[str] = set()
    n_amended = 0
    for r in active_rows:
        type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1
        cat_counts[r["category"]] = cat_counts.get(r["category"], 0) + 1
        docs.add(r["source_doc"])
        if r.get("amendment_origin"):
            n_amended += 1

    with ui.row().classes("flex-wrap gap-2 pt-2"):
        ui.chip(f"{len(active_rows)} active items", icon="rule").props("color=primary text-color=white")
        ui.chip(f"{len(docs)} document(s)", icon="folder").props("color=blue-grey-3 text-color=black")
        if n_amended:
            ui.chip(f"{n_amended} amended").props("color=blue-3 text-color=black")

    with ui.expansion("By type", icon="category", value=False).classes("w-full"):
        with ui.row().classes("flex-wrap gap-2"):
            for t, n in sorted(type_counts.items(), key=lambda kv: -kv[1]):
                color = _REQ_TYPE_COLOR.get(t, "blue-grey-3")
                ui.chip(f"{t}: {n}").props(f"color={color} text-color=black")

    with ui.expansion("By category", icon="folder_open", value=False).classes("w-full"):
        with ui.row().classes("flex-wrap gap-2"):
            for c, n in sorted(cat_counts.items(), key=lambda kv: -kv[1]):
                ui.chip(f"{c}: {n}").props("color=blue-grey-3 text-color=black")

    # The actual table. Renderer is wrapped in a refresh-on-toggle helper
    # so the user's "Active only" / "All statuses" selection re-renders
    # the row set in place without reloading the page.
    columns = [
        {
            "name": "requirement_id",
            "label": "ID",
            "field": "requirement_id",
            "align": "left",
            "sortable": True,
        },
        {
            "name": "requirement_text",
            "label": "Requirement (verbatim)",
            "field": "requirement_text",
            "align": "left",
        },
        {"name": "type", "label": "Type", "field": "type", "sortable": True},
        {"name": "category", "label": "Category", "field": "category", "sortable": True},
        {"name": "source", "label": "Source", "field": "source"},
        {"name": "status", "label": "Status", "field": "status"},
        {"name": "weight", "label": "Wt", "field": "weight"},
    ]

    @ui.refreshable
    def _render_table() -> None:
        mode = status_filter.value or "Active only"
        visible = _filter_rows(matrix_rows, mode)
        rows = []
        for r in visible:
            badge_label, _ = _status_badge(r, matrix_rows)
            status_cell = badge_label or (r.get("status") or "active")
            rows.append(
                {
                    "id": r["id"],
                    "requirement_id": r["requirement_id"],
                    "requirement_text": r["requirement_text"],
                    "type": r["type"],
                    "category": r["category"],
                    "source": (
                        f"{r['source_doc']}"
                        + (f" · {r['source_section']}" if r["source_section"] else "")
                        + (f" · p.{r['source_page']}" if r["source_page"] else "")
                    ),
                    "status": status_cell,
                    "weight": r["weight"] if r["weight"] is not None else "—",
                }
            )
        ui.table(columns=columns, rows=rows, row_key="id", pagination=25).classes("w-full")

    status_filter.on_value_change(lambda _e: _render_table.refresh())
    _render_table()
