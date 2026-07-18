"""Teaming Researcher (Claude + web_search) — Pass B of the dual
market-research pipeline. Mirrors the Gemini-grounded version
(`teaming_researcher.py`) but uses Anthropic's `web_search_20250305`
tool so the search backend is genuinely different (Brave under the
hood vs Google), giving cross-provider coverage on partner suggestions.

Pipeline per gap:
  1. Grounded research (Sonnet 4.6 + web_search) — free-form research
     call. Claude issues web searches on demand and answers in prose.
     Citations are captured from inline `web_search_result_location`
     annotations on the text blocks.
  2. Structuring (Haiku, forced tool call) — same downstream schema as
     the Gemini path, so the consolidator can union the two outputs
     trivially.

Cost: ~$0.04 (Sonnet + web_search add-on) + ~$0.005 (Haiku
structuring) ≈ $0.045 per gap, very close to the Gemini path. Budget
roughly doubles vs single-provider research; the doubled spend buys
real cross-verification of partner names that would otherwise go
unflagged when one provider hallucinates.

Returns the same `TeamingPartnerResearch` dataclass the Gemini path
returns, so callers can swap and a single consolidator type-checks
both inputs.
"""

from __future__ import annotations

import logging

from app.agents.teaming_researcher import (
    _RESEARCH_SYSTEM,
    _RESEARCH_USER_TEMPLATE,
    _STRUCTURE_SYSTEM,
    _STRUCTURE_USER_TEMPLATE,
    _TOOL,
    TeamingPartnerResearch,
    _format_citations_block,
)
from app.config import get_settings
from app.services.llm import call_tool_for_model, get_anthropic

log = logging.getLogger(__name__)


def research_partners_for_gap_claude(
    *,
    gap_id: str,
    gap_severity: str,
    requirement_id: str,
    requirement_text: str,
    current_state: str,
    strategist_partner_suggestions: list[dict],
    rfp_title: str,
    rfp_agency: str,
    rfp_scope: str,
    quadratic_summary: str,
    proposal_id: int | None = None,
) -> TeamingPartnerResearch:
    """Claude+web_search variant of the teaming researcher. Same I/O
    contract as `app.agents.teaming_researcher.research_partners_for_gap`
    so the orchestrator can fan out to both in parallel and feed both
    results into the consolidator without per-provider branching.
    """
    settings = get_settings()
    anthropic = get_anthropic()

    if strategist_partner_suggestions:
        existing_block = "\n".join(
            f"- {p.get('name', '?')}: {(p.get('fit_rationale') or '').strip() or '(no rationale)'}"
            for p in strategist_partner_suggestions
        )
    else:
        existing_block = "(Strategist proposed no specific partner names yet.)"

    research_user = _RESEARCH_USER_TEMPLATE.format(
        rfp_title=rfp_title or "(unknown)",
        rfp_agency=rfp_agency or "(unknown)",
        rfp_scope=rfp_scope or "(unknown)",
        quadratic_summary=quadratic_summary or "(unknown)",
        gap_id=gap_id,
        gap_severity=gap_severity or "(unknown)",
        requirement_id=requirement_id or "(unknown)",
        requirement_text=(requirement_text or "(empty)").strip()[:2000],
        current_state=(current_state or "(unknown)").strip()[:1000],
        strategist_suggestions=existing_block,
    )

    # ---- Step 1: web_search-grounded research --------------------------
    # max_uses=2 (was 5): each web_search call returns the result text
    # back in the next turn's input tokens, so a high cap balloons the
    # bill on Sonnet 4.6 input pricing. Two searches is enough to
    # cross-verify a teaming-partner candidate cohort against a
    # different search backend than Gemini's; more searches mostly
    # paid for redundant context.
    brief, citations, research_usage = anthropic.complete_with_web_search(
        model=settings.model_teaming_researcher_b,
        system=_RESEARCH_SYSTEM,
        user_prompt=research_user,
        max_tokens=8000,
        agent_name="teaming_researcher_b_research",
        proposal_id=proposal_id,
        max_uses=2,
    )
    if not brief.strip():
        log.warning(
            "teaming_researcher_b: gap=%s — web_search research returned empty text; skipping structuring",
            gap_id,
        )
        return TeamingPartnerResearch(
            gap_id=gap_id,
            partners=[],
            citations=citations,
            cost_usd=float(research_usage.get("cost_usd") or 0.0),
        )

    # ---- Step 2: structuring (same Haiku tool the Gemini path uses) ---
    structuring_user = _STRUCTURE_USER_TEMPLATE.format(
        brief=brief,
        citations_block=_format_citations_block(citations),
    )
    try:
        tool_input, structure_usage = call_tool_for_model(
            model=settings.model_light_extraction,  # Haiku
            system=_STRUCTURE_SYSTEM,
            messages=[{"role": "user", "content": structuring_user}],
            tool=_TOOL,
            max_tokens=8000,
            agent_name="teaming_researcher_b_structure",
            proposal_id=proposal_id,
        )
    except Exception:
        log.exception(
            "teaming_researcher_b: gap=%s — structuring step failed; returning empty partner list",
            gap_id,
        )
        return TeamingPartnerResearch(
            gap_id=gap_id,
            partners=[],
            citations=citations,
            cost_usd=float(research_usage.get("cost_usd") or 0.0),
        )

    raw_partners = tool_input.get("partners") or []
    total_cost = float(research_usage.get("cost_usd") or 0.0) + float(structure_usage.get("cost_usd") or 0.0)
    log.info(
        "teaming_researcher_b: gap=%s -> %d partners, %d citations, %d searches, total cost=$%.4f",
        gap_id,
        len(raw_partners),
        len(citations),
        research_usage.get("web_searches", 0) or 0,
        total_cost,
    )

    return TeamingPartnerResearch(
        gap_id=gap_id,
        partners=list(raw_partners),
        citations=citations,
        cost_usd=total_cost,
    )


__all__ = [
    "research_partners_for_gap_claude",
]
