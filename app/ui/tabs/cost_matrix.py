"""Cost Matrix panel embedded at the top of the Cost workspace."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from nicegui import ui

from app.services.cost_matrix import (
    RECONCILIATION_MAPPING_KEY,
    CostMatrixNotReadyError,
    add_cost_matrix_target,
    attach_cost_matrix,
    confirm_cost_matrix,
    dismiss_cost_matrix,
    generate_cost_matrix,
    get_cost_matrix_download,
    get_cost_matrix_snapshots,
    save_cost_matrix_mapping,
)

log = logging.getLogger(__name__)


_STATUS = {
    "needs_confirmation": ("Possible matrix — decision needed", "amber"),
    "dismissed": ("Ordinary attachment", "grey"),
    "mapping_required": ("Mapping required", "amber"),
    "waiting_for_costs": ("Waiting for approved costs", "blue-grey"),
    "ready": ("Ready to generate", "positive"),
    "generated": ("Generated and current", "positive"),
    "stale": ("Generated output is stale", "negative"),
    "error": ("Needs attention", "negative"),
    "detected": ("Detected", "amber"),
}


def _display_value(source: dict[str, Any]) -> str:
    value = source.get("value")
    if value is None:
        return "not available"
    if source.get("kind") == "money" and isinstance(value, (int, float)):
        return f"${value:,.2f}"
    if source.get("kind") == "percentage" and isinstance(value, (int, float)):
        return f"{value * 100:g}%"
    if isinstance(value, float):
        return f"{value:,.4g}"
    return str(value)


def _compatible_source(target: dict[str, Any], source: dict[str, Any]) -> bool:
    return str(target.get("kind") or "text") == str(source.get("kind") or "")


def _render_mapping_editor(
    proposal_id: int,
    matrix: dict[str, Any],
    *,
    on_refresh: Callable[[], None],
) -> None:
    artifact_id = int(matrix["id"])
    targets = list((matrix.get("analysis") or {}).get("targets") or [])
    mappings = matrix.get("mapping") or {}
    suggestions = (matrix.get("readiness") or {}).get("suggestions") or {}
    sources = list(matrix.get("sources") or [])
    controls: list[tuple[dict[str, Any], Any, Any]] = []
    reconciliation_select = None
    reconciliation_detail = None

    if not targets:
        ui.label(
            "The workbook was detected, but no writable cells were inferred. "
            "Add the exact cells requested by this buyer below."
        ).classes("text-sm text-amber-800")

    for target in targets:
        target_id = str(target["id"])
        mapping = mappings.get(target_id) or {}
        suggestion = suggestions.get(target_id) or {}
        options: dict[str, str] = {
            "": "Not mapped",
            "__manual__": "Manual approved value",
            "__skip__": "Leave unchanged — explain why",
        }
        grouped_sources = [
            source for source in sources if _compatible_source(target, source)
        ]
        for source in grouped_sources:
            options[str(source["key"])] = (
                f"{source['label']} · {_display_value(source)}"
            )

        if mapping.get("mode") == "source":
            selected = str(mapping.get("source_key") or "")
            if selected and selected not in options:
                options[selected] = f"Unavailable source: {selected} — remap required"
        elif mapping.get("mode") == "manual":
            selected = "__manual__"
        elif mapping.get("mode") == "skip":
            selected = "__skip__"
        elif suggestion.get("source_key") in options:
            selected = str(suggestion["source_key"])
        else:
            selected = ""

        with ui.card().classes("w-full p-3 gap-2 shadow-none border border-slate-200"):
            with ui.row().classes("w-full items-start gap-3"):
                with ui.column().classes("min-w-64 flex-1 gap-0"):
                    ui.label(str(target.get("label") or target["cell"])).classes(
                        "font-medium text-sm"
                    )
                    ui.label(
                        f"{target['sheet']}!{target['cell']} · "
                        f"{str(target.get('kind') or 'value').title()}"
                    ).classes("text-xs opacity-60")
                    if target.get("header"):
                        ui.label(f"Column/header: {target['header']}").classes(
                            "text-xs opacity-60"
                        )
                    if target_id in suggestions and not mapping:
                        ui.badge("Suggested — confirm by saving", color="blue").props(
                            "outline"
                        ).classes("mt-1")
                select_input = ui.select(
                    options=options,
                    value=selected,
                    label="Populate from",
                ).classes("min-w-80 flex-1")
            detail_value = ""
            detail_label = (
                "Manual value (for example 24% or 0.24) or skip explanation"
                if target.get("kind") == "percentage"
                else "Manual value or skip explanation"
            )
            if mapping.get("mode") == "manual":
                detail_value = str(mapping.get("value") if mapping.get("value") is not None else "")
            elif mapping.get("mode") == "skip":
                detail_value = str(mapping.get("reason") or "")
            detail_input = ui.input(
                detail_label,
                value=detail_value,
                placeholder=(
                    "Required only for Manual approved value or Leave unchanged"
                ),
            ).classes("w-full")
            controls.append((target, select_input, detail_input))

    reconciliation_review = (
        (matrix.get("analysis") or {}).get("reconciliation_review") or {}
    )
    if reconciliation_review.get("review_required"):
        saved_reconciliation = mappings.get(RECONCILIATION_MAPPING_KEY) or {}
        with ui.card().classes(
            "w-full p-3 gap-2 shadow-none border border-amber-300 bg-amber-50"
        ):
            ui.label("Review how buyer totals reconcile").classes(
                "font-medium text-sm text-amber-900"
            )
            ui.label(
                "This template has multiple or partial total formulas, so the system "
                "will not assume each one equals the full proposal price."
            ).classes("text-xs text-amber-900")
            reconciliation_select = ui.select(
                options={
                    "": "Decision required",
                    "aggregate_to_proposal_total": (
                        "All unique financial inputs add to the proposal total"
                    ),
                    "independent_totals": (
                        "Totals are independent — document why"
                    ),
                },
                value=str(saved_reconciliation.get("mode") or ""),
                label="Reconciliation treatment",
            ).classes("w-full")
            reconciliation_detail = ui.input(
                "Explanation (required for independent totals)",
                value=str(saved_reconciliation.get("reason") or ""),
            ).classes("w-full")

    def save_mapping() -> None:
        payload: dict[str, dict[str, Any]] = {}
        for target, select_input, detail_input in controls:
            selected = str(select_input.value or "")
            target_id = str(target["id"])
            if not selected:
                continue
            if selected == "__manual__":
                payload[target_id] = {
                    "mode": "manual",
                    "value": detail_input.value,
                    "note": "Approved in Cost Matrix workspace",
                }
            elif selected == "__skip__":
                payload[target_id] = {
                    "mode": "skip",
                    "reason": detail_input.value,
                }
            else:
                payload[target_id] = {
                    "mode": "source",
                    "source_key": selected,
                }
        if reconciliation_select is not None and reconciliation_select.value:
            payload[RECONCILIATION_MAPPING_KEY] = {
                "mode": str(reconciliation_select.value),
                "reason": str(
                    reconciliation_detail.value if reconciliation_detail is not None else ""
                ),
            }
        try:
            save_cost_matrix_mapping(proposal_id, artifact_id, payload)
        except Exception as exc:
            log.exception("cost matrix mapping save failed")
            ui.notify(str(exc), type="negative", timeout=8000)
            return
        ui.notify("Cost matrix mapping saved.", type="positive")
        on_refresh()

    if controls or reconciliation_select is not None:
        ui.button("Save reviewed mapping", icon="save", on_click=save_mapping).props(
            "outline color=primary"
        )

    with ui.expansion("Add a workbook cell the inspector missed", icon="add_box").classes(
        "w-full"
    ):
        sheet_options = {
            sheet["name"]: sheet["name"]
            for sheet in (matrix.get("analysis") or {}).get("sheets", [])
            if sheet.get("state") == "visible"
        }
        with ui.row().classes("w-full gap-2 items-end"):
            sheet_input = ui.select(
                options=sheet_options,
                value=next(iter(sheet_options), None),
                label="Exact sheet",
            ).classes("min-w-64")
            cell_input = ui.input("Cell", placeholder="C9").classes("w-28")
            label_input = ui.input("Buyer row/field label").classes("flex-1")
            kind_input = ui.select(
                options={
                    "money": "Money",
                    "number": "Number",
                    "percentage": "Percentage",
                    "text": "Text",
                    "date": "Date",
                },
                value="money",
                label="Value type",
            ).classes("w-36")

            def add_target() -> None:
                try:
                    add_cost_matrix_target(
                        proposal_id,
                        artifact_id,
                        sheet=str(sheet_input.value or ""),
                        cell_coordinate=str(cell_input.value or ""),
                        label=str(label_input.value or ""),
                        kind=str(kind_input.value or "money"),
                    )
                except Exception as exc:
                    log.exception("add cost matrix target failed")
                    ui.notify(str(exc), type="negative", timeout=8000)
                    return
                ui.notify("Workbook cell added to the mapping.", type="positive")
                on_refresh()

            ui.button("Add cell", icon="add", on_click=add_target).props("outline")


def _render_matrix_card(
    proposal_id: int,
    matrix: dict[str, Any],
    *,
    on_refresh: Callable[[], None],
) -> None:
    status_label, status_color = _STATUS.get(
        str(matrix.get("status")),
        (str(matrix.get("status") or "Detected").replace("_", " ").title(), "grey"),
    )
    with ui.card().classes("w-full gap-3 border border-slate-200 shadow-none"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("table_view").classes("text-emerald-700")
            ui.label(matrix["filename"]).classes("font-semibold flex-1")
            ui.badge(status_label, color=status_color).props("outline")

        analysis = matrix.get("analysis") or {}
        pricing_targets = [
            target for target in analysis.get("targets", [])
            if target.get("category") == "pricing"
        ]

        def confirm_candidate() -> None:
            try:
                confirm_cost_matrix(proposal_id, int(matrix["id"]))
            except Exception as exc:
                log.exception("cost matrix confirmation failed")
                ui.notify(str(exc), type="negative", timeout=8000)
                return
            ui.notify("Workbook confirmed as a required cost matrix.", type="positive")
            on_refresh()

        def dismiss_candidate() -> None:
            try:
                dismiss_cost_matrix(proposal_id, int(matrix["id"]))
            except Exception as exc:
                log.exception("cost matrix dismissal failed")
                ui.notify(str(exc), type="negative", timeout=8000)
                return
            ui.notify("Workbook retained as an ordinary RFP attachment.", type="positive")
            on_refresh()

        if matrix.get("status") == "needs_confirmation":
            ui.label(
                "This workbook looks price-related, but no reliable fillable matrix was "
                "confirmed. It remains in normal requirements intake until you decide."
            ).classes("text-sm text-amber-900")
            with ui.row().classes("w-full gap-2"):
                ui.button(
                    "Confirm required cost matrix",
                    icon="check_circle",
                    on_click=confirm_candidate,
                )
                ui.button(
                    "Treat as ordinary attachment",
                    icon="description",
                    on_click=dismiss_candidate,
                ).props("outline")
            return

        if matrix.get("status") == "dismissed":
            decision = analysis.get("operator_decision") or {}
            ui.label(
                "This workbook is retained with the RFP package but no longer blocks "
                "cost-matrix completion or submission."
            ).classes("text-sm opacity-70")
            if decision.get("reason"):
                ui.label(str(decision["reason"])).classes("text-xs opacity-60")
            ui.button(
                "Reclassify as a required cost matrix",
                icon="table_view",
                on_click=confirm_candidate,
            ).props("outline")
            return

        ui.label(
            f"Detected immediately · {len(pricing_targets)} financial cell(s) · "
            f"{len(analysis.get('formulas') or [])} formula cell(s) preserved · "
            f"source SHA-256 {matrix['template_sha256'][:12]}…"
        ).classes("text-xs opacity-70")
        if analysis.get("reconciliations"):
            ui.label(
                "Buyer total rule detected — mapped line items must reconcile "
                "to the selected proposed price within $0.01."
            ).classes("text-xs text-emerald-800")

        warnings = list(analysis.get("warnings") or [])
        if warnings:
            with ui.expansion(f"Workbook preservation notes ({len(warnings)})", icon="warning").classes(
                "w-full text-sm"
            ):
                for warning in warnings:
                    ui.label(f"• {warning}").classes("text-xs text-amber-900")

        with ui.expansion("Review template-specific cell mapping", icon="account_tree").classes(
            "w-full"
        ):
            _render_mapping_editor(
                proposal_id,
                matrix,
                on_refresh=on_refresh,
            )

        blockers = list((matrix.get("readiness") or {}).get("blockers") or [])
        if blockers:
            with ui.column().classes("w-full gap-0 rounded bg-amber-50 p-3"):
                ui.label("Held until these items are resolved:").classes(
                    "font-medium text-sm text-amber-900"
                )
                for blocker in blockers:
                    ui.label(f"• {blocker}").classes("text-xs text-amber-900")

        latest = matrix.get("latest_output")
        with ui.row().classes("w-full items-center gap-2"):
            def generate() -> None:
                try:
                    generate_cost_matrix(proposal_id, int(matrix["id"]))
                except CostMatrixNotReadyError as exc:
                    ui.notify(str(exc), type="warning", timeout=10000)
                    return
                except Exception as exc:
                    log.exception("cost matrix generation failed")
                    ui.notify(str(exc), type="negative", timeout=10000)
                    return
                ui.notify(
                    "Completed workbook generated from the immutable template.",
                    type="positive",
                )
                on_refresh()

            generate_button = ui.button(
                "Regenerate completed copy" if latest else "Generate completed copy",
                icon="auto_fix_high",
                on_click=generate,
            )
            if not (matrix.get("readiness") or {}).get("ready"):
                generate_button.disable()

            if latest:
                def download(output_id: int = int(latest["id"])) -> None:
                    try:
                        data, filename = get_cost_matrix_download(output_id)
                        ui.download(data, filename=filename)
                    except Exception as exc:
                        log.exception("cost matrix download failed")
                        ui.notify(str(exc), type="negative")

                ui.button(
                    "Download current copy" if latest.get("current") else "Download prior copy",
                    icon="download",
                    on_click=download,
                ).props("outline")
                ui.label(
                    f"v{latest['version']} · {latest['generated_at'][:10]} · "
                    f"{latest.get('pricing_scenario') or 'approved payment basis'}"
                ).classes("text-xs opacity-60")
        ui.button(
            "This is not a fillable cost matrix",
            icon="undo",
            on_click=dismiss_candidate,
        ).props("flat color=grey")


def render_cost_matrix_panel(
    proposal_id: int,
    *,
    on_refresh: Callable[[], None],
) -> None:
    """Render early awareness, late attachment, mapping, and output actions."""
    try:
        matrices = get_cost_matrix_snapshots(proposal_id)
    except Exception as exc:
        log.exception("cost matrix snapshot failed")
        ui.label(f"Cost matrix workspace unavailable: {exc}").classes(
            "text-sm text-red-700"
        )
        return

    panel = ui.card().classes("w-full gap-3 border-2 border-emerald-100")
    panel.props("data-testid=cost-matrix-panel")
    with panel:
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("dataset").classes("text-emerald-700 text-xl")
            with ui.column().classes("gap-0 flex-1"):
                ui.label("Buyer Cost Matrices").classes("text-lg font-semibold")
                ui.label(
                    "Every workbook is inspected and mapped independently. Originals are "
                    "preserved; completion waits for reviewed pricing."
                ).classes("text-xs opacity-70")

        async def upload_later(event) -> None:
            try:
                data = await event.file.read()
                filename = event.file.name
                await asyncio.to_thread(
                    attach_cost_matrix,
                    proposal_id,
                    filename=filename,
                    content=data,
                )
            except Exception as exc:
                log.exception("late cost matrix attachment failed")
                ui.notify(str(exc), type="negative", timeout=10000)
                return
            ui.notify(
                f"Attached {filename}; it is detected and held for pricing.",
                type="positive",
            )
            on_refresh()

        ui.upload(
            label="Add a cost matrix later",
            auto_upload=True,
            on_upload=upload_later,
        ).props("accept=.xlsx").classes("w-full")

        if not matrices:
            ui.label(
                "No cost matrix was included in the original package. Add one here if "
                "the buyer supplies it later."
            ).classes("text-sm opacity-70")
        for matrix in matrices:
            _render_matrix_card(
                proposal_id,
                matrix,
                on_refresh=on_refresh,
            )


__all__ = ["render_cost_matrix_panel"]
