"""Proposal Review > Cost tab.

Surfaces the three-agent cost pipeline (Market Researcher → Cost
Analyst → Cost Volume Writer) in one place. Branches on service_line:

  - it_services (default): scenario-based labor build (LOW / MEDIUM /
    HIGH / CUSTOM), each with its own bid posture, margin, and
    contingency. Pipeline status chips for the four agents +
    Run buttons for each.
  - payment_systems: payment-market scan + cost-basis editor +
    fee-narrative cost writer. Pipeline status chips + per-stage
    Run buttons.

Includes the helpers that drive the Run buttons on this tab:
  _render_cost_pipeline_status / _render_pipeline_chip /
  _render_run_market_research_button / _render_run_cost_analyst_button /
  _render_run_cost_writer_button / _render_run_cost_reviewer_button
plus _load_cost_deferred_sections (DB snapshot used by the header).
"""

from __future__ import annotations

import json
import logging

from nicegui import ui
from sqlalchemy import select

from app.db.session import session_scope
from app.jobs.cost_analyst import spawn_cost_analyst
from app.jobs.cost_reviewer import spawn_cost_reviewer
from app.jobs.cost_writer import spawn_cost_writer
from app.jobs.market_researcher import spawn_market_research
from app.jobs.payment_market_researcher import spawn_payment_market_research
from app.models import Proposal, ProposalSection
from app.services.service_line import (
    SERVICE_LINE_PAYMENT_SYSTEMS,
    get_service_line,
)
from app.ui._shared import _empty_state

log = logging.getLogger(__name__)


# Per-tab references to large render helpers and dialogs that still
# live in pages.py. Resolved lazily on first call so we don't hit a
# circular import at module-load time (pages.py imports this module
# at the top of its imports block).
def _pages_helper(name: str):
    from app.ui import pages

    return getattr(pages, name)


def _render_payment_market_scan_section(*args, **kwargs):
    return _pages_helper("_render_payment_market_scan_section")(*args, **kwargs)


def _render_cost_section_drafts(*args, **kwargs):
    return _pages_helper("_render_cost_section_drafts")(*args, **kwargs)


def _render_cost_market_scan_section(*args, **kwargs):
    return _pages_helper("_render_cost_market_scan_section")(*args, **kwargs)


def _render_cost_pricing_headline(*args, **kwargs):
    return _pages_helper("_render_cost_pricing_headline")(*args, **kwargs)


def _open_edit_cost_basis_dialog(*args, **kwargs):
    return _pages_helper("_open_edit_cost_basis_dialog")(*args, **kwargs)


# Scenario → display config (label, color, badge color). Mirrors the
# scenario_definitions in data/internal_pricing_rules.json.
_COST_SCENARIO_VISUAL = {
    "LOW": {
        "label": "Low — Competitive",
        "subtitle": "Low coverage · 18% margin · 0% contingency",
        "accent": "blue",
        "card_bg": "bg-blue-50",
        "border": "border-blue-300",
    },
    "MEDIUM": {
        "label": "Medium — Target",
        "subtitle": "High coverage · 25% margin · 5% contingency",
        "accent": "primary",
        "card_bg": "bg-emerald-50",
        "border": "border-emerald-400",
    },
    "HIGH": {
        "label": "High — Protective",
        "subtitle": "High coverage · 30% margin · 10% contingency",
        "accent": "amber",
        "card_bg": "bg-amber-50",
        "border": "border-amber-300",
    },
    "CUSTOM": {
        "label": "Custom — What-if",
        "subtitle": "Edit labor · edit ODCs · drag bid posture",
        "accent": "violet",
        "card_bg": "bg-violet-50",
        "border": "border-violet-300",
    },
}

_COST_BID_REC_VISUAL = {
    "bid": ("BID", "positive"),
    "walk_away": ("WALK AWAY", "negative"),
    "flag_for_review": ("FLAG FOR REVIEW", "warning"),
}

_COST_POSITION_VISUAL = {
    "below": ("BELOW MARKET", "blue-grey-7"),
    "in_band": ("IN BAND", "green-7"),
    "above": ("ABOVE MARKET", "deep-orange-7"),
}


def _render_cost_tab(proposal_id: int) -> None:
    """Cost tab — surfaces the three-agent cost pipeline (Market
    Researcher → Cost Analyst → Cost Volume Writer) in one place.

    Layout branches by service_line:

    it_services (default):
      1. Pipeline status + action buttons (Market Research, Cost
         Analyst, Cost Writer, Cost Reviewer)
      2. Market Scan summary (band, comparable awards, competitors)
      3. Proposed Price (3 selectable LOW/MEDIUM/HIGH cards + the
         selected scenario's detail rendered directly below; CUSTOM
         slider for free-form what-if exploration)
      4. Cost-deferred section drafts (status + preview)

    payment_systems:
      1. Pipeline status (Cost Analyst + Cost Reviewer chips greyed
         since they don't run for this service line)
      2. Info banner + action buttons (Run Payment Market Research,
         Run Cost Writer, Edit Cost Basis) — labor controls hidden
      3. Payment Market Scan card (pricing recommendation with
         median-vs-proposed comparison, processed-volume estimate,
         profit math, comparable processor awards table, competitor
         processors table — populated from
         proposals.payment_market_scan_json once the dual-pipeline
         scan has run)
      4. Cost-deferred section drafts (status + preview)

    Refreshable so action buttons re-render after kicking off a
    background job. Most heavy lifting lives in helpers below to
    keep this function readable.
    """
    from app.services.market_scan import get_market_scan_snapshot

    # Tab-level state — survives `render.refresh()` calls because
    # it's bound in the outer closure. Tracks which scenario card
    # the user clicked so the detail panel shows that scenario.
    # Seed from the persisted proposal.proposed_scenario so the
    # selection survives across reloads / app restarts.
    from app.services.pricing import get_pricing_packages_snapshot, get_proposed_scenario

    tab_state = {"selected_scenario": get_proposed_scenario(proposal_id)}

    @ui.refreshable
    def render() -> None:
        from app.services.cost_reviewer import (
            get_cost_review_findings_snapshot,
        )

        scan = get_market_scan_snapshot(proposal_id)
        packages = get_pricing_packages_snapshot(proposal_id)
        cost_sections = _load_cost_deferred_sections(proposal_id)
        # Pipeline-status chip only — full findings list lives on
        # the Cost Review tab.
        review_finding_rows = get_cost_review_findings_snapshot(
            proposal_id,
        )

        has_scan = scan is not None
        has_packages = len(packages) > 0
        has_drafts = any(s["has_draft"] for s in cost_sections)
        has_review = len(review_finding_rows) > 0

        _render_cost_pipeline_status(
            proposal_id,
            has_scan=has_scan,
            has_packages=has_packages,
            has_drafts=has_drafts,
            has_review=has_review,
            n_cost_sections=len(cost_sections),
            on_refresh=render.refresh,
        )

        # Service-line branch: payment_systems uses a different scan
        # source (proposals.payment_market_scan_json) and skips the
        # labor-flow proposed-price scenarios entirely.
        if get_service_line(proposal_id) == SERVICE_LINE_PAYMENT_SYSTEMS:
            payment_scan_data: dict = {}
            with session_scope() as db:
                p = db.get(Proposal, proposal_id)
                raw_scan = p.payment_market_scan_json if p else None
            if raw_scan:
                try:
                    payment_scan_data = json.loads(raw_scan)
                except json.JSONDecodeError:
                    log.warning(
                        "payment_market_scan_json invalid JSON on proposal %d",
                        proposal_id,
                    )

            if not payment_scan_data and not cost_sections:
                _empty_state(
                    "Payment Market Research hasn't run yet. Click "
                    "'Run Payment Market Research' above to start — "
                    "Gemini + Claude will research typical pricing for "
                    "this procurement, find comparable processor "
                    "awards, estimate annual volume, and recommend a "
                    "rate posture. Cost Volume Writer follows once "
                    "the scan finishes.",
                    icon="payments",
                )
                return

            if payment_scan_data:
                _render_payment_market_scan_section(
                    proposal_id,
                    payment_scan_data,
                )

            if cost_sections:
                _render_cost_section_drafts(proposal_id, cost_sections)
            return

        # First-run empty state.
        if not has_scan and not has_packages:
            _empty_state(
                "Cost analysis hasn't started yet. Run Market Research "
                "above to begin — Gemini will scan comparable federal "
                "awards and identify likely competitors. Cost Analyst "
                "and Cost Volume Writer follow once the scan is done.",
                icon="payments",
            )
            return

        # Market Scan first — it's the upstream context (band,
        # competitors, comparable awards) that the user reads before
        # interpreting the proposed price. Pricing makes more sense
        # once the market is visible.
        if has_scan:
            _render_cost_market_scan_section(scan, packages)

        if has_packages:
            # Proposed Price section now hosts the full scenario
            # detail (LOW/MEDIUM/HIGH read-only, CUSTOM editable).
            # No separate Cost Build Detail section needed below.
            _render_cost_pricing_headline(
                packages,
                proposal_id=proposal_id,
                tab_state=tab_state,
                on_select=render.refresh,
            )

        if cost_sections:
            _render_cost_section_drafts(proposal_id, cost_sections)

    render()


def _load_cost_deferred_sections(proposal_id: int) -> list[dict]:
    """Pull cost-deferred sections + draft status. Returns plain dicts
    so the UI can render after the session closes."""
    with session_scope() as db:
        rows = db.execute(
            select(
                ProposalSection.id,
                ProposalSection.section_id,
                ProposalSection.section_title,
                ProposalSection.section_order,
                ProposalSection.section_brief,
                ProposalSection.draft_text_markdown,
                ProposalSection.citations_json,
                ProposalSection.needs_human_placeholders_json,
                ProposalSection.excluded_from_draft,
                ProposalSection.current_revision_number,
                ProposalSection.updated_at,
            )
            .where(
                ProposalSection.proposal_id == proposal_id,
                ProposalSection.requires_cost_analysis == True,  # noqa: E712
            )
            .order_by(ProposalSection.section_order)
        ).all()
        return [
            {
                "pk": pk,
                "section_id": sid,
                "section_title": title,
                "section_order": order,
                "section_brief": brief or "",
                "draft_text_markdown": draft or "",
                "has_draft": bool(draft and draft.strip()),
                "citations": list(cits or []),
                "needs_human_placeholders": list(nh or []),
                "excluded_from_draft": bool(excluded),
                "revision": revision or 0,
                "updated_at": updated_at,
            }
            for (
                pk,
                sid,
                title,
                order,
                brief,
                draft,
                cits,
                nh,
                excluded,
                revision,
                updated_at,
            ) in rows
        ]


def _render_cost_pipeline_status(
    proposal_id: int,
    *,
    has_scan: bool,
    has_packages: bool,
    has_drafts: bool,
    has_review: bool,
    n_cost_sections: int,
    on_refresh,
) -> None:
    """Sticky-feeling status header. Pipeline-stage chips + action
    buttons that adapt to what's been run. Branches by service_line:
    payment_systems shows a 2-stage pipeline (Payment Market Research
    + Cost Volume Writer) since Cost Analyst / Cost Reviewer don't
    apply when there's no labor build."""
    is_payment_systems = get_service_line(proposal_id) == SERVICE_LINE_PAYMENT_SYSTEMS
    if is_payment_systems:
        # has_scan + has_payment_review for payment_systems both come
        # from columns on the proposal — the labor-flow MarketScan +
        # cost_review_findings tables stay empty for these proposals.
        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            has_payment_scan = bool(p and (p.payment_market_scan_json or "").strip())
            has_payment_review = bool(p and (p.payment_cost_review_findings_json or "").strip())

    with ui.card().classes("w-full"):
        ui.label("Cost pipeline").classes("text-base font-medium")
        if is_payment_systems:
            ui.label(
                "Three stages: the Payment Market Researcher (Gemini "
                "grounded + Claude+web_search) finds comparable "
                "processor rate disclosures, estimates annual "
                "processed volume, and recommends a competitive bid "
                "posture; the Cost Volume Writer drafts the fee "
                "narrative directly from the recommendation; the "
                "Payment Cost Reviewer (Sonnet adversarial) fact-"
                "checks the drafted narrative against the scan + "
                "compliance posture + brand framing before the "
                "proposal goes to the Writer Team."
            ).classes("text-xs opacity-70")
        else:
            ui.label(
                "Four agents: market research finds comparable "
                "awards and competitor rates; the Cost Analyst "
                "synthesizes those + your internal pricing rules "
                "into H/M/L scenarios; the Cost Volume Writer drafts "
                "narrative for cost-deferred sections; the Cost "
                "Reviewer adversarially fact-checks the build before "
                "submission."
            ).classes("text-xs opacity-70")

        with ui.row().classes("items-center gap-3 pt-2 flex-wrap"):
            if is_payment_systems:
                _render_pipeline_chip(
                    "Payment Market Research",
                    has_payment_scan,
                    "Gemini + Claude dual-pipeline scan",
                )
                ui.icon("east").classes("opacity-50")
                _render_pipeline_chip(
                    "Cost Volume Writer",
                    has_drafts and n_cost_sections > 0,
                    f"Sonnet drafts {n_cost_sections} cost section(s)",
                )
                ui.icon("east").classes("opacity-50")
                _render_pipeline_chip(
                    "Payment Cost Reviewer",
                    has_payment_review,
                    "Sonnet adversarial fact-check",
                )
            else:
                _render_pipeline_chip(
                    "Market Research",
                    has_scan,
                    "Gemini grounded scan",
                )
                ui.icon("east").classes("opacity-50")
                _render_pipeline_chip(
                    "Cost Analyst",
                    has_packages,
                    "GPT-5.5 H/M/L cost build",
                )
                ui.icon("east").classes("opacity-50")
                _render_pipeline_chip(
                    "Cost Volume Writer",
                    has_drafts and n_cost_sections > 0,
                    f"Sonnet drafts {n_cost_sections} cost section(s)",
                )
                ui.icon("east").classes("opacity-50")
                _render_pipeline_chip(
                    "Cost Reviewer",
                    has_review,
                    "Gemini Pro adversarial review",
                )
            ui.element("div").classes("flex-1")
            ui.button(
                "Refresh",
                icon="refresh",
                on_click=on_refresh,
            ).props("flat dense")

        # Action buttons row — buttons appear/relabel based on state
        # so the user always sees the "what's next" action surfaced.
        # Service-line branch: payment_systems skips Cost Analyst +
        # Cost Reviewer entirely (no labor build to make; Cost Writer
        # renders the fee schedule directly from
        # data/pricing/payment_systems.json).
        if is_payment_systems:
            # has_payment_scan was computed at the top of this function
            # for the pipeline chip; reused here for the Run button label.
            with ui.card().classes("w-full bg-blue-50 border-l-4 border-blue-500 mt-2"):
                ui.label(
                    "Payment-systems service line — labor cost-build "
                    "skipped. The Cost Writer will render the proposed "
                    "fee schedule directly from data/pricing/"
                    "payment_systems.json (Quadratic Financial / NAC "
                    "rate sheet + market-rate-driven county-tier offer)."
                ).classes("text-sm text-blue-900")
            with ui.row().classes("items-center gap-2 pt-3 flex-wrap"):
                _render_run_market_research_button(proposal_id, has_payment_scan)
                # has_packages forced True for payment_systems so the
                # Cost Writer button renders without requiring a Cost
                # Analyst run upstream.
                _render_run_cost_writer_button(
                    proposal_id,
                    True,
                    has_drafts,
                    n_cost_sections,
                )
                # Cost Reviewer functions live on the Cost Review tab
                # (the dedicated review surface). The pipeline chip
                # above stays as a status indicator; the action lives
                # next to the findings it produces.
                ui.button(
                    "Edit Cost Basis",
                    icon="edit_note",
                    on_click=lambda: _open_edit_cost_basis_dialog(
                        proposal_id,
                        on_change=on_refresh,
                    ),
                ).props("flat color=primary").tooltip(
                    "Edit our internal cost-of-service inputs "
                    "(sponsor fees, gateway, PCI, support allocation). "
                    "After saving, profit math re-computes on the "
                    "existing scan — no need to re-run the Gemini + "
                    "Claude grounded research."
                )
        else:
            with ui.row().classes("items-center gap-2 pt-3 flex-wrap"):
                _render_run_market_research_button(proposal_id, has_scan)
                _render_run_cost_analyst_button(
                    proposal_id,
                    has_scan,
                    has_packages,
                )
                _render_run_cost_writer_button(
                    proposal_id,
                    has_packages,
                    has_drafts,
                    n_cost_sections,
                )
                _render_run_cost_reviewer_button(
                    proposal_id,
                    has_packages,
                    has_review,
                )


def _render_pipeline_chip(label: str, done: bool, subtitle: str) -> None:
    """One stage in the pipeline status row."""
    if done:
        with ui.row().classes("items-center gap-2 px-3 py-1 rounded bg-emerald-50 border border-emerald-300"):
            ui.icon("check_circle").classes("text-emerald-600")
            with ui.column().classes("gap-0"):
                ui.label(label).classes("text-sm font-medium")
                ui.label(subtitle).classes("text-xs opacity-70")
    else:
        with ui.row().classes("items-center gap-2 px-3 py-1 rounded bg-slate-50 border border-slate-300"):
            ui.icon("radio_button_unchecked").classes("text-slate-400")
            with ui.column().classes("gap-0"):
                ui.label(label).classes("text-sm opacity-70")
                ui.label(subtitle).classes("text-xs opacity-50")


def _render_run_market_research_button(
    proposal_id: int,
    has_scan: bool,
) -> None:
    # Service-line branch: payment_systems gets the payment-processing
    # researcher (Gemini grounded scan for comparable processor awards
    # + processed-volume estimate + profit math), not the labor-flow
    # researcher (which looks for federal IT awards with FTE rates).
    is_payment_systems = get_service_line(proposal_id) == SERVICE_LINE_PAYMENT_SYSTEMS

    if is_payment_systems:
        label = "Re-run Payment Market Research" if has_scan else "Run Payment Market Research"
        tooltip_text = (
            "Gemini 2.5 Pro grounded — researches typical pricing "
            "model for THIS procurement, comparable processor rate "
            "disclosures (Forte / NIC / Tyler / Heartland / Worldpay), "
            "estimated annual processed volume, and recommended rate "
            "posture (match/beat median). Computes revenue × rate − "
            "cost basis = profit projection. ~$0.15-0.30/run."
        )

        def _kick_off() -> None:
            spawn_payment_market_research(proposal_id)
            ui.notify(
                "Payment Market Research started — watch progress on "
                "Run Progress; this tab refreshes when complete.",
                type="positive",
                multi_line=True,
                timeout=6000,
            )
            ui.navigate.to(f"/proposals/{proposal_id}/progress")
    else:
        label = "Re-run Market Research" if has_scan else "Run Market Research"
        tooltip_text = (
            "Gemini 2.5 Pro grounded — finds comparable awards and "
            "likely competitors with rate inferences. ~$0.15-0.30/run."
        )

        def _kick_off() -> None:
            spawn_market_research(proposal_id)
            ui.notify(
                "Market Research started — watch progress on Run Progress; this tab refreshes when complete.",
                type="positive",
                multi_line=True,
                timeout=6000,
            )
            ui.navigate.to(f"/proposals/{proposal_id}/progress")

    btn = ui.button(label, icon="search", on_click=_kick_off)
    btn.props("color=primary" if not has_scan else "outline color=primary")
    btn.tooltip(tooltip_text)


def _render_run_cost_analyst_button(
    proposal_id: int,
    has_scan: bool,
    has_packages: bool,
) -> None:
    label = "Re-run Cost Analyst" if has_packages else "Run Cost Analyst"

    def _kick_off() -> None:
        spawn_cost_analyst(proposal_id)
        ui.notify(
            "Cost Analyst started — H/M/L cost build will appear here when complete.",
            type="positive",
            multi_line=True,
            timeout=6000,
        )
        ui.navigate.to(f"/proposals/{proposal_id}/progress")

    btn = ui.button(label, icon="calculate", on_click=_kick_off)
    if has_packages:
        btn.props("outline color=primary")
    else:
        btn.props("color=primary" if has_scan else "color=grey-5")
    if not has_scan:
        btn.props("disable").tooltip(
            "Run Market Research first — the analyst needs the market band for vs-market positioning."
        )
    else:
        btn.tooltip(
            "GPT-5.5 — synthesizes labor estimate. Math runs deterministically in Python. ~$0.10-0.30/run."
        )


def _render_run_cost_writer_button(
    proposal_id: int,
    has_packages: bool,
    has_drafts: bool,
    n_cost_sections: int,
) -> None:
    # When the Cost Reviewer has run AND the user has accepted
    # findings, surface the count in the button label so the next
    # action ("apply these fixes by re-drafting") is obvious. Only
    # applies to payment_systems — the labor flow handles fix
    # application via the Strategy Implementer, not the Cost Writer.
    n_accepted = 0
    if has_drafts and (get_service_line(proposal_id) == SERVICE_LINE_PAYMENT_SYSTEMS):
        try:
            from app.services.payment_cost_review import (
                count_accepted_payment_findings,
            )

            n_accepted = count_accepted_payment_findings(proposal_id)
        except Exception:
            log.exception(
                "failed to count accepted payment findings for proposal %d",
                proposal_id,
            )

    if n_accepted > 0:
        label = f"Re-run Cost Writer (apply {n_accepted} accepted fix{'es' if n_accepted != 1 else ''})"
    elif has_drafts:
        label = "Re-run Cost Volume Writer"
    else:
        label = "Run Cost Volume Writer"

    def _kick_off() -> None:
        spawn_cost_writer(proposal_id)
        if n_accepted > 0:
            ui.notify(
                f"Cost Volume Writer started — re-drafting "
                f"{n_cost_sections} cost-deferred section(s) with "
                f"{n_accepted} accepted reviewer fix(es) applied. "
                f"Watch Run Progress.",
                type="positive",
                multi_line=True,
                timeout=6000,
            )
        else:
            ui.notify(
                f"Cost Volume Writer started — drafting {n_cost_sections} cost-deferred section(s).",
                type="positive",
                multi_line=True,
                timeout=6000,
            )
        ui.navigate.to(f"/proposals/{proposal_id}/progress")

    btn = ui.button(label, icon="article", on_click=_kick_off)
    if n_accepted > 0:
        # Highlight the apply-fixes path: this is the recommended
        # next action when the user has accepted Cost Reviewer
        # findings.
        btn.props("color=positive unelevated")
    elif has_drafts:
        btn.props("outline color=primary")
    else:
        btn.props("color=primary" if has_packages else "color=grey-5")
    if not has_packages:
        btn.props("disable").tooltip(
            "Run Cost Analyst first — the writer needs the cost "
            "build to pin every dollar value in the narrative."
        )
    elif n_cost_sections == 0:
        btn.props("disable").tooltip(
            "No cost-deferred sections in the outline. Mark a "
            "section requires_cost_analysis=True on the Outline "
            "tab to enable."
        )
    elif n_accepted > 0:
        btn.tooltip(
            f"Re-drafts {n_cost_sections} cost-deferred section(s) "
            f"with the {n_accepted} accepted reviewer finding(s) "
            f"injected as ACCEPTED REVIEWER DIRECTIVES at the top "
            f"of the writer's cached prefix. Each accepted fix gets "
            f"applied to its cited section verbatim (CRITICAL / "
            f"MAJOR) or paraphrased to fit narrative (MINOR)."
        )
    else:
        btn.tooltip(
            f"Sonnet 4.6 — drafts {n_cost_sections} cost-deferred "
            f"section(s) with citations to the structured cost "
            f"build. Cached prefix shared across sections."
        )


def _render_run_cost_reviewer_button(
    proposal_id: int,
    has_packages: bool,
    has_review: bool,
) -> None:
    """Action button for the Cost Reviewer stage. Disabled with a
    tooltip when prerequisites aren't met."""
    label = "Re-run Cost Reviewer" if has_review else "Run Cost Reviewer"

    def _kick_off() -> None:
        spawn_cost_reviewer(proposal_id)
        ui.notify(
            "Cost Reviewer started — adversarial findings will appear here when complete.",
            type="positive",
            multi_line=True,
            timeout=6000,
        )
        ui.navigate.to(f"/proposals/{proposal_id}/progress")

    btn = ui.button(label, icon="fact_check", on_click=_kick_off)
    if has_review:
        btn.props("outline color=primary")
    else:
        btn.props("color=primary" if has_packages else "color=grey-5")
    if not has_packages:
        btn.props("disable").tooltip(
            "Run Cost Analyst first — the reviewer needs the persisted cost build to fact-check."
        )
    else:
        btn.tooltip(
            "Gemini Pro adversarial pass on the cost build. Looks "
            "for missed scope, unrealistic hours, margin pressure "
            "vs market, ceiling violations, phase gaps, and ODC "
            "issues. ~$0.05-0.20/run."
        )
