"""Cost Analyst orchestration.

Two entry points:
  - run_cost_analyst(proposal_id) — sync; called by background thread
  - spawn_cost_analyst(proposal_id) — daemon thread launcher for the
    "Run Cost Analyst" button on the Cost tab

Reads market scan + proposal + compliance matrix + outline → calls
the Cost Analyst LLM (GPT-5.5) → applies deterministic Python math
to compute H/M/L scenario cost builds → upserts 3 PricingPackage
rows + N PricingPackageLine rows per scenario.

Re-running replaces the existing pricing packages for the proposal
(per the (proposal_id, scenario) unique constraint).
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import select

from app.agents.cost_analyst import analyze_costs
from app.core.company_profile import get_company_profile
from app.core.enums import RequirementCategory
from app.db.session import session_scope
from app.models import (
    ComplianceMatrixItem,
    Proposal,
    ProposalSection,
    RfpPackageDocument,
)
from app.services.market_scan import get_market_scan_snapshot
from app.services.pricing import (
    CostAnalystLaborLine,
    compute_scenario_packages,
    upsert_pricing_packages,
)
from app.services.stages import record_stage as _set_stage
from app.services.team import (
    format_team_roster_for_cost_analyst,
    roster_to_labor_lines,
)

log = logging.getLogger(__name__)


# Same fallback pattern as market_researcher.py — keep a wide,
# non-tight value range so the LLM has scope context without us
# pretending to know the exact ceiling.
_DEFAULT_POP_MONTHS = 12
_FALLBACK_RATE_LOW_USD_PER_HR = 100.0
_FALLBACK_RATE_HIGH_USD_PER_HR = 200.0


def _snapshot_cost_analyst_inputs(proposal_id: int) -> dict | None:
    """Build the bundle of inputs the agent reads. Returns None if the
    proposal doesn't exist."""
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return None

        rfp_title = (prop.title or "").strip()
        rfp_agency = (prop.agency or "").strip()
        naics = (prop.naics or "").strip()

        # Compliance items → scope summary + personnel-item count for
        # FTE proxy.
        rows = db.execute(
            select(
                ComplianceMatrixItem.requirement_text,
                ComplianceMatrixItem.category,
            ).where(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
        ).all()

        scope_chunks: list[str] = []
        scope_total = 0
        n_personnel_items = 0
        for text, category in rows:
            if category == RequirementCategory.PERSONNEL:
                n_personnel_items += 1
            if category in (
                RequirementCategory.TECHNICAL,
                RequirementCategory.MANAGEMENT,
            ):
                t = (text or "").strip()
                if not t:
                    continue
                if scope_total + len(t) + 2 > 3500:
                    break
                scope_chunks.append(t)
                scope_total += len(t) + 2
        scope_summary = "\n\n".join(scope_chunks)

        if not scope_summary:
            doc = db.execute(
                select(RfpPackageDocument.extracted_text_md)
                .where(
                    RfpPackageDocument.rfp_package_id == prop.rfp_package_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            scope_summary = (doc or "")[:3500]

        # Outline section briefs — section_title + section_brief per
        # row, formatted as a compact block. The agent uses these to
        # spot-check labor coverage against scope.
        sections = db.execute(
            select(
                ProposalSection.section_id,
                ProposalSection.section_title,
                ProposalSection.section_brief,
                ProposalSection.requires_cost_analysis,
                ProposalSection.excluded_from_draft,
            )
            .where(
                ProposalSection.proposal_id == proposal_id,
            )
            .order_by(ProposalSection.section_order)
        ).all()

        outline_rows: list[str] = []
        for sec_id, title, brief, requires_cost, excluded in sections:
            tag = ""
            if requires_cost:
                tag = " [cost-deferred]"
            elif excluded:
                tag = " [excluded]"
            brief_str = (brief or "(no brief)").strip()[:300]
            outline_rows.append(f"  - {sec_id}{tag}: {title}\n    {brief_str}")
        outline_briefs = "\n".join(outline_rows) if outline_rows else ""

    # Heuristic estimates for the agent's value-range context.
    est_fte = max(3.0, float(n_personnel_items) / 2.0)
    pop_months = _DEFAULT_POP_MONTHS
    annual_hours_per_fte = 1950
    est_value_low = est_fte * annual_hours_per_fte * _FALLBACK_RATE_LOW_USD_PER_HR * (pop_months / 12.0)
    est_value_high = est_fte * annual_hours_per_fte * _FALLBACK_RATE_HIGH_USD_PER_HR * (pop_months / 12.0)

    quadratic_summary = _build_quadratic_summary()

    return {
        "rfp_title": rfp_title,
        "rfp_agency": rfp_agency,
        "naics": naics,
        "pop_months": pop_months,
        "est_value_low_usd": est_value_low,
        "est_value_high_usd": est_value_high,
        "scope_summary": scope_summary,
        "outline_briefs": outline_briefs,
        "quadratic_summary": quadratic_summary,
    }


def _build_quadratic_summary() -> str:
    """Compact firm summary tailored to cost analysis (skip cert /
    vehicle detail; lean on size + focus + competitive edge)."""
    profile = get_company_profile()
    bits: list[str] = []
    name = profile.get("legal_name") or profile.get("name") or "Quadratic Digital"
    bits.append(name)
    if size := profile.get("employee_count") or profile.get("size"):
        bits.append(f"Current headcount: {size}")
    if focus := profile.get("market_focus") or profile.get("focus"):
        bits.append(f"Focus: {focus}")
    bits.append(
        "Competitive edge: AI-accelerated custom delivery — COTS-like "
        "speed at custom-build flexibility. The labor approach should "
        "reflect this — leaner team than typical for the scope, "
        "compensated by accelerated delivery velocity."
    )
    return ". ".join(bits)


def run_cost_analyst(proposal_id: int) -> None:
    """Sync entry point. Loads inputs (including the persisted market
    scan), runs the LLM, applies math, persists. Catches all
    exceptions and surfaces via stage banner."""
    log.info("cost analyst starting for proposal %d", proposal_id)
    try:
        _set_stage(
            proposal_id,
            "Cost Analyst: building inputs (scope + market scan + pricing rules)…",
        )
        inputs = _snapshot_cost_analyst_inputs(proposal_id)
        if inputs is None:
            _set_stage(
                proposal_id,
                f"Cost Analyst: proposal {proposal_id} not found.",
            )
            return

        market_scan = get_market_scan_snapshot(proposal_id)
        if market_scan is None:
            _set_stage(
                proposal_id,
                "Cost Analyst: no market scan persisted for this "
                "proposal — run Market Research first (Cost tab → "
                "'Run Market Research'). Cost build needs the band "
                "for vs-market positioning.",
            )
            return

        from app.config import get_settings

        settings = get_settings()

        # Phase 1C: when a team roster has been approved, the user's
        # labor decisions drive the build. The agent gets the roster
        # as authoritative input AND we replace its labor_lines
        # post-call with the deterministic roster-derived version.
        # When no roster is approved, the agent decides labor mix
        # as before.
        pop_months = inputs["pop_months"]
        roster_block = format_team_roster_for_cost_analyst(
            proposal_id,
            pop_months,
        )
        roster_lines = roster_to_labor_lines(proposal_id, pop_months) if roster_block else []
        roster_driven = bool(roster_block and roster_lines)

        if roster_driven:
            _set_stage(
                proposal_id,
                f"Cost Analyst ({settings.model_cost_analyst}): "
                f"using user-approved team roster ({len(roster_lines)} "
                f"role(s)) for labor mix; agent decides phases / "
                f"ODCs / risks…",
            )
        else:
            _set_stage(
                proposal_id,
                f"Cost Analyst ({settings.model_cost_analyst}): "
                f"synthesizing labor estimate from market band $"
                f"{_fmt_band(market_scan, 'market_band_low_usd')}-$"
                f"{_fmt_band(market_scan, 'market_band_high_usd')} "
                f"+ scope (no approved team roster — agent decides "
                f"labor mix)…",
            )

        agent_output = analyze_costs(
            proposal_id=proposal_id,
            rfp_title=inputs["rfp_title"],
            rfp_agency=inputs["rfp_agency"],
            naics=inputs["naics"],
            pop_months=pop_months,
            est_value_low_usd=inputs["est_value_low_usd"],
            est_value_high_usd=inputs["est_value_high_usd"],
            scope_summary=inputs["scope_summary"],
            outline_briefs=inputs["outline_briefs"],
            market_scan_snapshot=market_scan,
            quadratic_summary=inputs["quadratic_summary"],
            team_roster_block=roster_block,
        )

        if roster_driven:
            # Defense in depth — even though the prompt instructs
            # the agent to mirror the roster, we replace its
            # labor_lines with the deterministic ones from the
            # roster so the math layer can never see a hallucinated
            # category or off-by-one hour count. We preserve any
            # rationale text the agent emitted by merging it into
            # the deterministic line when categories match.
            agent_rationales: dict[str, str] = {}
            for ll in agent_output.labor_lines:
                key = (ll.labor_category or "").strip().lower()
                if key and ll.rationale:
                    agent_rationales[key] = ll.rationale
            new_lines: list[CostAnalystLaborLine] = []
            for rl in roster_lines:
                key = (rl["labor_category"] or "").strip().lower()
                rationale = rl["rationale"]
                # Append agent's contribution when present, so the
                # user sees the deterministic explanation + any
                # extra context the agent added.
                extra = agent_rationales.get(key)
                if extra and extra.strip() and extra.strip() not in rationale:
                    rationale = f"{rationale} {extra.strip()}"
                new_lines.append(
                    CostAnalystLaborLine(
                        labor_category=rl["labor_category"],
                        wage_band=rl["wage_band"],
                        hours=float(rl["hours"]),
                        rationale=rationale,
                    )
                )
            n_dropped = len(agent_output.labor_lines) - len(
                set(
                    (ll.labor_category or "").strip().lower() for ll in agent_output.labor_lines
                ).intersection((rl["labor_category"] or "").strip().lower() for rl in roster_lines)
            )
            if n_dropped > 0:
                log.warning(
                    "cost_analyst (roster-driven): replaced agent's "
                    "labor_lines with %d roster-derived line(s); "
                    "agent had emitted %d line(s) (some categories "
                    "did not match the roster — drift suppressed).",
                    len(new_lines),
                    len(agent_output.labor_lines),
                )
            else:
                log.info(
                    "cost_analyst (roster-driven): replaced agent's "
                    "labor_lines with %d roster-derived line(s).",
                    len(new_lines),
                )
            agent_output.labor_lines = new_lines

        _set_stage(
            proposal_id,
            f"Cost Analyst: computing H/M/L scenarios "
            f"({len(agent_output.labor_lines)} labor lines"
            + (" from approved roster" if roster_driven else " from agent judgment")
            + f", {int(agent_output.avg_headcount_during_pop)} avg "
            f"headcount)…",
        )

        packages = compute_scenario_packages(
            output=agent_output,
            market_band_low_usd=market_scan.get("market_band_low_usd"),
            market_band_mid_usd=market_scan.get("market_band_mid_usd"),
            market_band_high_usd=market_scan.get("market_band_high_usd"),
        )

        new_ids = upsert_pricing_packages(
            proposal_id=proposal_id,
            packages=packages,
            market_scan_id=market_scan.get("id"),
            agent_run_id=None,  # cross-link deferred until needed
            executive_summary=agent_output.executive_summary,
        )

        # Phase 2B: advance the pre-draft pipeline. Cost Analyst
        # success at the AWAITING_COST_BUILD gate unlocks "Begin
        # Drafting". Other statuses are left untouched — re-running
        # the Cost Analyst after a draft exists doesn't rewind the
        # pipeline.
        from app.core.enums import ProposalStatus

        with session_scope() as db:
            p = db.get(Proposal, proposal_id)
            if p is not None and p.status == ProposalStatus.AWAITING_COST_BUILD:
                p.status = ProposalStatus.AWAITING_DRAFT
                log.info(
                    "cost_analyst: proposal=%d advanced AWAITING_COST_BUILD -> AWAITING_DRAFT",
                    proposal_id,
                )

        # Stage banner — surface the H/M/L price spread + the bid
        # recommendation so the user knows what to look at.
        prices = {p.scenario: p.total_proposed_price_usd for p in packages}
        recs = {p.scenario: p.bid_recommendation for p in packages}
        _set_stage(
            proposal_id,
            f"Cost Analyst complete: "
            f"LOW ${prices.get('LOW', 0):,.0f} ({recs.get('LOW')}) · "
            f"MEDIUM ${prices.get('MEDIUM', 0):,.0f} "
            f"({recs.get('MEDIUM')}) · "
            f"HIGH ${prices.get('HIGH', 0):,.0f} ({recs.get('HIGH')}). "
            f"Open the Cost tab.",
        )
        log.info(
            "cost_analyst: proposal %d done, package_ids=%s",
            proposal_id,
            new_ids,
        )
    except Exception:
        log.exception(
            "cost analyst failed for proposal %d",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            "Cost Analyst failed — check logs.",
        )


def spawn_cost_analyst(proposal_id: int) -> threading.Thread:
    """Daemon thread launcher for the 'Run Cost Analyst' button."""
    t = threading.Thread(
        target=run_cost_analyst,
        args=(proposal_id,),
        name=f"cost-analyst-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


def _fmt_band(snap: dict, key: str) -> str:
    v = snap.get(key)
    return f"{float(v):,.0f}" if v is not None else "?"


__all__ = [
    "run_cost_analyst",
    "spawn_cost_analyst",
]
