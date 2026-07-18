"""Team Composer orchestration.

Snapshots the proposal context (RFP info, outline + briefs,
compliance items, labor catalog from internal_pricing_rules,
optional cost-analyst phase definitions, Quadratic summary), runs
the team_composer agent, and returns the structured proposal for
the Team-tab preview dialog.

No persistence — the user reviews the proposed roles and clicks
'Apply to Roster' to commit via app.services.team.replace_team.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.agents.team_composer import (
    propose_team,
)
from app.core.company_profile import get_company_profile
from app.core.enums import RequirementCategory
from app.db.session import session_scope
from app.models import (
    ComplianceMatrixItem,
    PricingPackage,
    Proposal,
    ProposalSection,
)

log = logging.getLogger(__name__)


def _build_quadratic_summary() -> str:
    """Compact firm summary tailored for team-composition reasoning.
    Mirrors the cost-reviewer/cost-analyst summaries — Quadratic's
    size + value framing matters for sizing the proposed team."""
    profile = get_company_profile()
    bits: list[str] = []
    name = profile.get("legal_name") or profile.get("name") or "Quadratic Digital"
    bits.append(name)
    if size := profile.get("employee_count") or profile.get("size"):
        bits.append(f"Headcount: {size}")
    if focus := profile.get("market_focus") or profile.get("focus"):
        bits.append(f"Focus: {focus}")
    bits.append(
        "Competitive edge: AI-accelerated custom delivery — leaner "
        "team than typical for the scope, compensated by accelerated "
        "delivery velocity. Size the proposed team for that posture."
    )
    return ". ".join(bits)


def _format_compliance_block(proposal_id: int) -> str:
    """Compliance items that drive labor (TECHNICAL / MANAGEMENT /
    PERSONNEL only). Capped at ~5K chars so the agent prompt stays
    tractable."""
    with session_scope() as db:
        rows = db.execute(
            select(
                ComplianceMatrixItem.requirement_id,
                ComplianceMatrixItem.requirement_text,
                ComplianceMatrixItem.requirement_type,
                ComplianceMatrixItem.category,
            )
            .where(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
            .order_by(ComplianceMatrixItem.id)
        ).all()
    relevant = [
        r
        for r in rows
        if r[3]
        in (
            RequirementCategory.TECHNICAL,
            RequirementCategory.MANAGEMENT,
            RequirementCategory.PERSONNEL,
        )
    ]
    if not relevant:
        return "(no labor-driving compliance items)"
    lines: list[str] = []
    total = 0
    for req_id, text, req_type, category in relevant:
        text = (text or "").strip()
        if not text:
            continue
        snippet = text[:400] + ("..." if len(text) > 400 else "")
        line = f"  [{req_id}] type={req_type} category={category}: {snippet}"
        if total + len(line) > 5000:
            lines.append(f"  ... ({len(relevant) - len(lines)} more items truncated for prompt budget)")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _format_outline_block(proposal_id: int) -> str:
    """Section list with briefs so the agent can see what's being
    delivered. Excludes cost-deferred and excluded sections (those
    don't drive staffing decisions for the writer team)."""
    with session_scope() as db:
        rows = db.execute(
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
    if not rows:
        return "(no outline)"
    lines: list[str] = []
    total = 0
    for sid, title, brief, requires_cost, excluded in rows:
        if requires_cost or excluded:
            continue
        brief_text = (brief or "").strip()[:300]
        line = f"  - {sid}: {title}\n      {brief_text or '(no brief)'}"
        if total + len(line) > 4500:
            lines.append(f"  ... ({len(rows) - len(lines)} more sections truncated)")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines) if lines else "(no eligible outline sections)"


def _format_labor_catalog_block() -> str:
    """The GSA OLM categories Quadratic can bid. Sourced from
    internal_pricing_rules.json's labor_catalog (or labor_rate_card
    fallback). Each line: title | min_years | hourly_rate."""
    try:
        from app.services.pricing import get_pricing_rules

        rules = get_pricing_rules() or {}
    except Exception:
        rules = {}
    cats = rules.get("labor_catalog") or []
    # Fall back to the company profile's labor_rate_card categories
    # when the pricing-rules file uses a different shape.
    if not cats:
        cats = (get_company_profile().get("labor_rate_card") or {}).get("categories") or []
    if not cats:
        return "(no labor catalog available)"
    lines: list[str] = []
    for c in cats:
        # internal_pricing_rules.json uses category / min_experience_years
        # / ceiling_hourly_rate_usd; the labor_rate_card fallback in the
        # company profile uses title / min_years / hourly_rate_usd. Read
        # both shapes so this formatter works regardless of source.
        title = (c.get("category") or c.get("title") or "?").strip()
        min_yrs = (
            c.get("min_experience_years") if c.get("min_experience_years") is not None else c.get("min_years")
        )
        rate = c.get("ceiling_hourly_rate_usd") or c.get("hourly_rate_usd") or c.get("ceiling_rate_usd")
        default_band = c.get("default_wage_band") or ""
        bits = [title]
        if min_yrs is not None:
            bits.append(f"min {min_yrs} yrs")
        if rate is not None:
            try:
                bits.append(f"${float(rate):.2f}/hr")
            except (TypeError, ValueError):
                pass
        if default_band:
            bits.append(f"default {default_band}")
        lines.append("  - " + " | ".join(bits))
    return "\n".join(lines)


def _format_phases_block(proposal_id: int) -> str:
    """Lifecycle phases from the cost analyst's phase_breakdown.
    Returns empty string when the cost analyst hasn't run — agent
    leaves phases_active empty and the user fills in later."""
    with session_scope() as db:
        rows = db.execute(
            select(PricingPackage.phase_breakdown_json)
            .where(PricingPackage.proposal_id == proposal_id)
            .where(PricingPackage.scenario == "MEDIUM")
            .limit(1)
        ).all()
    if not rows or not rows[0][0]:
        return ""
    phase_data = rows[0][0]
    phases = phase_data.get("phases") if isinstance(phase_data, dict) else None
    if not phases:
        return ""
    lines: list[str] = []
    for p in phases:
        name = p.get("name") or p.get("phase_name") or "?"
        duration = p.get("duration_months") or p.get("months")
        bits = [name]
        if duration is not None:
            try:
                bits.append(f"{float(duration):.1f} months")
            except (TypeError, ValueError):
                pass
        lines.append("  - " + " | ".join(bits))
    return "\n".join(lines)


def propose_team_composition(
    proposal_id: int,
) -> dict | None:
    """Sync entry point. Snapshots inputs, runs the agent, returns
    a dict ready for the preview dialog:

        {
          "summary": str,
          "roles": [
            {role_name, labor_category, time_allocation_pct,
             phases_active, bio_summary, rationale},
            ...
          ],
          "n_existing_members": int,  // for the replace warning
        }

    Returns None when the proposal doesn't exist."""
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return None
        rfp_title = (prop.title or "").strip()
        rfp_agency = (prop.agency or "").strip()

    pop_months = 12  # same default as elsewhere
    compliance_block = _format_compliance_block(proposal_id)
    outline_block = _format_outline_block(proposal_id)
    labor_catalog_block = _format_labor_catalog_block()
    phases_block = _format_phases_block(proposal_id)
    quadratic_summary = _build_quadratic_summary()

    result = propose_team(
        proposal_id=proposal_id,
        rfp_title=rfp_title,
        rfp_agency=rfp_agency,
        pop_months=pop_months,
        compliance_block=compliance_block,
        outline_block=outline_block,
        labor_catalog_block=labor_catalog_block,
        quadratic_summary=quadratic_summary,
        phases_block=phases_block,
    )

    # How many existing roster members? Used by the dialog to warn
    # before applying.
    from app.services.team import get_team_members

    n_existing = len(get_team_members(proposal_id))

    return {
        "summary": result.summary,
        "roles": [
            {
                "role_name": r.role_name,
                "labor_category": r.labor_category,
                "time_allocation_pct": r.time_allocation_pct,
                "phases_active": list(r.phases_active),
                "bio_summary": r.bio_summary,
                "rationale": r.rationale,
            }
            for r in result.roles
        ],
        "n_existing_members": n_existing,
    }


__all__ = ["propose_team_composition"]
