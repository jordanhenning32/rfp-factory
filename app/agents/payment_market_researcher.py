"""Payment-Systems Market Researcher — service_line=payment_systems variant.

Two-step grounded research producing the data the Cost Writer needs to
draft a competitive payment-processing fee narrative:

  1. **Grounded research (Gemini 2.5 Pro + Google Search)** — searches
     for the typical pricing model for THIS kind of payment-processing
     procurement, comparable processor rate disclosures (Forte, NIC/
     Tyler, Heartland, Worldpay etc. recent county/municipal awards),
     and an estimate of the buyer's annual processed-volume tier.

  2. **Structuring (Haiku, forced tool call)** — converts the brief
     into the `report_payment_market_scan` schema:
       - pricing_structure (model + recommended rate posture)
       - comparable_awards (processor + rate + customer + source URL)
       - competitor_processors (likely bidders + market posture)
       - volume_estimate (low / mid / high annual processed $)

Why a separate agent (vs. the labor-flow market_researcher.py): the
labor researcher's schema is built around per-FTE billing rates and
comparable AWARD VALUES; it doesn't capture the variables a payment-
processing bid hinges on (pricing model, basis points over interchange,
processed-volume tier, transaction-count). Forcing payment data into
the labor schema produces awkward, half-empty rows that the Cost
Writer can't narrate from cleanly.

Cost: ~$0.10-0.30 per scan (1 Gemini Pro grounded + 1 Haiku structuring).

The orchestrator (jobs/payment_market_researcher.py) computes the
profit math AFTER this agent returns, by combining the recommended
rate × estimated volume - our_cost_basis. That math is deliberately
in the orchestrator (not the agent) — keeps the agent's job focused on
research, lets us re-run profit math without re-running grounded
search if the cost basis changes.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from app.config import get_settings
from app.services.llm import call_tool_for_model, get_gemini

log = logging.getLogger(__name__)


# ---- Output dataclasses ---------------------------------------------------


@dataclass
class PaymentPricingStructure:
    """The pricing model the LLM recommends for THIS procurement and
    how our proposed rate positions vs. the market median."""

    pricing_model: str  # interchange_plus | flat_rate | tiered | percentage_of_collected | hybrid
    pricing_model_rationale: str
    median_market_credit_card_markup_bps: int | None
    proposed_credit_card_markup_bps: int | None
    median_market_per_txn_fee_usd: float | None
    proposed_per_txn_fee_usd: float | None
    median_market_ach_fee_usd: float | None
    proposed_ach_fee_usd: float | None
    median_market_monthly_fee_usd: float | None
    proposed_monthly_fee_usd: float | None
    other_fees_recommended: list[dict[str, Any]] = field(default_factory=list)
    rate_positioning: str = "match"  # match | beat_by_5_pct | beat_by_10_pct | premium


@dataclass
class ComparableProcessorAward:
    """One real processor contract with disclosed rates that informs
    the recommended structure."""

    processor_name: str
    customer_name: str
    award_year: int | None
    pricing_model: str
    disclosed_credit_card_rate_text: str
    annual_volume_estimate_usd: float | None
    contract_term_years: int | None
    source_url: str
    notes: str
    # Dual-pipeline provenance (set by payment_market_consolidator).
    # Empty list / False when this row came from a single-provider
    # run — the cost block then renders no chips.
    confirmed_by: list[str] = field(default_factory=list)
    needs_review: bool = False


@dataclass
class CompetitorProcessor:
    """A processor likely to bid this RFP."""

    name: str
    market_position: str  # incumbent | challenger | niche
    typical_pricing_summary: str
    likelihood_to_bid: str  # HIGH | MEDIUM | LOW
    source_urls: list[str]
    notes: str
    # Dual-pipeline provenance (set by payment_market_consolidator).
    confirmed_by: list[str] = field(default_factory=list)
    needs_review: bool = False


@dataclass
class VolumeEstimate:
    """Estimated annual processed volume for the buyer."""

    annual_processed_volume_low_usd: float | None
    annual_processed_volume_midpoint_usd: float | None
    annual_processed_volume_high_usd: float | None
    estimated_transaction_count_annual: int | None
    average_transaction_size_usd: float | None
    estimation_basis: str
    confidence: str  # low | medium | high


@dataclass
class ProfitMath:
    """Computed by the ORCHESTRATOR after the agent returns. Not part
    of the agent's tool-call output — left here as a dataclass shared
    between agent and orchestrator so the persistence shape is one
    object."""

    annual_processor_revenue_low_usd: float | None = None
    annual_processor_revenue_midpoint_usd: float | None = None
    annual_processor_revenue_high_usd: float | None = None
    annual_internal_costs_usd: float | None = None
    annual_net_profit_low_usd: float | None = None
    annual_net_profit_midpoint_usd: float | None = None
    annual_net_profit_high_usd: float | None = None
    profit_margin_pct_at_midpoint: float | None = None
    cost_basis_assumptions: list[str] = field(default_factory=list)
    computation_notes: str = ""


@dataclass
class PaymentMarketScanResult:
    pricing_structure: PaymentPricingStructure
    comparable_awards: list[ComparableProcessorAward]
    competitor_processors: list[CompetitorProcessor]
    volume_estimate: VolumeEstimate
    profit_math: ProfitMath
    insufficient_data_warning: bool
    citations: list[dict[str, str]]

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaymentMarketResearchInputs:
    rfp_title: str
    rfp_agency: str
    scope_summary: str
    quadratic_summary: str
    fallback_rate_ranges: dict[str, Any]  # from data/pricing/payment_systems.json
    # When set, the agent is INSTRUCTED to recommend this specific
    # pricing model (overrides the agent's own model selection). Used
    # by the "Re-run scan with <model> focus" UI affordance when the
    # user has overridden the agent's recommendation. None = let the
    # agent pick the best fit.
    model_focus: str | None = None


# ---- Step 1 — grounded research prompt ------------------------------------

_RESEARCH_SYSTEM = """You are a payment-processing market analyst. Your job: research the typical pricing for the kind of contract described in the user prompt, find comparable real-world awards, estimate the buyer's processed-volume tier, and identify likely competitor processors. You search the web liberally via Google Search.

You output a free-form research brief — NO JSON yet, no tool calls. The output gets converted to a structured schema in a follow-up call. Cite source URLs for every factual claim. Be honest about gaps; mark "insufficient data" when you can't find solid grounding.

Key research tasks:

1. PRICING MODEL — Identify which pricing model county/municipal payment-processing procurements typically use for THIS kind of buyer (POS, online, ACH, or hybrid). Common models:
   - Interchange-plus: cost = (interchange) + (markup bps) + ($ per txn). Most transparent; most counties prefer it.
   - Flat-rate: single % + fixed $ per txn (Stripe/Square style). Smaller buyers / e-commerce-only.
   - Tiered: qualified / mid-qualified / non-qualified rates. Older model; rare in modern county RFPs.
   - Percentage-of-collected: % of total recoveries. Receivables / collections context, NOT typical POS.
   Recommend ONE model with rationale. Don't hedge.

2. COMPARABLE PROCESSOR RATE DISCLOSURES — find 5-10 real, recent (2022-2026) processor contracts with public rate disclosures. Best sources:
   - State / county procurement websites publishing awarded contracts ("[county/state] payment processor RFP award" + the typical reissue cycle of 3-5 years)
   - Federal awards: SAM.gov, USAspending.gov for payment-processing line items
   - Processor public rate cards (Stripe public-sector, Square public-sector, PayPal-government, etc.)
   - State treasurer / DOR procurement docs for merchant services
   For each, capture: processor, customer (county/state/agency), year, pricing model, disclosed rate (e.g., "interchange + 25 bps + $0.10/txn"), annual processed volume if disclosed, contract term, source URL.

3. RATE BAND — From the comparable disclosures, derive the median + low/high credit-card markup bps, per-txn fee, ACH fee, monthly fee for THIS procurement tier (small county / mid-sized county / state agency / etc.).

4. VOLUME ESTIMATE — estimate the buyer's annual processed volume. Method depends on the buyer type:
   - County government: search the county's annual financial report, total operating budget, fee revenue, court fines + utilities + tax payments accepting cards. Estimate the % of those flows that go through card.
   - Municipal: same approach scaled to city size.
   - State agency: agency-specific revenue lines.
   Provide LOW / MIDPOINT / HIGH bands. Cite the budget-document URL. If you can't find a budget, use comparable-county-population proxies and document the proxy.

5. LIKELY COMPETITORS — 4-7 processors likely to bid this RFP. Common payments incumbents in county procurement: Forte (CSG), NIC (Tyler Technologies), Heartland (Global Payments), Worldpay (FIS), Bank of America Merchant Services, Elavon, JetPay, Authorize.Net, PaymentVision. Surface the firms that fit THIS buyer's size / vertical / geography.

6. RECOMMENDED RATE POSTURE — given the median market rate from #3 and our small-vendor-modernization positioning, recommend our proposed credit-card markup bps + per-txn fee + monthly fee. Default posture: match median or beat by 5-10% to win on price without underselling. State the posture explicitly.

OUTPUT FORMAT — free-form text with these sections (NOT a tool call yet — that's a follow-up):

## Recommended Pricing Model
- Model: <interchange_plus | flat_rate | tiered | percentage_of_collected | hybrid>
- Rationale: <2-3 sentences on why this model fits this buyer + procurement type>

## Market Rate Band (from comparable awards)
- Median credit-card markup: <bps>
- Median per-txn fee: $<amount>
- Median ACH fee: $<amount>
- Median monthly fee: $<amount>
- Sample size: <N comparable awards used to derive median>
- Spread: <low-high>

## Recommended Rate Posture
- Proposed credit-card markup: <bps> (vs. median <bps> — match | beat by X%)
- Proposed per-txn fee: $<amount>
- Proposed ACH fee: $<amount>
- Proposed monthly fee: $<amount>
- Other fees: <statement, batch, chargeback, PCI compliance — recommended values>
- Positioning: <match | beat_by_5_pct | beat_by_10_pct | premium>
- Rationale: <1-2 sentences>

## Comparable Processor Awards
For each award:
- Processor: <name>
- Customer: <name>
- Year: <YYYY>
- Pricing model: <model>
- Disclosed rate: <free-form text — e.g., "interchange + 25 bps + $0.10/txn, $25/mo">
- Annual volume estimate: $<amount>
- Term: <years>
- Source: <URL>
- Notes: <1-sentence relevance>

## Likely Competitor Processors
For each:
- Name: <processor>
- Market position: <incumbent | challenger | niche>
- Typical pricing summary: <1-sentence>
- Likelihood to bid: <HIGH | MEDIUM | LOW>
- Sources: <list URLs>
- Notes: <1-sentence>

## Annual Processed Volume Estimate
- Low: $<amount>
- Midpoint: $<amount>
- High: $<amount>
- Estimated transaction count (annual): <N>
- Average transaction size: $<amount>
- Estimation basis: <2-3 sentences on how derived; cite budget URL if used>
- Confidence: <low | medium | high>

GROUNDING DISCIPLINE:
- Real processors, real customers, real award URLs only. Never invent.
- If you find fewer than 3 comparable disclosures with rates, say so explicitly: "## Insufficient Data — only N comparable rate disclosures found." The downstream pipeline handles sparse data.
- County budget data is usually findable via the county's "Annual Comprehensive Financial Report" (ACFR) or "CAFR" — search for those terms.
- Volume estimates are approximate. Wide ranges (1.5x spread) are honest; tight ranges require strong sourcing.
- Skip any processor / award you can't verify exists.
"""

_RESEARCH_USER_TEMPLATE = """Research the payment-processing market for this procurement. Use Google Search liberally.

=== RFP context ===
Title: {rfp_title}
Customer agency: {rfp_agency}

=== Brief scope ===
{scope_summary}

=== Quadratic Financial (us — for fit awareness) ===
{quadratic_summary}

=== Fallback rate ranges (system defaults — use only if grounded research yields nothing) ===
{fallback_rate_ranges_block}

{model_focus_block}Produce the research brief now in the format described in your instructions. Search the web before answering. Cite URLs for every factual claim."""


_MODEL_FOCUS_BLOCK_TEMPLATE = """=== USER PRICING MODEL OVERRIDE ===
The user has explicitly chosen `{model_focus}` as the pricing model for this bid (overriding the model the agent would normally pick). Recommend rates aligned with that model:
  - Set `pricing_model` = "{model_focus}" in your structured output.
  - Search for comparable awards using THAT model — not the model that's most common in county procurement, the model the user picked.
  - Derive median market rates from disclosures in the chosen model. If the chosen model is rare for this kind of buyer, the brief MUST flag that explicitly so the user can decide whether to re-run with a different choice.
  - Frame `pricing_model_rationale` as "Per user override — recommending <model_focus> with rates derived from comparable <model_focus> disclosures." Do NOT argue for a different model.

"""


# ---- Step 2 — structuring -------------------------------------------------

_TOOL: dict = {
    "name": "report_payment_market_scan",
    "description": (
        "Convert the upstream payment-processing research brief into "
        "the structured market scan result. Do NOT add data not "
        "present in the brief. Do NOT drop entries unless they're "
        "clearly malformed. Preserve order from the brief."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pricing_model": {
                "type": "string",
                "description": "interchange_plus | flat_rate | tiered | percentage_of_collected | hybrid",
            },
            "pricing_model_rationale": {"type": "string"},
            "median_market_credit_card_markup_bps": {"type": ["integer", "null"]},
            "proposed_credit_card_markup_bps": {"type": ["integer", "null"]},
            "median_market_per_txn_fee_usd": {"type": ["number", "null"]},
            "proposed_per_txn_fee_usd": {"type": ["number", "null"]},
            "median_market_ach_fee_usd": {"type": ["number", "null"]},
            "proposed_ach_fee_usd": {"type": ["number", "null"]},
            "median_market_monthly_fee_usd": {"type": ["number", "null"]},
            "proposed_monthly_fee_usd": {"type": ["number", "null"]},
            "other_fees_recommended": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "amount_usd": {"type": ["number", "null"]},
                        "notes": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
            "rate_positioning": {
                "type": "string",
                "description": "match | beat_by_5_pct | beat_by_10_pct | premium",
            },
            "comparable_awards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "processor_name": {"type": "string"},
                        "customer_name": {"type": "string"},
                        "award_year": {"type": ["integer", "null"]},
                        "pricing_model": {"type": "string"},
                        "disclosed_credit_card_rate_text": {"type": "string"},
                        "annual_volume_estimate_usd": {"type": ["number", "null"]},
                        "contract_term_years": {"type": ["integer", "null"]},
                        "source_url": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                    "required": ["processor_name", "customer_name", "source_url"],
                },
            },
            "competitor_processors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "market_position": {
                            "type": "string",
                            "description": "incumbent | challenger | niche",
                        },
                        "typical_pricing_summary": {"type": "string"},
                        "likelihood_to_bid": {
                            "type": "string",
                            "description": "HIGH | MEDIUM | LOW",
                        },
                        "source_urls": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "string"},
                    },
                    "required": ["name", "likelihood_to_bid"],
                },
            },
            "annual_processed_volume_low_usd": {"type": ["number", "null"]},
            "annual_processed_volume_midpoint_usd": {"type": ["number", "null"]},
            "annual_processed_volume_high_usd": {"type": ["number", "null"]},
            "estimated_transaction_count_annual": {"type": ["integer", "null"]},
            "average_transaction_size_usd": {"type": ["number", "null"]},
            "volume_estimation_basis": {"type": "string"},
            "volume_confidence": {
                "type": "string",
                "description": "low | medium | high",
            },
            "insufficient_data_warning": {
                "type": "boolean",
                "description": "True when the brief flagged sparse data (< 3 comparable rate disclosures).",
            },
        },
        "required": [
            "pricing_model",
            "pricing_model_rationale",
            "comparable_awards",
            "competitor_processors",
            "volume_estimation_basis",
            "volume_confidence",
            "insufficient_data_warning",
        ],
    },
}


_STRUCTURE_SYSTEM = (
    "You convert a free-form payment-processing market-research brief "
    "into a structured tool call. Preserve every entry from the brief. "
    "If a numeric field isn't in the brief, return null — never invent. "
    "Forced tool call: you MUST call report_payment_market_scan."
)


_STRUCTURE_USER_TEMPLATE = """Convert this research brief into the report_payment_market_scan tool call. Preserve every comparable award, every competitor, every numeric estimate. Use null for missing numeric fields. Do not add or drop entries.

=== Research brief ===
{brief}

=== Citations attached to the brief (URLs from grounded search) ===
{citations}
"""


# ---- Public entry point ---------------------------------------------------


def research_payment_market(
    *,
    proposal_id: int,
    inputs: PaymentMarketResearchInputs,
) -> PaymentMarketScanResult:
    """Run the two-step payment-systems market researcher pipeline.
    Returns a structured result the orchestrator persists to
    proposals.payment_market_scan_json (after computing profit math)."""
    settings = get_settings()
    gemini = get_gemini()

    fallback_block = _format_fallback_block(inputs.fallback_rate_ranges)
    model_focus_block = _format_model_focus_block(inputs.model_focus)

    research_user = _RESEARCH_USER_TEMPLATE.format(
        rfp_title=inputs.rfp_title or "(untitled)",
        rfp_agency=inputs.rfp_agency or "(agency unknown)",
        scope_summary=inputs.scope_summary or "(no scope summary available)",
        quadratic_summary=(inputs.quadratic_summary or "(no Quadratic summary provided)"),
        fallback_rate_ranges_block=fallback_block,
        model_focus_block=model_focus_block,
    )

    brief, citations, _grounded_usage = gemini.complete_with_search(
        model=settings.model_market_researcher,
        system=_RESEARCH_SYSTEM,
        user_prompt=research_user,
        max_tokens=10000,
        agent_name="payment_market_researcher_grounded",
        proposal_id=proposal_id,
    )

    if not brief.strip():
        raise RuntimeError(
            "payment_market_researcher: Gemini grounded call returned "
            "empty brief. Possible safety filter or empty search "
            "results. Check the agent_runs row for input/output token "
            "counts."
        )

    log.info(
        "payment_market_researcher: grounded brief = %d chars, %d citations",
        len(brief),
        len(citations),
    )

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
        agent_name="payment_market_researcher_structure",
        proposal_id=proposal_id,
    )

    if struct_usage.get("stop_reason") == "max_tokens":
        n_partial_awards = len(tool_input.get("comparable_awards") or [])
        n_partial_competitors = len(tool_input.get("competitor_processors") or [])
        raise RuntimeError(
            f"payment_market_researcher: structuring output truncated "
            f"at max_tokens (in={struct_usage['input_tokens']}, "
            f"out={struct_usage['output_tokens']}). Got "
            f"{n_partial_awards} partial award(s) + "
            f"{n_partial_competitors} partial competitor(s). Bump "
            f"max_tokens or split the brief."
        )

    return _assemble_result(tool_input, citations)


# ---- Helpers --------------------------------------------------------------


def _format_model_focus_block(model_focus: str | None) -> str:
    """Render the optional 'USER PRICING MODEL OVERRIDE' block for
    the agent prompt. Empty string when no override — agent picks
    its own model from the four supported options."""
    if not model_focus:
        return ""
    return _MODEL_FOCUS_BLOCK_TEMPLATE.format(model_focus=model_focus)


def _format_fallback_block(ranges: dict[str, Any]) -> str:
    """Render the fallback rate ranges from payment_systems.json as
    plain text for the prompt. Keeps the prompt portable when the
    JSON evolves."""
    if not ranges:
        return "(no fallback ranges available)"
    lines = ["These ranges are SYSTEM defaults — use only if grounded research yields nothing solid:"]
    for k, v in ranges.items():
        if k.startswith("_"):
            continue
        lines.append(f"  - {k}: {v}")
    return "\n".join(lines)


def _assemble_result(
    tool_input: dict[str, Any],
    citations: list[dict[str, str]],
) -> PaymentMarketScanResult:
    """Assemble the structured result from the Haiku tool call. Profit
    math is left blank here — orchestrator computes it from rates +
    volume + cost basis."""
    awards = [
        ComparableProcessorAward(
            processor_name=a.get("processor_name") or "",
            customer_name=a.get("customer_name") or "",
            award_year=a.get("award_year"),
            pricing_model=a.get("pricing_model") or "",
            disclosed_credit_card_rate_text=a.get("disclosed_credit_card_rate_text") or "",
            annual_volume_estimate_usd=a.get("annual_volume_estimate_usd"),
            contract_term_years=a.get("contract_term_years"),
            source_url=a.get("source_url") or "",
            notes=a.get("notes") or "",
        )
        for a in (tool_input.get("comparable_awards") or [])
    ]

    competitors = [
        CompetitorProcessor(
            name=c.get("name") or "",
            market_position=c.get("market_position") or "",
            typical_pricing_summary=c.get("typical_pricing_summary") or "",
            likelihood_to_bid=c.get("likelihood_to_bid") or "",
            source_urls=c.get("source_urls") or [],
            notes=c.get("notes") or "",
        )
        for c in (tool_input.get("competitor_processors") or [])
    ]

    pricing_structure = PaymentPricingStructure(
        pricing_model=tool_input.get("pricing_model") or "",
        pricing_model_rationale=tool_input.get("pricing_model_rationale") or "",
        median_market_credit_card_markup_bps=tool_input.get("median_market_credit_card_markup_bps"),
        proposed_credit_card_markup_bps=tool_input.get("proposed_credit_card_markup_bps"),
        median_market_per_txn_fee_usd=tool_input.get("median_market_per_txn_fee_usd"),
        proposed_per_txn_fee_usd=tool_input.get("proposed_per_txn_fee_usd"),
        median_market_ach_fee_usd=tool_input.get("median_market_ach_fee_usd"),
        proposed_ach_fee_usd=tool_input.get("proposed_ach_fee_usd"),
        median_market_monthly_fee_usd=tool_input.get("median_market_monthly_fee_usd"),
        proposed_monthly_fee_usd=tool_input.get("proposed_monthly_fee_usd"),
        other_fees_recommended=tool_input.get("other_fees_recommended") or [],
        rate_positioning=tool_input.get("rate_positioning") or "match",
    )

    volume_estimate = VolumeEstimate(
        annual_processed_volume_low_usd=tool_input.get("annual_processed_volume_low_usd"),
        annual_processed_volume_midpoint_usd=tool_input.get("annual_processed_volume_midpoint_usd"),
        annual_processed_volume_high_usd=tool_input.get("annual_processed_volume_high_usd"),
        estimated_transaction_count_annual=tool_input.get("estimated_transaction_count_annual"),
        average_transaction_size_usd=tool_input.get("average_transaction_size_usd"),
        estimation_basis=tool_input.get("volume_estimation_basis") or "",
        confidence=tool_input.get("volume_confidence") or "low",
    )

    return PaymentMarketScanResult(
        pricing_structure=pricing_structure,
        comparable_awards=awards,
        competitor_processors=competitors,
        volume_estimate=volume_estimate,
        profit_math=ProfitMath(),  # filled by orchestrator
        insufficient_data_warning=bool(tool_input.get("insufficient_data_warning") or False),
        citations=[{"title": c.get("title", ""), "uri": c.get("uri", "")} for c in citations],
    )


__all__ = [
    "PaymentPricingStructure",
    "ComparableProcessorAward",
    "CompetitorProcessor",
    "VolumeEstimate",
    "ProfitMath",
    "PaymentMarketScanResult",
    "PaymentMarketResearchInputs",
    "research_payment_market",
]
