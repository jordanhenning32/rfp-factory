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


def _render_compliance_tab(
    matrix_rows: list[dict],
    requirements_reviews: list[dict] | None = None,
) -> None:
    review_rows = requirements_reviews or []
    recovered_by_document = {
        int(row["id"]): set(
            (row.get("review") or {}).get("recovered_requirement_ids") or []
        )
        for row in review_rows
        if row.get("id") is not None
    }
    if review_rows:
        with ui.card().classes("w-full mb-3"):
            ui.label("Requirements review coverage").classes(
                "text-base font-medium"
            )
            ui.label(
                "Independent classification and source-completeness results "
                "are retained for each source file."
            ).classes("text-xs opacity-60")
            for row in review_rows:
                review = dict(row.get("review") or {})
                if not review:
                    continue
                status = str(review.get("status") or "waiting")
                status_visual = {
                    "pending": ("Queued for review", "blue-2", "schedule"),
                    "extracting": ("Extracting", "blue-2", "description"),
                    "reviewing": ("Reviewing", "blue-2", "fact_check"),
                    "complete": ("Complete", "green-2", "verified"),
                    "review_required": (
                        "Human review needed",
                        "amber-2",
                        "warning",
                    ),
                    "degraded": (
                        "Fallback used — human review needed",
                        "amber-2",
                        "warning",
                    ),
                    "not_applicable": (
                        "Not a requirements source",
                        "blue-grey-2",
                        "grid_on",
                    ),
                    "partial": ("Review incomplete", "red-2", "error"),
                    "failed": ("Review failed", "red-2", "error"),
                    "unknown": ("Invalid review state", "red-2", "error"),
                }.get(status, ("Waiting", "blue-grey-2", "schedule"))
                label, color, icon = status_visual
                classification = dict(review.get("classification") or {})
                completeness = dict(review.get("completeness") or {})
                extraction = dict(review.get("extraction") or {})
                with ui.row().classes("items-start gap-3 w-full py-1"):
                    ui.icon(icon)
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(str(row.get("filename") or "Source document")).classes(
                            "text-sm font-medium"
                        )
                        details: list[str] = []
                        if extraction.get("model"):
                            details.append(f"extraction {extraction['model']}")
                        if classification.get("primary_model"):
                            details.append(
                                f"independent review {classification['primary_model']}"
                            )
                        if classification.get("total_count") is not None:
                            details.append(
                                f"items {classification.get('reviewed_count', 0)}/"
                                f"{classification.get('total_count', 0)}"
                            )
                        extraction_coverage = dict(
                            extraction.get("coverage") or {}
                        )
                        if extraction_coverage.get("source_chunks_total"):
                            details.append(
                                "extraction chunks "
                                f"{extraction_coverage.get('source_chunks_completed', 0)}/"
                                f"{extraction_coverage.get('source_chunks_total', 0)}"
                            )
                        if extraction_coverage.get("state") not in {
                            None,
                            "complete",
                        }:
                            details.append(
                                "extraction coverage "
                                + str(extraction_coverage.get("state"))
                            )
                        if completeness.get("source_units_total") is not None:
                            details.append(
                                f"source units {completeness.get('reviewed_units', 0)}/"
                                f"{completeness.get('source_units_total', 0)}"
                            )
                        if classification.get("fallback_used") or completeness.get(
                            "fallback_used"
                        ):
                            details.append(
                                "fallback "
                                + str(
                                    classification.get("fallback_model")
                                    or completeness.get("fallback_model")
                                )
                            )
                        recovered = extraction.get("recovered_item_count") or 0
                        if recovered:
                            details.append(f"{recovered} omission(s) recovered")
                        auto_applied_count = int(
                            classification.get("auto_applied_count") or 0
                        )
                        if auto_applied_count:
                            details.append(f"{auto_applied_count} auto-corrected")
                        human_review_count = (
                            int(classification.get("manual_review_count") or 0)
                            + int(
                                completeness.get("manual_review_candidate_count") or 0
                            )
                            + int(completeness.get("uncertain_passage_count") or 0)
                        )
                        if human_review_count:
                            details.append(f"{human_review_count} need human review")
                        if details:
                            ui.label(" · ".join(details)).classes(
                                "text-xs opacity-65"
                            )
                        unresolved_ids = list(
                            classification.get("unresolved_requirement_ids") or []
                        )
                        if unresolved_ids:
                            ui.label(
                                "Unreviewed requirement IDs: "
                                + ", ".join(str(value) for value in unresolved_ids)
                            ).classes("text-xs text-red-700")
                        review_reason = str(review.get("reason") or "").strip()
                        if review_reason:
                            ui.label(review_reason).classes("text-xs opacity-65")
                    ui.chip(label, icon=icon).props(
                        f"dense color={color} text-color=black"
                    )

                extraction_reasons = list(
                    extraction_coverage.get("incomplete_reasons") or []
                )
                failed_chunk_labels = list(
                    extraction_coverage.get("failed_chunk_labels") or []
                )
                if extraction_reasons or failed_chunk_labels:
                    with ui.expansion(
                        "Source extraction coverage details",
                        icon="document_scanner",
                        value=False,
                    ).classes("w-full ml-8"):
                        for reason in extraction_reasons:
                            ui.label(str(reason).replace("_", " ")).classes(
                                "text-xs"
                            )
                        if failed_chunk_labels:
                            ui.label(
                                "Failed chunks: "
                                + ", ".join(str(value) for value in failed_chunk_labels)
                            ).classes("text-xs text-red-700")

                manual = list(completeness.get("manual_review") or [])
                if manual:
                    manual_count = int(
                        completeness.get("manual_review_candidate_count")
                        or len(manual)
                    )
                    with ui.expansion(
                        f"{manual_count} possible omission(s) for human review",
                        icon="manage_search",
                        value=False,
                    ).classes("w-full ml-8"):
                        for finding in manual:
                            page = finding.get("source_page") or "?"
                            confidence = finding.get("confidence") or ""
                            reason = finding.get("reason") or "Possible omission"
                            ui.label(
                                f"Page {page} · {confidence} · {reason}"
                            ).classes("text-xs")
                            requirement_text = str(
                                finding.get("requirement_text") or ""
                            ).strip()
                            if requirement_text:
                                ui.label(requirement_text).classes(
                                    "text-xs opacity-75 ml-3"
                                )
                        if manual_count > len(manual):
                            ui.label(
                                f"Showing the first {len(manual)} candidates."
                            ).classes("text-xs opacity-60")

                uncertain = list(completeness.get("uncertain_passages") or [])
                if uncertain:
                    uncertain_count = int(
                        completeness.get("uncertain_passage_count")
                        or len(uncertain)
                    )
                    with ui.expansion(
                        f"{uncertain_count} uncertain source passage(s)",
                        icon="help_outline",
                        value=False,
                    ).classes("w-full ml-8"):
                        for passage in uncertain:
                            page = passage.get("source_page") or "?"
                            reason = passage.get("reason") or "Source review is uncertain."
                            ui.label(f"Page {page} · {reason}").classes("text-xs")
                        if uncertain_count > len(uncertain):
                            ui.label(
                                f"Showing the first {len(uncertain)} passages."
                            ).classes("text-xs opacity-60")

                classification_manual = list(
                    classification.get("manual_review") or []
                )
                if classification_manual:
                    classification_manual_count = int(
                        classification.get("manual_review_count")
                        or len(classification_manual)
                    )
                    with ui.expansion(
                        f"{classification_manual_count} classification finding(s) "
                        "for human review",
                        icon="rule",
                        value=False,
                    ).classes("w-full ml-8"):
                        for finding in classification_manual:
                            requirement_id = (
                                finding.get("requirement_id") or "Unknown requirement"
                            )
                            confidence = finding.get("confidence") or ""
                            issue = finding.get("issue") or "classification finding"
                            role = finding.get("review_role") or "primary"
                            ui.label(
                                f"{requirement_id} · {confidence} · {issue} "
                                f"· {role} reviewer"
                            ).classes("text-xs font-medium")
                            reason = str(finding.get("reason") or "").strip()
                            if reason:
                                ui.label(reason).classes("text-xs opacity-75 ml-3")
                            suggestions = []
                            if finding.get("suggested_type"):
                                suggestions.append(
                                    f"type → {finding['suggested_type']}"
                                )
                            if finding.get("suggested_category"):
                                suggestions.append(
                                    f"category → {finding['suggested_category']}"
                                )
                            if suggestions:
                                ui.label(
                                    "Suggested: " + ", ".join(suggestions)
                                ).classes("text-xs text-amber-800 ml-3")
                        if classification_manual_count > len(classification_manual):
                            ui.label(
                                f"Showing the first {len(classification_manual)} findings."
                            ).classes("text-xs opacity-60")

                auto_applied = list(classification.get("auto_applied") or [])
                if auto_applied:
                    auto_count = int(
                        classification.get("auto_applied_change_count")
                        or len(auto_applied)
                    )
                    with ui.expansion(
                        f"{auto_count} automatic classification field correction(s)",
                        icon="published_with_changes",
                        value=False,
                    ).classes("w-full ml-8"):
                        for change in auto_applied:
                            ui.label(
                                f"{change.get('requirement_id') or '?'} · "
                                f"{change.get('field') or 'field'}: "
                                f"{change.get('from') or '—'} → "
                                f"{change.get('to') or '—'}"
                            ).classes("text-xs font-medium")
                            reason = str(change.get("reason") or "").strip()
                            if reason:
                                ui.label(reason).classes("text-xs opacity-75 ml-3")
                        if auto_count > len(auto_applied):
                            ui.label(
                                f"Showing the first {len(auto_applied)} corrections."
                            ).classes("text-xs opacity-60")

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
    docs: set[tuple[str, object]] = set()
    n_amended = 0
    for r in active_rows:
        type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1
        cat_counts[r["category"]] = cat_counts.get(r["category"], 0) + 1
        if r.get("source_document_id") is not None:
            docs.add(("id", r["source_document_id"]))
        else:
            docs.add(("name", r["source_doc"]))
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
            recovered = r.get("requirement_id") in recovered_by_document.get(
                r.get("source_document_id"),
                set(),
            )
            status_cell = (
                badge_label
                or ("recovered by source audit" if recovered else None)
                or (r.get("status") or "active")
            )
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
