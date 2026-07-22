"""Cost Market Researcher — two-step grounded research that produces
the market band, comparable awards, and likely-competitor rate
estimates that feed the Cost Analyst (Agent 2).

Pipeline per proposal:

  1. **Grounded research (Gemini 3.5 Pro + Google Search)** — single
     comprehensive call with `tools=[GoogleSearch()]`. Gemini issues
     web queries on demand to find comparable federal awards, identify
     likely competitors, and infer per-FTE billing rates from public
     award data. Returns a free-form research brief with citations
     (URL + title) attached via grounding_metadata.

  2. **Structuring (Haiku, forced tool call)** — cheap follow-up that
     converts the brief into the `report_market_scan` schema
     (market_band, comparable_awards[], competitors[]). Haiku does no
     market reasoning; it just re-shapes text into structured JSON.

Why two steps: Gemini disallows `tools=[GoogleSearch]` combined with
`tool_config(mode='ANY', allowed_function_names=…)` in the same
request — they're mutually exclusive APIs. Same constraint that drove
the teaming researcher's two-step design.

Sparse-data fallback: If the grounded call can't surface enough
comparable awards (< 3), the agent still produces a result — but with
`insufficient_data_warning` set so the Cost Analyst knows the band
is weakly grounded. Per Jordan: don't block the cost analyst on a
perfect market scan; produce reports with what we have.

Cost: ~$0.10-0.30 per scan (1 Pro grounded + 1 Haiku structuring).
The query_budget setting caps runaway research at 12 grounded calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import get_settings
from app.core.enums import CompetitorBidLikelihood
from app.services.llm import call_tool_for_model, get_gemini

log = logging.getLogger(__name__)


# ---- Output dataclasses ---------------------------------------------------


@dataclass
class ComparableAward:
    """One federal award that informs the market band."""

    award_title: str
    award_value_usd: float | None
    period_of_performance_months: int | None
    awardee_name: str | None
    customer_agency: str | None
    source_url: str
    relevance_score: float | None
    notes: str
    # Dual-pipeline provenance (set by market_consolidator). Empty list
    # / False when this row came from a single-provider run — UI
    # renders no chips in that case.
    confirmed_by: list[str] = field(default_factory=list)
    needs_review: bool = False


@dataclass
class Competitor:
    """One firm likely to bid this RFP, with rate inference."""

    name: str
    likelihood_to_bid: str  # CompetitorBidLikelihood enum value
    estimated_rate_low_usd: float | None
    estimated_rate_high_usd: float | None
    rate_estimation_basis: str
    source_urls: list[str]
    notes: str
    # Dual-pipeline provenance (set by market_consolidator).
    confirmed_by: list[str] = field(default_factory=list)
    needs_review: bool = False


@dataclass
class MarketScanResult:
    """Full output of one market research run. Ready for persistence."""

    market_band_low_usd: float | None
    market_band_mid_usd: float | None
    market_band_high_usd: float | None
    methodology: str
    comparable_awards: list[ComparableAward] = field(default_factory=list)
    competitors: list[Competitor] = field(default_factory=list)
    # Set when fewer than 3 comparable awards were found OR when the
    # band has wide low/high spread (>3x) suggesting weak grounding.
    # Cost Analyst should surface this in the H/M/L scenario commentary.
    insufficient_data_warning: str | None = None


# ---- Step 1 — grounded research ------------------------------------------

_RESEARCH_SYSTEM = """You are a federal-contracts market researcher for Quadratic Digital, a small public-sector software firm. Your job is grounded, current research about real awards and real firms — NOT memory recall.

You have access to Google Search. USE IT LIBERALLY. For this research run, search for:

1. COMPARABLE AWARDS — real federal contracts in the past 3 years similar to the RFP scope. Search:
   - "[agency] [scope keywords] award" on SAM.gov, USAspending.gov, FPDS-NG
   - Recent press releases from the agency announcing similar contract awards
   - GSA Schedule task orders for similar work
   Find 5-10 awards if possible. For each: title, value, period of performance, awardee, customer agency, source URL.

2. LIKELY COMPETITORS — firms LIKELY TO BID AS PRIME for this scope. Search:
   - Past awardees on the same vehicle / NAICS / agency where they delivered as prime
   - Firms with similar past performance the customer would recognize
   - Recent acquisitions, mergers, leadership changes affecting fit
   Identify 5-7 firms that are likely to bid this RFP AS PRIME.

   EXCLUDE these firm types — they don't compete for custom-development primes even if they show up on the same vehicle:
   - Resellers / technology aggregators (Carahsoft, immixGroup, DLT Solutions, GovConnection, etc.) — they sell other people's products on government schedules, they don't deliver custom-build contracts
   - Pure staff-aug firms when the RFP wants a turnkey solution
   - Hardware-only or software-license-only vendors when the scope is services
   If a firm primarily fills one of those roles, do not list them as a competitor — they're not in the same bid pool.

3. PER-COMPETITOR RATE INFERENCE — for each competitor, find a public award where they were prime, and back-derive the per-FTE billing rate:
   blended_rate = award_value ÷ period_of_performance_months ÷ estimated_FTE × 1950
   Show the math in your output. ALWAYS cite the source URL the math came from.

OUTPUT FORMAT — free-form text with these sections (NOT a tool call yet — that's a follow-up):

## Comparable Awards
For each award, list:
- Title: <contract title>
- Value: $<value>
- PoP: <months> months
- Awardee: <firm>
- Customer: <agency>
- Source: <URL — SAM.gov / USAspending / FPDS preferred>
- Relevance: <0.0-1.0 score>
- Why comparable: <1-sentence explanation>

## Market Band
Based on comparable awards, estimate the LOW / MID / HIGH end of the typical contract value for THIS RFP scope. Show your method.
- Low (USD): <value>
- Mid (USD): <value>
- High (USD): <value>
- Methodology: <2-3 sentences on how you derived the band — median? Average? Low/high of comparable values? Adjusted for scope size?>

## Likely Competitors
For each firm, list:
- Name: <firm>
- Likelihood to bid: HIGH | MEDIUM | LOW
- Estimated billing rate: $<low>/hr - $<high>/hr per FTE
- Rate estimation basis: <show your math: $X award ÷ Y months ÷ Z FTE × 1950 = $/hr blended; cite source URL>
- Source URL(s): <list>
- Notes: <1-sentence on why they fit this RFP>

GROUNDING DISCIPLINE:
- Real awards, real firms, real URLs only. NEVER invent contract numbers, award values, or firm names. NEVER invent URLs — if you can't find a source, omit the entry.
- Federal award data lags 6-18 months. Be transparent about recency: "Most recent comparable: 2024-09 award" beats fake-precision 2026 numbers.
- If you find fewer than 3 comparable awards even after thorough search, SAY SO at the top of the output: "## Insufficient Data — only N comparable awards found." The downstream pipeline handles sparse data; better to be honest than fabricate.
- Rate inference is approximate. Always cite the public source the inference was derived from. Wide rate ranges ($120-$200/hr) are honest; tight ranges ($147-$152/hr) require strong sourcing.
- Skip firms you can't verify exist. Real firms only.
- Same RFP family across agencies (e.g., MMIS work for state Medicaid agencies) often has different rate norms than federal-prime work — call this out in methodology.

VALUE-FINDING DISCIPLINE (for comparable awards):
- ATTEMPT to find the contract value for every comparable award. Search the source URL, the awardee's press releases, USAspending.gov totals, FPDS Mod History — values are usually findable for federal awards.
- If you cannot find a value after honest search, you may STILL include the award ONLY IF it adds qualitative grounding the band needs (same scope keyword, same agency, distinctive technical match, set-aside fit). Mark relevance_score ≤ 0.5 for value-less entries.
- DO NOT include value-less awards that just confirm "yes, similar work happens." Those add noise without grounding the band. Drop them.

SCOPE-SIMILARITY DISCIPLINE:
- The user prompt provides an est_value_range for THIS bid (e.g., "$585,000 - $1,170,000"). Use those numbers as your scope anchor.
- Awards more than 5× outside that range (either direction — too small or too large) are NOT comparable for band calculation. They distort the band by anchoring on scope the bidder can't deliver or wouldn't be asked to. Example: if est_value_range is $500K-$1M, a $25M multi-year platform contract is a 25× outlier — exclude it.
- If a far-outside-range award is genuinely informative (e.g., it reveals an unusual market signal worth surfacing), you may include it — but mark relevance_score ≤ 0.3 and explain in notes WHY it's an outlier and what it tells you. Do not let outliers drive the band.

IMPORTANT OVERRIDE ON THE ROUGH RANGE:
- The rough value range in the user prompt is an internal, low-confidence query-scoping aid. It is not buyer budget, not a target price, and not evidence.
- Do NOT anchor the market band on that rough range. The band must come from cited comparable awards with disclosed values, preferably normalized to this RFP's period of performance.
- If fewer than two valued comparable awards are found, leave the band unknown/null and flag insufficient data instead of estimating from the rough range.

ORDER: Most relevant comparable awards first. Most likely-to-bid competitors first."""


_RESEARCH_USER_TEMPLATE = """Research the federal market for this RFP. Use Google Search liberally.

=== RFP context ===
Title: {rfp_title}
Customer agency: {rfp_agency}
NAICS: {naics}
Period of performance: ~{pop_months} months
Estimated FTE count (rough): {est_fte}
Internal rough sanity range (low-confidence; do NOT anchor the band): ${est_value_low_usd:,.0f} - ${est_value_high_usd:,.0f}

=== Brief scope ===
{scope_summary}

=== Quadratic Digital (us — for reciprocal-fit awareness) ===
{quadratic_summary}

Produce the research brief now in the format described in your instructions. Search the web before answering. Cite URLs for every claim."""


# ---- Step 2 — structuring -------------------------------------------------

_TOOL: dict = {
    "name": "report_market_scan",
    "description": (
        "Convert the upstream research brief into the structured market "
        "scan result. Do NOT add data not present in the brief. Do NOT "
        "drop entries unless they're clearly malformed (no source URL, "
        "no firm/award name). If a numeric field is missing, use null — "
        "do not invent. Preserve order from the brief: most relevant "
        "awards first, most likely competitors first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "market_band_low_usd": {
                "type": ["number", "null"],
                "description": (
                    "Low end of the typical contract value for this "
                    "scope, USD. Null if the brief couldn't establish "
                    "a band."
                ),
            },
            "market_band_mid_usd": {
                "type": ["number", "null"],
                "description": "Mid (most-likely) value. Null if not in the brief.",
            },
            "market_band_high_usd": {
                "type": ["number", "null"],
                "description": "High end. Null if not in the brief.",
            },
            "methodology": {
                "type": "string",
                "description": (
                    "2-3 sentence summary from the brief on how the "
                    "band was derived. Used downstream as the agent's "
                    "defense of the market position claim."
                ),
            },
            "insufficient_data_warning": {
                "type": ["string", "null"],
                "description": (
                    "Set when the brief explicitly flagged sparse data "
                    "(< 3 comparable awards) or when the band's "
                    "low/high spread exceeds 3x. Null when grounding "
                    "is solid. Used by Cost Analyst to caveat the "
                    "vs-market-position commentary."
                ),
            },
            "comparable_awards": {
                "type": "array",
                "description": "One entry per award listed in the brief.",
                "items": {
                    "type": "object",
                    "properties": {
                        "award_title": {"type": "string"},
                        "award_value_usd": {"type": ["number", "null"]},
                        "period_of_performance_months": {"type": ["integer", "null"]},
                        "awardee_name": {"type": ["string", "null"]},
                        "customer_agency": {"type": ["string", "null"]},
                        "source_url": {
                            "type": "string",
                            "description": (
                                "REQUIRED — every comparable award must "
                                "trace to a public source URL. Drop "
                                "entries that don't have one."
                            ),
                        },
                        "relevance_score": {
                            "type": ["number", "null"],
                            "description": (
                                "0.0-1.0 — agent's confidence this "
                                "award is comparable to the current "
                                "RFP scope."
                            ),
                        },
                        "notes": {"type": "string"},
                    },
                    "required": ["award_title", "source_url", "notes"],
                },
            },
            "competitors": {
                "type": "array",
                "description": "One entry per firm listed in the brief.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "likelihood_to_bid": {
                            "type": "string",
                            "enum": [lk.value for lk in CompetitorBidLikelihood],
                        },
                        "estimated_rate_low_usd": {"type": ["number", "null"]},
                        "estimated_rate_high_usd": {"type": ["number", "null"]},
                        "rate_estimation_basis": {
                            "type": "string",
                            "description": (
                                "Show the math from the brief: "
                                "'$X award ÷ Y mo ÷ Z FTE × 1950 = "
                                "$/hr blended'. Required if rate "
                                "fields are populated."
                            ),
                        },
                        "source_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "REQUIRED — at least one URL per competitor. Drop entries with no source."
                            ),
                        },
                        "notes": {"type": "string"},
                    },
                    "required": [
                        "name",
                        "likelihood_to_bid",
                        "rate_estimation_basis",
                        "source_urls",
                        "notes",
                    ],
                },
            },
        },
        "required": [
            "methodology",
            "comparable_awards",
            "competitors",
        ],
    },
}


_STRUCTURE_SYSTEM = """You convert a free-form market-research brief about federal contracts and competitors into the strict structured format the Cost Analyst pipeline requires. Do NOT add awards or firms not in the brief; do NOT change values; do NOT invent URLs. Drop entries that are missing a source URL — citation is required for every persisted row. Preserve the brief's ordering.

Call the report_market_scan tool with the structured output."""


_STRUCTURE_USER_TEMPLATE = """Convert the research brief below into the structured market_scan tool call.

=== Research brief ===
{brief}

=== Citations attached to the brief ===
{citations}

Call report_market_scan now."""


# ---- Public entry point ---------------------------------------------------


@dataclass
class MarketResearchInputs:
    """Canonical inputs the orchestrator gathers and hands to the agent."""

    rfp_title: str
    rfp_agency: str
    naics: str
    pop_months: int
    est_fte: float
    est_value_low_usd: float
    est_value_high_usd: float
    scope_summary: str
    quadratic_summary: str


def research_market(
    *,
    proposal_id: int,
    inputs: MarketResearchInputs,
) -> MarketScanResult:
    """Run the two-step Cost Market Researcher pipeline. Returns a
    structured result the orchestrator persists.

    Does NOT touch the DB itself — caller is responsible for upserting
    the MarketScan + relational detail rows. This keeps the agent
    testable in isolation and matches the ConsistencyChecker pattern.

    Caller MUST handle exceptions — the Gemini grounded call can fail
    on rate limits, search-API errors, or model availability. Re-raise
    so the job orchestrator can mark the run failed and surface in
    the stage banner.
    """
    settings = get_settings()
    gemini = get_gemini()

    # ---- Step 1: grounded research ----
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

    brief, citations, _grounded_usage = gemini.complete_with_search(
        model=settings.model_market_researcher,
        system=_RESEARCH_SYSTEM,
        user_prompt=research_user,
        max_tokens=10000,
        agent_name="market_researcher_grounded",
        proposal_id=proposal_id,
    )

    if not brief.strip():
        raise RuntimeError(
            "market_researcher: Gemini grounded call returned empty "
            "brief. Possible safety filter or empty search results. "
            "Check the agent_runs row for input/output token counts."
        )

    log.info(
        "market_researcher: grounded brief = %d chars, %d citations",
        len(brief),
        len(citations),
    )

    # ---- Step 2: structuring ----
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
        agent_name="market_researcher_structure",
        proposal_id=proposal_id,
    )

    if struct_usage.get("stop_reason") == "max_tokens":
        # Truncated mid-tool-call — empty findings would be a silent-zero.
        # Match the ConsistencyChecker fix: raise instead of returning
        # half-built data the orchestrator might persist as "complete".
        n_partial_awards = len(tool_input.get("comparable_awards") or [])
        n_partial_competitors = len(tool_input.get("competitors") or [])
        raise RuntimeError(
            f"market_researcher: structuring output truncated at "
            f"max_tokens (in={struct_usage['input_tokens']}, "
            f"out={struct_usage['output_tokens']}). Got {n_partial_awards} "
            f"partial award(s) + {n_partial_competitors} partial "
            f"competitor(s). Bump max_tokens or split the brief."
        )

    # ---- Build dataclass result ----
    awards = []
    for a in tool_input.get("comparable_awards", []) or []:
        try:
            url = str(a.get("source_url") or "").strip()
            if not url:
                # Drop entries without citations — required per schema.
                log.warning(
                    "market_researcher: dropping comparable award with no source_url: %r",
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
        except (KeyError, TypeError) as exc:
            log.warning(
                "market_researcher: skipping malformed award %r: %s",
                a,
                exc,
            )

    competitors = []
    for c in tool_input.get("competitors", []) or []:
        try:
            urls = [str(u) for u in (c.get("source_urls") or []) if u]
            if not urls:
                log.warning(
                    "market_researcher: dropping competitor with no source_urls: %r",
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
        except (KeyError, TypeError) as exc:
            log.warning(
                "market_researcher: skipping malformed competitor %r: %s",
                c,
                exc,
            )

    # ---- Sparse-data check ----
    # Per Jordan: produce reports with what we have, just flag insufficiency.
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
        "market_researcher: proposal %d — band=$%s/$%s/$%s, %d awards, %d competitors, insufficient=%s",
        proposal_id,
        result.market_band_low_usd,
        result.market_band_mid_usd,
        result.market_band_high_usd,
        len(awards),
        len(competitors),
        bool(insufficient),
    )
    return result


# ---- Helpers --------------------------------------------------------------


def _to_float_or_none(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


__all__ = [
    "ComparableAward",
    "Competitor",
    "MarketResearchInputs",
    "MarketScanResult",
    "research_market",
]
