"""Proposal Review > Spend tab.

LLM-spend dashboard for one proposal. Read-only view over the
agent_runs table (every call_tool_for_model invocation records
input/output tokens + USD cost). Bar chart by pipeline stage on top,
per-agent table in a collapsible expansion below.

Self-refresh via a manual button — no live polling needed since the
user already sees stage progress on the Run Progress page.
"""

from __future__ import annotations

from nicegui import ui

from app.ui._shared import _empty_state


def _render_spend_tab(proposal_id: int) -> None:
    """Spend dashboard — bar chart of $ spent per pipeline stage,
    plus per-agent detail. Pulls from agent_runs (cost_usd, tokens
    are recorded by every call_tool_for_model call). Read-only.

    Self-refreshes via a manual button — no live polling needed since
    the user already sees stage progress on Run Progress.
    """
    from app.services.cost_dashboard import compute_proposal_costs

    @ui.refreshable
    def render() -> None:
        summary = compute_proposal_costs(proposal_id)

        if summary.total_calls == 0:
            _empty_state(
                "No agent runs recorded yet. Spend appears here once the pipeline starts making LLM calls.",
                icon="savings",
            )
            return

        # Top summary card
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full flex-wrap gap-3"):
                with ui.column().classes("gap-0"):
                    ui.label(
                        f"${summary.total_cost_usd:.4f}"
                    ).classes("text-2xl font-semibold")
                    ui.label("estimated API cost from recorded tokens").classes(
                        "text-xs opacity-70"
                    )
                with ui.column().classes("gap-0"):
                    ui.label(f"{summary.total_calls:,} calls").classes("text-base font-medium")
                    in_tok = summary.total_input_tokens
                    out_tok = summary.total_output_tokens
                    ui.label(f"{in_tok:,} input tok · {out_tok:,} output tok").classes(
                        "text-xs opacity-70 font-mono"
                    )
                if summary.first_run_at and summary.last_run_at:
                    span_min = (summary.last_run_at - summary.first_run_at).total_seconds() / 60.0
                    with ui.column().classes("gap-0"):
                        ui.label(f"{span_min:.1f} min span").classes("text-base font-medium")
                        ui.label(
                            f"first {summary.first_run_at:%Y-%m-%d %H:%M:%S} · "
                            f"last {summary.last_run_at:%H:%M:%S}"
                        ).classes("text-xs opacity-70 font-mono")
                ui.element("div").classes("flex-1")
                ui.button(
                    "Refresh",
                    icon="refresh",
                    on_click=render.refresh,
                ).props("flat dense")

        # Per-stage breakdown with inline bars
        with ui.card().classes("w-full"):
            ui.label("By pipeline stage").classes("text-base font-medium pb-1")
            ui.label(
                "Stages logically group related agent calls — e.g., "
                "Compliance Matrix bundles source extraction, independent "
                "classification, source-completeness review, retries, and "
                "fallbacks. Bars are normalized to total estimated API cost."
            ).classes("text-xs opacity-60 pb-2")
            max_stage_cost = max((s.cost_usd for s in summary.stages), default=0.0) or 1.0
            for st in summary.stages:
                pct = (st.cost_usd / summary.total_cost_usd) * 100 if summary.total_cost_usd > 0 else 0.0
                bar_pct = (st.cost_usd / max_stage_cost) * 100 if max_stage_cost > 0 else 0.0
                with ui.row().classes("items-center w-full gap-3 pt-1"):
                    ui.label(st.stage).classes("text-sm w-44 truncate")
                    # Bar — colored div sized as % of max-stage. Width
                    # animates via CSS transition for nicer refresh
                    # behavior. Outer track gives a visual ceiling.
                    with ui.row().classes("flex-1 h-5 bg-slate-100 rounded overflow-hidden relative"):
                        ui.element("div").classes("h-full bg-blue-400").style(
                            f"width: {bar_pct:.1f}%; transition: width 0.4s ease-out;"
                        )
                    ui.label(f"${st.cost_usd:.4f}").classes("text-sm font-mono w-20 text-right")
                    ui.label(f"{pct:>5.1f}%").classes("text-xs font-mono w-12 text-right opacity-70")
                    ui.label(f"{st.n_calls:>3} calls").classes("text-xs font-mono w-16 text-right opacity-70")

        # Per-agent breakdown — collapsible since users mostly want the
        # stage view, but the underlying detail is here when needed.
        with ui.expansion(
            "Per-agent breakdown",
            icon="data_object",
            value=False,
        ).classes("w-full"):
            ui.label(
                "Granular view — every distinct agent_name, sorted by "
                "cost descending. Useful for spotting which specific "
                "agent inside a stage is the hot spot (e.g., is "
                "review spend is driven by repeated revision passes or by "
                "the initial pass?)."
            ).classes("text-xs opacity-60 pb-2")
            columns = [
                {
                    "name": "agent_name",
                    "label": "Agent",
                    "field": "agent_name",
                    "align": "left",
                    "sortable": True,
                },
                {"name": "stage", "label": "Stage", "field": "stage", "align": "left", "sortable": True},
                {"name": "n_calls", "label": "Calls", "field": "n_calls", "sortable": True},
                {"name": "input_tokens", "label": "Input tok", "field": "input_tokens", "sortable": True},
                {"name": "output_tokens", "label": "Output tok", "field": "output_tokens", "sortable": True},
                {"name": "cost_usd", "label": "$ spend", "field": "cost_usd", "sortable": True},
            ]
            rows = [
                {
                    "agent_name": a.agent_name,
                    "stage": a.stage,
                    "n_calls": a.n_calls,
                    "input_tokens": f"{a.input_tokens:,}",
                    "output_tokens": f"{a.output_tokens:,}",
                    "cost_usd": f"${a.cost_usd:.4f}",
                }
                for a in summary.agents
            ]
            ui.table(
                columns=columns,
                rows=rows,
                row_key="agent_name",
                pagination=20,
            ).classes("w-full")

    render()
