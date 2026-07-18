"""Payment-Systems Market Researcher (Claude + web_search) — Pass B
of the dual payment-market pipeline. Mirrors the Gemini-grounded
version (payment_market_researcher.py) but uses Anthropic's
web_search_20250305 tool so the search backend is genuinely different
(Brave under Anthropic vs Google under Gemini), giving cross-provider
coverage on processor rate disclosures and competitor identification.

Pipeline:
  1. Grounded research (Sonnet 4.6 + web_search) — single comprehensive
     call. Claude issues web searches on demand to find comparable
     processor awards + competitors + volume basis. Citations captured
     from inline annotations.
  2. Structuring (Haiku, forced tool call) — same downstream schema as
     the Gemini path so the consolidator can union the two outputs by
     (processor_name, customer_name) and competitor name without
     per-provider branching.

Cost: ~$0.20 per scan (vs Gemini's ~$0.04). Web_search input-token
cost dominates because search results return in next-turn context.
max_uses=2 (matching the labor-flow tuning) keeps the input bill
bounded — two well-targeted searches is enough breadth for a single-
shot payment market scan.

Returns the same PaymentMarketScanResult dataclass the Gemini path
returns, so the orchestrator can fan both providers without
per-provider code branches.
"""

from __future__ import annotations

import logging

from app.agents.payment_market_researcher import (
    _RESEARCH_SYSTEM,
    _RESEARCH_USER_TEMPLATE,
    _STRUCTURE_SYSTEM,
    _STRUCTURE_USER_TEMPLATE,
    _TOOL,
    PaymentMarketResearchInputs,
    PaymentMarketScanResult,
    _assemble_result,
    _format_fallback_block,
    _format_model_focus_block,
)
from app.config import get_settings
from app.services.llm import call_tool_for_model, get_anthropic

log = logging.getLogger(__name__)


def research_payment_market_claude(
    *,
    proposal_id: int,
    inputs: PaymentMarketResearchInputs,
) -> PaymentMarketScanResult:
    """Claude+web_search variant of the Payment Market Researcher.
    Same I/O contract as research_payment_market so the orchestrator
    can fan both providers in parallel and feed both results into the
    payment-market consolidator without per-provider branching.

    Caller MUST handle exceptions — single-provider failure at the
    orchestrator level should still let the other provider's output
    through (graceful degradation matching the labor flow)."""
    settings = get_settings()
    anthropic = get_anthropic()

    research_user = _RESEARCH_USER_TEMPLATE.format(
        rfp_title=inputs.rfp_title or "(untitled)",
        rfp_agency=inputs.rfp_agency or "(agency unknown)",
        scope_summary=inputs.scope_summary or "(no scope summary available)",
        quadratic_summary=(inputs.quadratic_summary or "(no Quadratic summary provided)"),
        fallback_rate_ranges_block=_format_fallback_block(inputs.fallback_rate_ranges),
        model_focus_block=_format_model_focus_block(inputs.model_focus),
    )

    # ---- Step 1: web_search-grounded research ----------------------------
    # max_uses=2 matches the labor-flow tuning. Search-result text
    # comes back in next-turn context, so a high cap balloons cost.
    # Two well-targeted searches is enough to find 5-10 comparable
    # processor awards + 4-7 competitor processors.
    brief, citations, _research_usage = anthropic.complete_with_web_search(
        model=settings.model_market_researcher_b,
        system=_RESEARCH_SYSTEM,
        user_prompt=research_user,
        max_tokens=10000,
        agent_name="payment_market_researcher_b_grounded",
        proposal_id=proposal_id,
        max_uses=2,
    )

    if not brief.strip():
        raise RuntimeError(
            "payment_market_researcher_b: Claude+web_search returned "
            "empty brief. Possible safety filter, search backend "
            "hiccup, or rate limit. Check the agent_runs row."
        )

    log.info(
        "payment_market_researcher_b: grounded brief = %d chars, %d citations",
        len(brief),
        len(citations),
    )

    # ---- Step 2: structuring (same Haiku tool the Gemini path uses) ------
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
        agent_name="payment_market_researcher_b_structure",
        proposal_id=proposal_id,
    )

    if struct_usage.get("stop_reason") == "max_tokens":
        n_partial_awards = len(tool_input.get("comparable_awards") or [])
        n_partial_competitors = len(tool_input.get("competitor_processors") or [])
        raise RuntimeError(
            f"payment_market_researcher_b: structuring output truncated "
            f"at max_tokens (in={struct_usage['input_tokens']}, "
            f"out={struct_usage['output_tokens']}). Got "
            f"{n_partial_awards} partial award(s) + "
            f"{n_partial_competitors} partial competitor(s). Bump "
            f"max_tokens or split the brief."
        )

    return _assemble_result(tool_input, citations)


__all__ = [
    "research_payment_market_claude",
]
