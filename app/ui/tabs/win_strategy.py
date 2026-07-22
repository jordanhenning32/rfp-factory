"""Proposal Review > Win Strategy tab.

Deterministic strategy workspace for improving evaluator fit before the
Writer Team or final review: scorecard, win themes, past-performance match
ranking, price posture, red-team risks, and recommended tables.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from app.services.win_strategy import (
    generate_all_win_strategy,
    generate_evaluator_scorecard,
    generate_graphics_tables,
    generate_past_performance_matches,
    generate_price_to_win,
    generate_red_team_findings,
    generate_win_themes,
    load_win_strategy,
)
from app.ui._shared import _empty_state


def _money(value: float | int | None) -> str:
    return f"${float(value):,.0f}" if value is not None else "TBD"


def _run(label: str, fn: Callable[[int], Any], proposal_id: int, *, on_change=None) -> None:
    try:
        fn(proposal_id)
    except Exception as exc:
        ui.notify(f"{label} failed: {type(exc).__name__}: {exc}", type="negative", multi_line=True)
        return
    ui.notify(f"{label} refreshed.", type="positive")
    if on_change is not None:
        on_change()
    ui.navigate.reload()


def _render_header(proposal_id: int, *, on_change=None) -> None:
    with ui.row().classes("w-full items-center justify-between gap-2 flex-wrap"):
        with ui.column().classes("gap-0"):
            ui.label("Win Strategy").classes("text-xl font-semibold")
            ui.label(
                "Evaluator scorecard, win themes, proof, pricing posture, red-team risks, and proposal tables."
            ).classes("text-sm opacity-70")
        with ui.row().classes("gap-2"):
            ui.button(
                "Generate All",
                icon="auto_awesome",
                on_click=lambda: _run("Win strategy", generate_all_win_strategy, proposal_id, on_change=on_change),
            ).props("color=primary")
            ui.button(
                "Scorecard",
                icon="scoreboard",
                on_click=lambda: _run("Evaluator scorecard", generate_evaluator_scorecard, proposal_id, on_change=on_change),
            ).props("flat")
            ui.button(
                "Red Team",
                icon="report",
                on_click=lambda: _run("Red team", generate_red_team_findings, proposal_id, on_change=on_change),
            ).props("flat")


def _render_scorecard(scorecard: dict | None, proposal_id: int, *, on_change=None) -> None:
    with ui.expansion("Evaluator Scorecard", icon="scoreboard", value=True).classes("w-full"):
        with ui.row().classes("w-full justify-end"):
            ui.button(
                "Refresh",
                icon="refresh",
                on_click=lambda: _run("Evaluator scorecard", generate_evaluator_scorecard, proposal_id, on_change=on_change),
            ).props("flat dense")
        if not scorecard:
            _empty_state("No evaluator scorecard yet.", icon="scoreboard")
            return
        with ui.row().classes("gap-2 flex-wrap"):
            ui.chip(f"Overall: {scorecard.get('overall_readiness')}").props("color=blue-2 text-color=black")
            ui.chip(f"Score: {scorecard.get('overall_score')}").props("color=grey-3 text-color=black")
            ui.chip(f"Method: {scorecard.get('method') or 'unknown'}").props("color=amber-2 text-color=black")
            ui.chip(f"Blockers: {scorecard.get('blocker_count', 0)}").props("color=red-2 text-color=black")
        rows = []
        for factor in scorecard.get("factors") or []:
            rows.append(
                {
                    "factor": f"{factor.get('factor_id')} {factor.get('factor_name')}",
                    "readiness": factor.get("readiness_band"),
                    "score": factor.get("score"),
                    "sections": ", ".join(factor.get("mapped_section_ids") or []),
                    "gaps": ", ".join(factor.get("unresolved_gap_ids") or []),
                    "findings": factor.get("open_findings_count"),
                }
            )
        ui.table(
            columns=[
                {"name": "factor", "label": "Factor", "field": "factor", "align": "left"},
                {"name": "readiness", "label": "Readiness", "field": "readiness"},
                {"name": "score", "label": "Score", "field": "score"},
                {"name": "sections", "label": "Sections", "field": "sections", "align": "left"},
                {"name": "gaps", "label": "Open Gaps", "field": "gaps", "align": "left"},
                {"name": "findings", "label": "Findings", "field": "findings"},
            ],
            rows=rows,
            row_key="factor",
        ).classes("w-full").props("dense flat")
        actions = scorecard.get("next_actions") or []
        if actions:
            ui.label("Next actions").classes("text-sm font-medium pt-2")
            for action in actions:
                ui.label(f"- {action}").classes("text-sm")


def _render_themes(themes: dict | None, proposal_id: int, *, on_change=None) -> None:
    with ui.expansion("Win Theme Control Room", icon="lightbulb", value=True).classes("w-full"):
        with ui.row().classes("w-full justify-end"):
            ui.button(
                "Refresh",
                icon="refresh",
                on_click=lambda: _run("Win themes", generate_win_themes, proposal_id, on_change=on_change),
            ).props("flat dense")
        items = (themes or {}).get("themes") or []
        if not items:
            _empty_state("No win themes yet.", icon="lightbulb")
            return
        for theme in items:
            with ui.card().classes("w-full mb-2"):
                with ui.row().classes("w-full items-center justify-between gap-2"):
                    ui.label(f"{theme.get('id')}  {theme.get('title')}").classes("text-base font-semibold")
                    ui.chip(theme.get("status", "active")).props("color=green-2 text-color=black dense")
                ui.label(theme.get("discriminator") or "").classes("text-sm")
                ui.label(f"Buyer pain: {theme.get('buyer_pain') or ''}").classes("text-xs opacity-70")
                with ui.row().classes("gap-1 flex-wrap pt-1"):
                    for fid in theme.get("linked_factor_ids") or []:
                        ui.chip(fid).props("color=blue-2 text-color=black dense")
                if theme.get("proof_points"):
                    ui.label("Proof").classes("text-xs font-medium pt-1")
                    for proof in theme["proof_points"]:
                        ui.label(f"- {proof}").classes("text-xs")


def _render_past_performance(matches: dict | None, proposal_id: int, *, on_change=None) -> None:
    with ui.expansion("Past Performance Match Scoring", icon="history_edu", value=False).classes("w-full"):
        with ui.row().classes("w-full justify-end"):
            ui.button(
                "Refresh",
                icon="refresh",
                on_click=lambda: _run("Past performance matches", generate_past_performance_matches, proposal_id, on_change=on_change),
            ).props("flat dense")
        rows = []
        for match in (matches or {}).get("matches") or []:
            rows.append(
                {
                    "project": match.get("project"),
                    "customer": match.get("customer"),
                    "fit": f"{match.get('fit')} ({match.get('fit_score')})",
                    "citable": "yes" if match.get("citable") else "no",
                    "terms": ", ".join(match.get("matched_terms") or []),
                    "use": match.get("recommended_use"),
                }
            )
        if not rows:
            _empty_state("No past-performance matches yet.", icon="history_edu")
            return
        ui.table(
            columns=[
                {"name": "project", "label": "Project", "field": "project", "align": "left"},
                {"name": "customer", "label": "Customer", "field": "customer", "align": "left"},
                {"name": "fit", "label": "Fit", "field": "fit"},
                {"name": "citable", "label": "Citable", "field": "citable"},
                {"name": "terms", "label": "Matched Terms", "field": "terms", "align": "left"},
                {"name": "use", "label": "Recommended Use", "field": "use", "align": "left"},
            ],
            rows=rows,
            row_key="project",
        ).classes("w-full").props("dense flat wrap-cells")


def _render_price_to_win(price: dict | None, proposal_id: int, *, on_change=None) -> None:
    with ui.expansion("Price-To-Win Layer", icon="price_check", value=False).classes("w-full"):
        with ui.row().classes("w-full justify-end"):
            ui.button(
                "Refresh",
                icon="refresh",
                on_click=lambda: _run("Price-to-win", generate_price_to_win, proposal_id, on_change=on_change),
            ).props("flat dense")
        if not price:
            _empty_state("No price-to-win posture yet.", icon="price_check")
            return
        with ui.row().classes("gap-2 flex-wrap"):
            ui.chip(f"Status: {price.get('status')}").props("color=grey-3 text-color=black")
            ui.chip(f"Method: {price.get('source_selection_method')}").props("color=blue-2 text-color=black")
            ui.chip(f"Posture: {price.get('posture') or 'pending'}").props("color=amber-2 text-color=black")
            ui.chip(f"Recommended: {price.get('recommended_scenario') or 'TBD'}").props("color=green-2 text-color=black")
        ui.label(price.get("rationale") or "").classes("text-sm pt-1")
        rows = [
            {
                "scenario": s.get("scenario"),
                "price": _money(s.get("total_proposed_price")),
                "market": s.get("vs_market_position") or "unknown",
                "recommendation": s.get("bid_recommendation") or "unknown",
                "role": s.get("narrative_role") or "comparison",
            }
            for s in price.get("scenarios") or []
        ]
        if rows:
            ui.table(
                columns=[
                    {"name": "scenario", "label": "Scenario", "field": "scenario"},
                    {"name": "price", "label": "Price", "field": "price"},
                    {"name": "market", "label": "Market", "field": "market"},
                    {"name": "recommendation", "label": "Bid Rec", "field": "recommendation"},
                    {"name": "role", "label": "Narrative Role", "field": "role"},
                ],
                rows=rows,
                row_key="scenario",
            ).classes("w-full").props("dense flat")
        if price.get("risks"):
            ui.label("Risks").classes("text-sm font-medium pt-2")
            for risk in price["risks"]:
                ui.label(f"- {risk}").classes("text-sm")


def _render_red_team(red_team: dict | None, proposal_id: int, *, on_change=None) -> None:
    with ui.expansion("Red Team Downgrade Review", icon="report", value=False).classes("w-full"):
        with ui.row().classes("w-full justify-end"):
            ui.button(
                "Refresh",
                icon="refresh",
                on_click=lambda: _run("Red team", generate_red_team_findings, proposal_id, on_change=on_change),
            ).props("flat dense")
        if not red_team:
            _empty_state("No red-team review yet.", icon="report")
            return
        summary = red_team.get("summary") or {}
        with ui.row().classes("gap-2 flex-wrap"):
            ui.chip(f"Critical: {summary.get('critical', 0)}").props("color=red-3 text-color=black")
            ui.chip(f"Major: {summary.get('major', 0)}").props("color=orange-3 text-color=black")
            ui.chip(f"Minor: {summary.get('minor', 0)}").props("color=blue-grey-2 text-color=black")
        rows = [
            {
                "severity": f.get("severity"),
                "area": f.get("area"),
                "section": f.get("section_id") or "",
                "finding": f.get("finding"),
                "fix": f.get("suggested_fix"),
            }
            for f in red_team.get("findings") or []
        ]
        if rows:
            ui.table(
                columns=[
                    {"name": "severity", "label": "Severity", "field": "severity"},
                    {"name": "area", "label": "Area", "field": "area"},
                    {"name": "section", "label": "Section", "field": "section"},
                    {"name": "finding", "label": "Finding", "field": "finding", "align": "left"},
                    {"name": "fix", "label": "Suggested Fix", "field": "fix", "align": "left"},
                ],
                rows=rows,
                row_key="finding",
            ).classes("w-full").props("dense flat wrap-cells")
        else:
            ui.label("No downgrade risks found by deterministic red-team checks.").classes("text-sm opacity-70")


def _render_graphics(graphics: dict | None, proposal_id: int, *, on_change=None) -> None:
    with ui.expansion("Graphics / Tables Generator", icon="table_chart", value=False).classes("w-full"):
        with ui.row().classes("w-full justify-end"):
            ui.button(
                "Refresh",
                icon="refresh",
                on_click=lambda: _run("Graphics/tables", generate_graphics_tables, proposal_id, on_change=on_change),
            ).props("flat dense")
        artifacts = (graphics or {}).get("artifacts") or []
        if not artifacts:
            _empty_state("No graphics or table specs yet.", icon="table_chart")
            return
        for artifact in artifacts:
            with ui.expansion(
                f"{artifact.get('id')}  {artifact.get('title')}",
                icon="table_rows",
                value=False,
            ).classes("w-full"):
                ui.label(f"Placement: {artifact.get('recommended_placement')}").classes("text-xs opacity-70")
                rows = artifact.get("rows") or []
                columns = [
                    {"name": c, "label": c, "field": c, "align": "left"}
                    for c in artifact.get("columns") or []
                ]
                if rows and columns:
                    ui.table(columns=columns, rows=rows[:25], row_key=columns[0]["name"]).classes("w-full").props("dense flat wrap-cells")
                    if len(rows) > 25:
                        ui.label(f"{len(rows) - 25} additional row(s) not shown in preview.").classes("text-xs opacity-60")
                else:
                    ui.label("No rows available yet.").classes("text-sm opacity-60")


def _render_win_strategy_tab(proposal_id: int, *, on_state_change=None) -> None:
    strategy = load_win_strategy(proposal_id)
    _render_header(proposal_id, on_change=on_state_change)
    _render_scorecard(strategy.get("evaluator_scorecard"), proposal_id, on_change=on_state_change)
    _render_themes(strategy.get("win_themes"), proposal_id, on_change=on_state_change)
    _render_past_performance(strategy.get("past_performance_matches"), proposal_id, on_change=on_state_change)
    _render_price_to_win(strategy.get("price_to_win"), proposal_id, on_change=on_state_change)
    _render_red_team(strategy.get("red_team_findings"), proposal_id, on_change=on_state_change)
    _render_graphics(strategy.get("graphics_tables"), proposal_id, on_change=on_state_change)
