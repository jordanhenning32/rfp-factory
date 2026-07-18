"""Teaming Researcher — two-step grounded market research that enriches
the Shortfall Strategist's partner suggestions with verifiable firm names.

Pipeline per gap:

  1. **Grounded research (Gemini Pro + Google Search)** — free-form
     research call with `tools=[GoogleSearch()]` enabled. Gemini issues
     web queries on demand and returns a research brief grounded in
     real, current sources. Citations are captured (URL + title) and
     attached to each partner profile we end up structuring.

  2. **Structuring (Haiku, forced tool call)** — cheap follow-up call
     that takes the research brief and emits the strict
     `partner_suggestions[]` schema the rest of the pipeline expects.
     Haiku doesn't need market knowledge here; it just reformats text.

We split the call because Gemini disallows `tools=[GoogleSearch]`
combined with `tool_config(mode='ANY', allowed_function_names=…)` in
the same request — they're mutually exclusive APIs. Two calls is
cheaper than not having the grounding at all.

Cost: ~$0.04 (Pro grounded) + ~$0.005 (Haiku structuring) ≈ $0.045
per gap. Typical RFP with 5-15 teaming gaps → ~$0.25-0.70 added to
intake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings
from app.services.llm import call_tool_for_model, get_gemini

log = logging.getLogger(__name__)


_TOOL: dict = {
    "name": "report_partner_research",
    "description": (
        "Report a ranked list of teaming-partner candidates for ONE "
        "specific compliance gap. Each entry must be a real firm "
        "(specific name) with structured fit + profile data. Do not "
        "invent firms; if you can't think of 5 strong fits, return "
        "fewer with HIGH confidence rather than fill with weak guesses."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "partners": {
                "type": "array",
                "description": ("5-8 partner candidates ordered by fit (best first)."),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Specific firm name. NEVER 'Partner X', "
                                "'TBD', or category labels like 'a CRM "
                                "specialist'. Real firms only."
                            ),
                        },
                        "fit_rationale": {
                            "type": "string",
                            "description": (
                                "1-2 sentences on why this partner fills "
                                "THIS specific gap. Be concrete (cite the "
                                "capability, certification, geography, or "
                                "vehicle that makes them fit)."
                            ),
                        },
                        "capability_focus": {
                            "type": "string",
                            "description": (
                                "Short label for what this partner brings "
                                "(e.g., 'Drupal-on-Acquia implementer', "
                                "'HUBZone prime', 'Section 508 specialist', "
                                "'NC-based small business'). Used as the "
                                "good_fit_for hint when adding to library."
                            ),
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["HIGH", "MEDIUM", "LOW"],
                            "description": (
                                "HIGH = well-known firm, you're confident "
                                "the profile is accurate. MEDIUM = real "
                                "firm but some details may be imprecise. "
                                "LOW = uncertain firm — flag for user to "
                                "verify before contacting."
                            ),
                        },
                        "profile": {
                            "type": "object",
                            "description": (
                                "Detailed profile mirroring the Shortfall "
                                "Strategist's schema so this output can be "
                                "dropped into mitigation_options[]."
                                "partner_suggestions[].profile directly."
                            ),
                            "properties": {
                                "overview": {
                                    "type": "string",
                                    "description": (
                                        "2-3 sentences: what they do, "
                                        "scale (employees / revenue band "
                                        "if known), where they operate."
                                    ),
                                },
                                "why_fits_this_project": {
                                    "type": "string",
                                    "description": (
                                        "2-4 sentences tying this firm "
                                        "specifically to THIS RFP — "
                                        "agency context, scope match, "
                                        "the gap being addressed."
                                    ),
                                },
                                "why_fits_quadratic": {
                                    "type": "string",
                                    "description": (
                                        "2-4 sentences on why this firm "
                                        "complements Quadratic Digital "
                                        "specifically (small public-"
                                        "sector software firm, ~12 named "
                                        "key personnel, custom-build + "
                                        "AI-assisted development edge). "
                                        "Reciprocal-fit angle: what does "
                                        "Quadratic offer THEM."
                                    ),
                                },
                                "key_capabilities": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "5-10 specific capability bullets "
                                        "(technologies, methodologies, "
                                        "domain expertise)."
                                    ),
                                },
                                "certs_or_set_asides": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Certifications and set-aside "
                                        "designations: HUBZone, 8(a), "
                                        "SDVOSB, WOSB, ISO 27001, "
                                        "FedRAMP Moderate / High, CMMC "
                                        "level, B Corp, etc. Empty array "
                                        "if none known. NEVER guess."
                                    ),
                                },
                                "engagement_model": {
                                    "type": "string",
                                    "description": (
                                        "1-2 sentences on how they "
                                        "typically engage (subcontractor "
                                        "to small firms, prime on small "
                                        "deals, platform reseller, "
                                        "etc.) and rough deal-size band "
                                        "if known."
                                    ),
                                },
                                "market_signals": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "OPTIONAL. Recent verifiable "
                                        "context: a notable contract win "
                                        "in the past 24 months, recent "
                                        "acquisition, leadership change, "
                                        "etc. Null if you don't have "
                                        "recent grounded info — DO NOT "
                                        "fabricate market signals."
                                    ),
                                },
                                "contact": {
                                    "type": "object",
                                    "description": (
                                        "Public contact info only — "
                                        "website + primary office + "
                                        "general email. Never invent "
                                        "specific people's names, phone "
                                        "numbers, or BD contacts."
                                    ),
                                    "properties": {
                                        "website": {
                                            "type": ["string", "null"],
                                            "description": ("Public corporate URL or null if uncertain."),
                                        },
                                        "primary_location": {
                                            "type": ["string", "null"],
                                            "description": ("HQ city/state. Null if uncertain."),
                                        },
                                        "general_email": {
                                            "type": ["string", "null"],
                                            "description": (
                                                "Public general / "
                                                "info@ email. Null if "
                                                "uncertain — DO NOT "
                                                "invent."
                                            ),
                                        },
                                        "linkedin": {
                                            "type": ["string", "null"],
                                            "description": ("LinkedIn company page URL or null."),
                                        },
                                    },
                                },
                            },
                            "required": [
                                "overview",
                                "why_fits_this_project",
                                "why_fits_quadratic",
                                "key_capabilities",
                                "certs_or_set_asides",
                                "engagement_model",
                                "contact",
                            ],
                        },
                    },
                    "required": [
                        "name",
                        "fit_rationale",
                        "capability_focus",
                        "confidence",
                        "profile",
                    ],
                },
            },
        },
        "required": ["partners"],
    },
}


# --- Step 1 — grounded research ------------------------------------

_RESEARCH_SYSTEM = """You are a teaming-partner market researcher for Quadratic Digital, a small public-sector software firm based in Harrisburg, PA. Quadratic has ~12 named key personnel; their competitive edge is rapid AI-assisted custom development for state and federal agencies.

You have access to Google Search. USE IT — your job is grounded, current research about real firms, not memory recall. Run searches as needed to find:

- Firms that have won similar contracts at the same agency or similar agencies in the past 3 years
- Firms with the specific certification, set-aside, contract vehicle, or platform credential the gap needs
- Recent acquisitions, mergers, or leadership changes that affect firm fit
- Whether each firm is still operating + their current scale

OUTPUT (free-form text, NOT a tool call):
Produce a ranked list of 5-8 SPECIFIC partner candidates for the gap. For each, give:

  ## <Firm name>
  - Confidence: HIGH | MEDIUM | LOW
  - Capability focus: <short label, e.g. "Drupal-on-Acquia implementer", "HUBZone prime">
  - Fit rationale: <1-2 sentences on why this firm fills THIS gap>
  - Overview: <2-3 sentences on what they do, scale, where they operate>
  - Why fits this project: <2-4 sentences tying them to THIS RFP — agency, scope, gap>
  - Why fits Quadratic: <2-4 sentences on reciprocal fit; what Quadratic offers them>
  - Key capabilities: <bullet list of 5-10 specific capabilities>
  - Certifications / set-asides: <list, or "(none confirmed)">
  - Engagement model: <how they typically engage + rough deal-size band>
  - Market signals: <recent verifiable contract wins / acquisitions; "(none found)" if nothing recent>
  - Contact: <website, HQ city/state, general info email if public; LinkedIn if found>

GROUNDING DISCIPLINE:
- Real firms only. NEVER write "a CRM vendor", "TBD", or "Partner X". If you can't find 5 strong fits even with search, return fewer with HIGH confidence rather than fill with weak guesses.
- Fit the customer. A federal CMS health-IT prime is the wrong partner for a NC state web RFP. Match the agency level (federal/state/local), the geography, and the contract vehicle landscape.
- Match the actual capability. Read the requirement_text. A "Section 508 accessibility" gap doesn't need a Drupal vendor; it needs an accessibility-compliance specialist or an integrator with a 508 practice.
- Diverse cohort. Mix larger primes (when certifications/vehicles matter), niche specialists (when expertise is the gap), small businesses with set-asides when the RFP rewards them, and regional firms when geography matters.
- Confidence honesty. HIGH only when search confirmed it. MEDIUM when the firm exists but some details may drift. LOW for uncertain — surface so user verifies before contacting. Never mark HIGH to look thorough.
- Reciprocal fit (critical). Quadratic is small. Bigger primes won't team for free — the "Why fits Quadratic" line MUST explain what Quadratic offers THIS partner (custom dev velocity, AI-assisted delivery, state-agency relationship, etc.). If Quadratic offers them nothing useful, the partner is the wrong fit.
- NEVER invent specific people's names, phone numbers, or BD contacts. Public website + general info@ email + HQ city only.

ORDER: Best fit first. The user reads top-down."""


_RESEARCH_USER_TEMPLATE = """Research teaming-partner candidates for this gap. Use Google Search liberally to ground your answer.

=== RFP context ===
Title: {rfp_title}
Agency: {rfp_agency}
Brief scope: {rfp_scope}

=== Quadratic Digital (us) ===
{quadratic_summary}

=== The gap ===
gap_id: {gap_id}
severity: {gap_severity}
requirement_id: {requirement_id}
requirement_text:
\"\"\"
{requirement_text}
\"\"\"
current_state: {current_state}

=== Partners the upstream Strategist already proposed (verify or improve on these) ===
{strategist_suggestions}

Produce your ranked partner list now in the format described in your instructions."""


# --- Step 2 — structuring ------------------------------------------

_STRUCTURE_SYSTEM = """You convert a free-form market-research brief about teaming-partner candidates into the strict structured format the downstream pipeline requires. Do NOT add new firms, drop firms, change rankings, or invent details — preserve the research output faithfully and only re-shape the text into the schema. If a field isn't in the brief, leave it null/empty rather than fabricate.

Call the report_partner_research tool with one entry per firm in the brief, in the same order they appear."""


_STRUCTURE_USER_TEMPLATE = """Convert the research brief below into the structured partners[] tool call.

=== Research brief ===
{brief}

{citations_block}Use report_partner_research now."""


@dataclass
class TeamingPartnerResearch:
    """One research result for a single gap."""

    gap_id: str
    partners: list[dict]
    citations: list[dict]
    cost_usd: float


def _format_citations_block(citations: list[dict]) -> str:
    """Inline the grounding citations into the structuring prompt so
    the structuring model can append them to each partner's profile if
    they're referenced. Empty when no citations were captured."""
    if not citations:
        return ""
    lines = ["=== Sources cited by Google Search grounding ==="]
    for i, c in enumerate(citations[:20], 1):
        title = (c.get("title") or "").strip()
        uri = (c.get("uri") or "").strip()
        if title or uri:
            lines.append(f"  [{i}] {title} — {uri}")
    return "\n".join(lines) + "\n\n"


def research_partners_for_gap(
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
    """Run the Teaming Researcher on ONE gap.

    Two-step pipeline:
      1. Gemini Pro WITH Google Search grounding → free-form research
         brief. Citations captured.
      2. Haiku WITH forced tool call → structured `partners[]` list
         conforming to the schema downstream consumers expect.

    Returns a `TeamingPartnerResearch` with the structured partners,
    the grounding citations (for surfacing to the user), and the total
    cost across both steps.
    """
    settings = get_settings()
    gemini = get_gemini()

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

    # ---- Step 1: grounded research -----------------------------------
    brief, citations, research_usage = gemini.complete_with_search(
        model=settings.model_teaming_researcher,
        system=_RESEARCH_SYSTEM,
        user_prompt=research_user,
        max_tokens=8000,
        agent_name="teaming_researcher_research",
        proposal_id=proposal_id,
    )
    if not brief.strip():
        log.warning(
            "teaming_researcher: gap=%s — grounded research returned empty text; skipping structuring",
            gap_id,
        )
        return TeamingPartnerResearch(
            gap_id=gap_id,
            partners=[],
            citations=citations,
            cost_usd=float(research_usage.get("cost_usd") or 0.0),
        )

    # ---- Step 2: structuring -----------------------------------------
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
            agent_name="teaming_researcher_structure",
            proposal_id=proposal_id,
        )
    except Exception:
        log.exception(
            "teaming_researcher: gap=%s — structuring step failed; returning empty partner list",
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
        "teaming_researcher: gap=%s -> %d partners, %d citations, total cost=$%.4f",
        gap_id,
        len(raw_partners),
        len(citations),
        total_cost,
    )

    return TeamingPartnerResearch(
        gap_id=gap_id,
        partners=list(raw_partners),
        citations=citations,
        cost_usd=total_cost,
    )


__all__ = [
    "TeamingPartnerResearch",
    "research_partners_for_gap",
]
