"""Cost Market Researcher (Claude + web_search) — Pass B of the
dual cost-market pipeline. Mirrors the Gemini-grounded version
(`market_researcher.py`) but uses Anthropic's `web_search_20250305`
tool so the search backend is genuinely different (Brave under
Anthropic vs Google under Gemini), giving cross-provider coverage on
comparable awards and competitor identification.

Pipeline per scan:
  1. Grounded research (Sonnet 4.6 + web_search) — single comprehensive
     call. Claude issues web searches on demand to find federal awards
     + competitors. Citations captured from inline annotations.
  2. Structuring (Haiku, forced tool call) — same downstream schema as
     the Gemini path so the consolidator can union the two outputs by
     award title and competitor name without per-provider branching.

Cost: ~$0.20 per scan (vs Gemini's ~$0.04). Web_search input-token
cost dominates because search results are returned in the next-turn
context. max_uses=2 (matching the teaming-researcher tuning) keeps
the input bill bounded — two searches is enough breadth for a
single-shot market scan.

Returns the same `MarketScanResult` dataclass the Gemini path returns,
so the orchestrator can swap or fan out without per-provider code.
"""

from __future__ import annotations

import logging

from app.agents.market_researcher import (
    _RESEARCH_SYSTEM,
    _RESEARCH_USER_TEMPLATE,
    _STRUCTURE_SYSTEM,
    _STRUCTURE_USER_TEMPLATE,
    _TOOL,
    ComparableAward,
    Competitor,
    MarketResearchInputs,
    MarketScanResult,
    _str_or_none,
    _to_float_or_none,
    _to_int_or_none,
)
from app.config import get_settings
from app.services.llm import call_tool_for_model, get_anthropic

log = logging.getLogger(__name__)


def research_market_claude(
    *,
    proposal_id: int,
    inputs: MarketResearchInputs,
) -> MarketScanResult:
    """Claude+web_search variant of the Cost Market Researcher. Same
    I/O contract as `app.agents.market_researcher.research_market` so
    the orchestrator can fan both providers in parallel and feed both
    results into the market consolidator without per-provider branching.

    Caller MUST handle exceptions — single-provider failure at the
    orchestrator level should still let the other provider's output
    through (graceful degradation).
    """
    settings = get_settings()
    anthropic = get_anthropic()

    research_user = _RESEARCH_USER_TEMPLATE.format(
        rfp_title=inputs.rfp_title or "(untitled)",
        rfp_agency=inputs.rfp_agency or "(agency unknown)",
        naics=inputs.naics or "(NAICS unknown)",
        pop_months=inputs.pop_months,
        est_fte=inputs.est_fte,
        est_value_low_usd=inputs.est_value_low_usd,
        est_value_high_usd=inputs.est_value_high_usd,
        scope_summary=inputs.scope_summary or "(no scope summary available)",
        quadratic_summary=(inputs.quadratic_summary or "(no Quadratic summary provided)"),
    )

    # ---- Step 1: web_search-grounded research --------------------------
    # max_uses=2 keeps the Sonnet input-token bill bounded — search
    # result text comes back in the next-turn context, so a high cap
    # balloons cost. Two well-targeted searches is enough to identify
    # 5-10 comparable awards + 5-7 competitor firms; more searches paid
    # for redundant context on the teaming pipeline at the same setting.
    brief, citations, _research_usage = anthropic.complete_with_web_search(
        model=settings.model_market_researcher_b,
        system=_RESEARCH_SYSTEM,
        user_prompt=research_user,
        max_tokens=10000,
        agent_name="market_researcher_b_grounded",
        proposal_id=proposal_id,
        max_uses=2,
    )

    if not brief.strip():
        raise RuntimeError(
            "market_researcher_b: Claude+web_search returned empty "
            "brief. Possible safety filter, search backend hiccup, or "
            "rate limit. Check the agent_runs row."
        )

    log.info(
        "market_researcher_b: grounded brief = %d chars, %d citations",
        len(brief),
        len(citations),
    )

    # ---- Step 2: structuring (same Haiku tool the Gemini path uses) ---
    citations_block = (
        "\n".join(f"  - {c.get('title', '(untitled)')} — {c.get('uri', '(no url)')}" for c in citations)
        if citations
        else "(no citations attached)"
    )

    structure_user = _STRUCTURE_USER_TEMPLATE.format(
        brief=brief,
        citations=citations_block,
    )

    tool_input, struct_usage = call_tool_for_model(
        model=settings.model_light_extraction,
        system=_STRUCTURE_SYSTEM,
        messages=[{"role": "user", "content": structure_user}],
        tool=_TOOL,
        max_tokens=8000,
        agent_name="market_researcher_b_structure",
        proposal_id=proposal_id,
    )

    if struct_usage.get("stop_reason") == "max_tokens":
        n_partial_awards = len(tool_input.get("comparable_awards") or [])
        n_partial_competitors = len(tool_input.get("competitors") or [])
        raise RuntimeError(
            f"market_researcher_b: structuring output truncated at "
            f"max_tokens (in={struct_usage['input_tokens']}, "
            f"out={struct_usage['output_tokens']}). Got {n_partial_awards} "
            f"partial award(s) + {n_partial_competitors} partial "
            f"competitor(s). Bump max_tokens or split the brief."
        )

    awards: list[ComparableAward] = []
    for a in tool_input.get("comparable_awards", []) or []:
        url = str(a.get("source_url") or "").strip()
        if not url:
            log.warning(
                "market_researcher_b: dropping comparable award with no source_url: %r",
                a.get("award_title"),
            )
            continue
        awards.append(
            ComparableAward(
                award_title=str(a.get("award_title") or "(untitled)"),
                award_value_usd=_to_float_or_none(a.get("award_value_usd")),
                period_of_performance_months=_to_int_or_none(
                    a.get("period_of_performance_months"),
                ),
                awardee_name=_str_or_none(a.get("awardee_name")),
                customer_agency=_str_or_none(a.get("customer_agency")),
                source_url=url,
                relevance_score=_to_float_or_none(a.get("relevance_score")),
                notes=str(a.get("notes") or ""),
            )
        )

    competitors: list[Competitor] = []
    for c in tool_input.get("competitors", []) or []:
        urls = [str(u) for u in (c.get("source_urls") or []) if u]
        if not urls:
            log.warning(
                "market_researcher_b: dropping competitor with no source_urls: %r",
                c.get("name"),
            )
            continue
        competitors.append(
            Competitor(
                name=str(c.get("name") or "(unnamed)"),
                likelihood_to_bid=str(c.get("likelihood_to_bid") or "low").lower(),
                estimated_rate_low_usd=_to_float_or_none(
                    c.get("estimated_rate_low_usd"),
                ),
                estimated_rate_high_usd=_to_float_or_none(
                    c.get("estimated_rate_high_usd"),
                ),
                rate_estimation_basis=str(
                    c.get("rate_estimation_basis") or "",
                ),
                source_urls=urls,
                notes=str(c.get("notes") or ""),
            )
        )

    insufficient = _str_or_none(tool_input.get("insufficient_data_warning"))
    if insufficient is None and len(awards) < 3:
        insufficient = (
            f"Only {len(awards)} comparable award(s) found — market "
            f"band is weakly grounded. Treat band as a rough estimate, "
            f"not authoritative."
        )

    result = MarketScanResult(
        market_band_low_usd=_to_float_or_none(
            tool_input.get("market_band_low_usd"),
        ),
        market_band_mid_usd=_to_float_or_none(
            tool_input.get("market_band_mid_usd"),
        ),
        market_band_high_usd=_to_float_or_none(
            tool_input.get("market_band_high_usd"),
        ),
        methodology=str(tool_input.get("methodology") or ""),
        comparable_awards=awards,
        competitors=competitors,
        insufficient_data_warning=insufficient,
    )

    log.info(
        "market_researcher_b: proposal %d — band=$%s/$%s/$%s, %d awards, %d competitors, insufficient=%s",
        proposal_id,
        result.market_band_low_usd,
        result.market_band_mid_usd,
        result.market_band_high_usd,
        len(awards),
        len(competitors),
        bool(insufficient),
    )
    return result


__all__ = [
    "research_market_claude",
]
