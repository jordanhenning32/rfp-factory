"""Shortfall Strategist Agent — gap analysis between RFP requirements and
Quadratic's capabilities.

Per design doc §6.3. For each compliance requirement, the Strategist decides:
- met: Quadratic clearly satisfies; cite the specific evidence.
- partial: Quadratic has analogous capability; "equivalent experience" framing.
- gap: Quadratic does not meet. 2-3 mitigation options OR no-bid recommendation.

Honesty constraints (non-negotiable, baked into the system prompt):
- Never invent certifications, clearances, or past performance.
- "Equivalent experience" only when defensible.
- "In progress" only with a concrete plan.
- Teaming requires a confirmed partner — flagged [NEEDS_HUMAN] until confirmed.
- If no honest mitigation exists: gap_severity=deal_breaker + no_bid_recommended.

The static prefix (profile + KB context) is sent via Anthropic prompt cache
so subsequent batches in the same proposal pay ~10% input cost on the prefix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings
from app.services.llm import fmt_llm_usage, get_anthropic

log = logging.getLogger(__name__)


_TOOL: dict = {
    "name": "report_gap_analyses",
    "description": (
        "Report verdict + gap analysis for every compliance requirement in this batch. "
        "One item per input requirement. Use the requirement_id values exactly as given."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "One entry per input requirement.",
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement_id": {
                            "type": "string",
                            "description": "Must match the REQ-N id from the input batch exactly.",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["met", "partial", "gap"],
                            "description": (
                                "met = Quadratic clearly satisfies; "
                                "partial = analogous experience exists; "
                                "gap = Quadratic does not meet."
                            ),
                        },
                        "current_state": {
                            "type": "string",
                            "description": (
                                "1-3 sentences describing what Quadratic actually has "
                                "relevant to this requirement, drawn from profile and KB. "
                                "Be specific."
                            ),
                        },
                        "evidence_citations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "For met/partial verdicts: short references to the "
                                "specific profile field or KB doc that supports the verdict "
                                "(e.g., 'company_profile.certifications', 'KB DOC #4 — "
                                "project manager resume')."
                            ),
                        },
                        "gap_severity": {
                            "type": ["string", "null"],
                            "enum": [None, "minor", "major", "technical", "deal_breaker"],
                            "description": (
                                "Required for partial/gap. null for met. "
                                "deal_breaker = no honest mitigation; recommend no-bid. "
                                "technical = gap in technical capability (tech stack, "
                                "methodology, platform support, integration). "
                                "major/minor = gap in FIRM characteristics (certifications, "
                                "geography, staffing, contract vehicles, business size)."
                            ),
                        },
                        "mitigation_options": {
                            "type": "array",
                            "description": (
                                "For partial/gap items: 2-3 mitigation options. "
                                "Empty array for met. Empty array if verdict=gap and "
                                "no_bid_recommended=true."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "approach": {
                                        "type": "string",
                                        "enum": [
                                            "self-perform",
                                            "teaming",
                                            "equivalent-experience",
                                            "in-progress",
                                            "custom-build",
                                            "acknowledge-and-risk-frame",
                                            "no-bid",
                                        ],
                                        "description": (
                                            "Strict label — pick one of the enum "
                                            "values. Do NOT append a partner name "
                                            "(e.g. 'teaming with Example Partner' is "
                                            "WRONG — use 'teaming' and put the "
                                            "partner name in partner_suggestions). "
                                            "self-perform = Quadratic does it itself; "
                                            "teaming = bring in a partner (populate "
                                            "partner_suggestions); "
                                            "equivalent-experience = cite analogous "
                                            "Quadratic work; "
                                            "in-progress = Quadratic is actively "
                                            "pursuing the cap (cert/vehicle/cred); "
                                            "custom-build = build to spec instead of "
                                            "using a named product; "
                                            "acknowledge-and-risk-frame = honestly "
                                            "acknowledge the limitation and frame "
                                            "the risk as managed; "
                                            "no-bid = honest no-bid recommendation."
                                        ),
                                    },
                                    "proposal_language_draft": {
                                        "type": "string",
                                        "description": (
                                            "3-5 sentences ready to insert in the proposal. "
                                            "Quadratic's voice, no boilerplate."
                                        ),
                                    },
                                    "honesty_check": {
                                        "type": "string",
                                        "description": (
                                            "1-2 sentences explaining why this language is "
                                            "truthful, not misleading. If 'in progress', "
                                            "name the concrete plan. If 'equivalent experience', "
                                            "explain the defensibility."
                                        ),
                                    },
                                    "additional_action_required": {
                                        "type": ["string", "null"],
                                        "description": (
                                            "Action required outside the proposal "
                                            "(e.g., 'Confirm teaming with Example Partner', "
                                            "'Pursue ISO 27001'). For teaming, MUST be "
                                            "flagged with [NEEDS_HUMAN]."
                                        ),
                                    },
                                    "partner_suggestions": {
                                        "type": "array",
                                        "description": (
                                            "If approach is teaming: 3-5 SPECIFIC partner "
                                            "firms. PREFER partners from the teaming partner "
                                            "library in the cached prefix when one fits. "
                                            "If the library doesn't have a fitting partner "
                                            "for this specific gap, suggest 3-5 firms from "
                                            "your training knowledge with from_library=false "
                                            "— give SPECIFIC company names that actually "
                                            "exist in the relevant market (CRM platform "
                                            "vendors like Salesforce/Slate/Anthology for "
                                            "higher-ed CRM gaps; HUBZone-certified primes "
                                            "for HUBZone gaps; etc.). Empty array for "
                                            "non-teaming approaches. ORDER by best fit first."
                                        ),
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "name": {
                                                    "type": "string",
                                                    "description": "Specific firm name. NEVER 'Partner X' or 'TBD'.",
                                                },
                                                "fit_rationale": {
                                                    "type": "string",
                                                    "description": (
                                                        "1-2 sentences on why this partner "
                                                        "fills THIS specific gap."
                                                    ),
                                                },
                                                "confirmed": {
                                                    "type": "boolean",
                                                    "description": (
                                                        "If from_library=true: pull from "
                                                        "the library entry's confirmed flag. "
                                                        "If from_library=false: always "
                                                        "false (not yet engaged)."
                                                    ),
                                                },
                                                "from_library": {
                                                    "type": "boolean",
                                                    "description": (
                                                        "true if this partner is in the "
                                                        "Quadratic teaming partner library "
                                                        "shown above. false if it's a market "
                                                        "suggestion not yet in the library "
                                                        "(UI will offer to add it)."
                                                    ),
                                                },
                                                "capability_focus": {
                                                    "type": ["string", "null"],
                                                    "description": (
                                                        "Short label for what this partner "
                                                        "brings (e.g., 'CRM platform vendor', "
                                                        "'HUBZone prime', 'Section 508 "
                                                        "specialist'). Used as the "
                                                        "good_fit_for hint when adding to "
                                                        "the library."
                                                    ),
                                                },
                                                "profile": {
                                                    "type": "object",
                                                    "description": (
                                                        "Detailed profile shown when the "
                                                        "user clicks the partner name in "
                                                        "the UI. Always populate this for "
                                                        "every suggestion."
                                                    ),
                                                    "properties": {
                                                        "overview": {
                                                            "type": "string",
                                                            "description": (
                                                                "2-3 sentences about the firm: "
                                                                "what they do, where they "
                                                                "operate, scale."
                                                            ),
                                                        },
                                                        "why_fits_this_project": {
                                                            "type": "string",
                                                            "description": (
                                                                "2-4 sentences on why this firm "
                                                                "specifically fits this RFP — "
                                                                "reference the customer, the "
                                                                "scope, and the gap being "
                                                                "addressed."
                                                            ),
                                                        },
                                                        "why_fits_quadratic": {
                                                            "type": "string",
                                                            "description": (
                                                                "2-4 sentences on capability "
                                                                "complementarity with Quadratic. "
                                                                "What does Quadratic bring; "
                                                                "what does the partner bring; "
                                                                "do their cultures/sizes/missions "
                                                                "fit; any prior collaboration."
                                                            ),
                                                        },
                                                        "key_capabilities": {
                                                            "type": "array",
                                                            "items": {"type": "string"},
                                                            "description": (
                                                                "5-10 capability bullets — what "
                                                                "this firm actually does well."
                                                            ),
                                                        },
                                                        "certifications_set_asides": {
                                                            "type": "array",
                                                            "items": {"type": "string"},
                                                            "description": (
                                                                "Known certifications and set-"
                                                                "aside statuses (8(a), HUBZone, "
                                                                "SDVOSB, ISO 27001, etc.). Empty "
                                                                "array if unknown."
                                                            ),
                                                        },
                                                        "typical_engagement_model": {
                                                            "type": "string",
                                                            "description": (
                                                                "How a Quadratic + this firm "
                                                                "engagement would be structured "
                                                                "(prime/sub split, who owns "
                                                                "platform vs implementation, "
                                                                "etc.)."
                                                            ),
                                                        },
                                                        "contact": {
                                                            "type": "object",
                                                            "description": (
                                                                "Contact info to help the user "
                                                                "reach out. ONLY populate fields "
                                                                "you're confident are publicly "
                                                                "correct. Use null for anything "
                                                                "uncertain — never guess. NEVER "
                                                                "invent specific person names "
                                                                "or direct phone numbers."
                                                            ),
                                                            "properties": {
                                                                "website": {
                                                                    "type": ["string", "null"],
                                                                    "description": (
                                                                        "Official corporate "
                                                                        "website (https://...). "
                                                                        "null if uncertain."
                                                                    ),
                                                                },
                                                                "primary_location": {
                                                                    "type": ["string", "null"],
                                                                    "description": (
                                                                        "City + state of HQ "
                                                                        "(e.g., 'Falls Church, "
                                                                        "VA'). null if "
                                                                        "uncertain."
                                                                    ),
                                                                },
                                                                "general_email": {
                                                                    "type": ["string", "null"],
                                                                    "description": (
                                                                        "Public business-"
                                                                        "development or general "
                                                                        "inquiry email "
                                                                        "(info@..., business@..., "
                                                                        "contact@...). null if "
                                                                        "uncertain. NEVER a "
                                                                        "specific named person."
                                                                    ),
                                                                },
                                                                "linkedin": {
                                                                    "type": ["string", "null"],
                                                                    "description": (
                                                                        "Company LinkedIn URL "
                                                                        "(https://linkedin.com/"
                                                                        "company/...). null if "
                                                                        "uncertain."
                                                                    ),
                                                                },
                                                            },
                                                        },
                                                    },
                                                    "required": [
                                                        "overview",
                                                        "why_fits_this_project",
                                                        "why_fits_quadratic",
                                                    ],
                                                },
                                            },
                                            "required": [
                                                "name",
                                                "fit_rationale",
                                                "from_library",
                                            ],
                                        },
                                    },
                                },
                                "required": [
                                    "approach",
                                    "proposal_language_draft",
                                    "honesty_check",
                                ],
                            },
                        },
                        "recommended_mitigation_index": {
                            "type": ["integer", "null"],
                            "description": "0-based index into mitigation_options. null for met.",
                        },
                        "no_bid_recommended": {
                            "type": "boolean",
                            "description": (
                                "True ONLY when ALL plausible mitigations would be "
                                "misleading or impossible. Triggers a no-bid banner on "
                                "the proposal page."
                            ),
                        },
                    },
                    "required": ["requirement_id", "verdict", "current_state"],
                },
            }
        },
        "required": ["items"],
    },
}


_SYSTEM = """You are the Shortfall Strategist for Quadratic Digital, a small public-sector-only software firm.

Your job: for each RFP compliance requirement in the batch, decide whether Quadratic meets it, partially meets it, or has a gap. For partial/gap items, draft mitigation language Quadratic can use directly in its proposal.

Quadratic's company profile and knowledge base are provided as static context (cached). The compliance batch is provided per call.

VERDICT RULES:
- "met" — the profile/KB provides clear, citable evidence that Quadratic satisfies this requirement.
- "partial" — Quadratic has analogous capability, but not exactly what's specified. Example: an RFP requires five years of state health-program experience while the profile supports analogous federal health work.
- "gap" — Quadratic does not meet this requirement and cannot honestly claim equivalent capability.

GAP SEVERITY / CATEGORY (for partial/gap) — these are flat buckets; pick ONE per item:

- "deal_breaker" — TRUE deal-breakers are RARE. Use this ONLY when ALL of the following are true:
  (a) Quadratic doesn't meet the requirement,
  (b) no equivalent-experience framing is defensible,
  (c) no in-progress plan can complete before performance start,
  (d) NO teaming partner — from the library OR the broader market — could honestly fill the gap, and
  (e) self-perform isn't realistic in the timeline (can't hire fast enough, no certification path that completes in time, no license/tool that could be acquired).
  When all five hold: set no_bid_recommended=true and mitigation_options=[].

  If teaming with a real partner WOULD honestly resolve the gap, classify it as MAJOR or TECHNICAL with a teaming mitigation. Do not use deal_breaker as a default for "Quadratic doesn't have this".

  Examples that are NOT deal-breakers (use teaming mitigation instead):
  - Missing certification or set-aside that a partner has (HUBZone, SDVOSB, 8(a), ISO 27001) → MAJOR + teaming with a qualifying partner.
  - Missing platform expertise (Salesforce, Slate, ServiceNow, Workday, Oracle) → TECHNICAL + teaming with the platform vendor or a certified implementation partner.
  - Geographic presence requirement Quadratic doesn't currently meet → MAJOR + teaming with an in-state firm or in-progress plan to open an office.
  - Past performance on a specific product/system Quadratic hasn't used → MAJOR/TECHNICAL + equivalent experience or teaming.
  - Required contract vehicle Quadratic isn't on (Alliant, OASIS+, GSA Schedule subset) → MAJOR + teaming with a vehicle holder.

  TRUE deal-breaker examples (rare):
  - Hard set-aside RFP (HUBZone-only, 8(a) sole source) AND no qualifying teaming partner exists AND no path to certification.
  - Specific TS/SCI clearance required AND no cleared partner available.
  - Explicit firm-level conflict of interest (RFP excludes the firm by name or relationship).
  - Mathematical impossibility (e.g., RFP requires 20 years in business and the firm is 8 years old AND no partner can satisfy the requirement on the firm's behalf).

- "major" — FIRM-RELATED gap that significantly affects scoring or compliance and is RESOLVABLE (via teaming, in-progress, equivalent experience, or self-perform). Examples: missing certification or contract vehicle that a partner has, business-size mismatch with a teaming workaround, geographic presence gap a partner fills.

- "minor" — FIRM-RELATED gap addressable with a brief note. Examples: minor administrative requirement, optional preference rather than hard requirement, weak point that's outweighed by other strengths.

- "technical" — TECHNICAL-CAPABILITY gap. Examples: RFP requires a specific platform Quadratic doesn't have (Salesforce, ServiceNow, specific CRM), specific methodology Quadratic doesn't practice, specific integration Quadratic hasn't built before, specific technical compliance framework (FedRAMP authorization, FISMA-High).

KEY DISTINCTION:
- major/minor are about WHO Quadratic IS as a firm.
- technical is about what Quadratic CAN BUILD or what platform Quadratic OPERATES.
- deal_breaker is the LAST RESORT — only when teaming + every other honest path is unavailable.

HONESTY CONSTRAINTS — non-negotiable:
1. NEVER claim a certification, clearance, or past performance Quadratic doesn't have. If you can't find it in the profile or KB, it doesn't exist for this analysis.
2. "Equivalent experience" framing is ONLY allowed when the equivalence is defensible. Examples:
   - DEFENSIBLE: commercial cloud migration ↔ government cloud migration (same technical work, different customer).
   - NOT DEFENSIBLE: commercial UI design ↔ Section 508 accessibility (different skill).
   - NOT DEFENSIBLE: commercial small business set-aside ↔ federal HUBZone (different program).
3. "In progress" framing is ONLY allowed when there is a CONCRETE plan with timeline. Without one, do not write "in progress" — write a true gap.
4. Teaming framing — when you suggest teaming, you MUST populate partner_suggestions with 3-5 SPECIFIC firm names. Two-step process:
   - FIRST, scan the teaming partner library in the cached prefix for partners whose good_fit_for or core_capabilities match this specific gap. Include those with from_library=true and the library entry's confirmed flag.
   - IF the library has no fitting partner for this specific gap (e.g., a higher-ed CRM platform vendor when the library only has general government IT primes), suggest 3-5 specific firms FROM YOUR TRAINING KNOWLEDGE that operate in the relevant market. Use real company names (Salesforce / Slate / Anthology / Ellucian for higher-ed CRM; specific HUBZone primes for HUBZone gaps; specific Section 508 specialists; etc.). Set from_library=false and confirmed=false for these. The user will get an "Add to library" button to persist confirmed prospects.
   - NEVER use placeholder names like "X", "TBD", "Partner A", "[partner name]". Always real firm names.
   - additional_action_required must reference the top suggestion: "Confirm teaming with [name] [NEEDS_HUMAN]".
   - PROFILE FIELD: For EVERY partner suggestion, populate the profile object with overview, why_fits_this_project (specific to this RFP and gap), why_fits_quadratic (capability complementarity), key_capabilities, certifications_set_asides (empty if unknown), and typical_engagement_model. For library partners, draw from the library entry's data. For market suggestions, draw from your training knowledge — be honest about uncertainty. The user will see this profile when they click the partner name in the UI.
   - CONTACT FIELD: Inside profile.contact, fill ONLY fields you're confident are publicly correct: website, primary_location (city + state), general_email (info@/business@/contact@ style — never a specific named person), linkedin company URL. NEVER invent specific person names or direct phone numbers. Use null when uncertain — null is always better than a guess.
5. If no honest mitigation exists, set verdict="gap", gap_severity="deal_breaker", mitigation_options=[], no_bid_recommended=true.
6. NON-TEAMING ALTERNATIVE REQUIRED. Whenever you propose teaming as a mitigation option, you MUST also include at least one non-teaming option in the same mitigation_options array. The user shouldn't be locked into needing a partner to bid. Non-teaming alternatives include:
   - CUSTOM BUILD: propose Quadratic builds the requested capability bespoke (see QUADRATIC'S POSITIONING section). Use when RFP describes a CAPABILITY without mandating a specific product brand AND timeline supports greenfield. Lead with tailored fit + IP ownership + no licensing + Section 508 baked in.
   - SELF-PERFORM: hire the missing role, build the missing capability internally, or buy a license/tool. Be specific about what Quadratic would do (e.g., "Hire a senior Salesforce architect before performance start; budget for ~$200K/yr loaded").
   - EQUIVALENT EXPERIENCE: position Quadratic's analogous capability with honest framing (apply the same defensibility test as rule #2).
   - IN-PROGRESS: concrete plan with timeline — only when a real plan exists (rule #3).
   - ACKNOWLEDGE + RISK-FRAME: name the gap honestly and explain how Quadratic manages the risk (e.g., "Quadratic does not currently hold ISO 27001; mitigation is to map our existing NIST 800-53 controls to the ISO framework and document the gap explicitly to the evaluator").
   The non-teaming alternative does NOT have to be the recommended choice — it just has to be available so the user has a Plan B. Set recommended_mitigation_index to whichever option you genuinely think is strongest.

   ONLY skip the non-teaming alternative when the gap is STRUCTURALLY IMPOSSIBLE to fill without a partner — meaning a non-teaming option would necessarily be misleading. Examples:
   - RFP requires a specific business set-aside Quadratic doesn't qualify for (HUBZone, SDVOSB, 8(a) sole source) — earning the certification mid-bid is not realistic.
   - RFP requires a specific commercial product license (Slate, ServiceNow) that Quadratic doesn't resell.
   When you skip the non-teaming alternative, you MUST explain in current_state why no honest non-teaming alternative exists ("No non-teaming alternative — RFP is HUBZone set-aside and Quadratic is not HUBZone-certified.").

   APPROACH FIELD VALUES — strict. When you emit the `approach` field on each mitigation_option, use ONE of these exact lowercase hyphenated tokens (the schema enforces this):
     self-perform | teaming | equivalent-experience | in-progress | custom-build | acknowledge-and-risk-frame | no-bid
   Do NOT compose strings like "teaming with Example Partner" — for teaming, set approach="teaming" and put the partner names inside partner_suggestions[]. Same for any other approach: the type is in `approach`; the specifics are in the surrounding fields.

PAST DECISIONS LEDGER:
The PAST DECISIONS LEDGER block in the cached prefix is Quadratic's accumulated memory of how prior gaps were resolved. For each compliance item:
- Check whether any past decision's "applies_to_gaps_like" describes a similar requirement (semantic match — same kind of gap, doesn't have to be exact wording).
- If yes, factor that decision into your analysis. Reference the decision id in current_state ("Past decision DEC-NNN established that ..."). Shape the recommended mitigation around the established practice.
- Only override a past decision if there's a clear contextual reason (different state, different agency, materially different requirement). Note the override reason in current_state.
- If you cite a past decision, mention its id (DEC-NNN) in current_state so the user can trace it.

CITATION RULES (per design doc §7.1):
- Past performance citations may only reference KB docs of class past_performance_won or past_performance_subbed, OR entries in the profile's past_performance array. NEVER cite a prior_proposal_* doc as completed work — those are voice-grounding only.
- The KB context provided to you EXCLUDES non-citable classes (prior_proposal_*, agency_context, procurement_craft, boilerplate). Anything you can see is fair game to cite.

QUADRATIC SCALE — use only the staff and scale stated in the active profile. Do NOT invent personnel, "hundreds of cleared engineers", or capabilities that aren't in the profile/KB. Authentic small-business framing wins more than inauthentic scale claims.

QUADRATIC'S POSITIONING — RAPID CUSTOM DEVELOPMENT IS A COMPETITIVE EDGE:
Many RFPs default to COTS (Commercial Off-The-Shelf) language because the writers haven't caught up to how fast modern custom development has become. With AI-assisted dev cycles (Claude/Copilot/Cursor) plus modern frameworks, small firms like Quadratic can deliver tailored solutions on timelines that previously required buying a product. This is one of Quadratic's competitive differentiators.

When an RFP requires a CAPABILITY (CRM, document management, ticketing, reporting, case management, portal) WITHOUT mandating a specific product brand, "custom build" is a legitimate non-teaming mitigation that often beats COTS on the procurement's own scoring criteria. Frame it with these HONEST advantages:
- Tailored fit to the agency's specific workflows (vs configuring a generic platform)
- Integration freedom (no vendor lock-in; REST/GraphQL APIs designed for the agency's existing stack)
- IP ownership (agency owns the code post-contract; no per-seat licensing or recurring SaaS fees)
- Modern dev velocity (AI-assisted cycles, ~30-50% faster than 3 years ago)
- Section 508 / accessibility built in from the ground up vs retrofitted on a vendor's platform

USE custom-build mitigation when ALL of these apply:
- RFP describes the CAPABILITY without mandating a specific product or vendor
- Period of performance is realistic for greenfield (typically 6+ months for an MVP)
- Quadratic's tech stack matches what would be built (Python / Java / .NET, React / Angular, AWS GovCloud / Azure Government — all in the company profile)
- The capability isn't a specialized domain Quadratic lacks expertise in (e.g., highly-regulated EHR with HIPAA-specific certifications Quadratic doesn't have)

DO NOT propose custom-build when:
- RFP explicitly names a required product/vendor ("must be Salesforce", "Slate is required")
- Required integrations only exist for specific COTS products (e.g., must integrate with a state's existing Salesforce instance)
- Timeline is too short for greenfield work (e.g., 90 days to full production)
- A COTS solution is genuinely the right answer for the customer's use case (rare, but possible — be honest)

Where appropriate, propose custom-build ALONGSIDE teaming (not instead of). The two are complementary: teaming gives the customer a known platform; custom-build gives them tailored fit + IP ownership. Letting the user choose between them is more valuable than picking only one path.

OUTPUT:
- Use the report_gap_analyses tool with one item per input requirement.
- For met items: verdict, current_state, evidence_citations are required. mitigation_options can be empty.
- For partial/gap items: provide 2-3 mitigation options where possible. Mark the recommended one via recommended_mitigation_index.
- requirement_id MUST exactly match what's in the input batch.
"""


_USER_TEMPLATE = """=== COMPLIANCE REQUIREMENTS TO ANALYZE — batch of {n_items} ===

{requirements_text}

For each requirement above, call report_gap_analyses with the verdict, current_state, evidence_citations, and (for partial/gap items) mitigation_options. Use the requirement_id values exactly as given."""


_CACHED_PREFIX_TEMPLATE = """=== QUADRATIC DIGITAL COMPANY PROFILE (canonical) ===
{profile_json}

=== TEAMING PARTNER LIBRARY (use for partner_suggestions in teaming mitigations) ===
{teaming_partners_json}

=== PAST DECISIONS LEDGER (cross-RFP institutional memory) ===
{decisions_text}

=== QUADRATIC DIGITAL KNOWLEDGE BASE (citable evidence) ===
{kb_context}
"""


@dataclass
class ShortfallItem:
    requirement_id: str
    verdict: str  # "met" | "partial" | "gap"
    current_state: str
    evidence_citations: list[str]
    gap_severity: str | None
    mitigation_options: list[dict]
    recommended_mitigation_index: int | None
    no_bid_recommended: bool


_BATCH_SIZE = 25


def make_batches(items: list, size: int = _BATCH_SIZE) -> list[list]:
    """Chunk items into batches of `size`. If the final chunk would
    have fewer than half `size` items, merge it into the prior batch —
    a 1-item batch pays the same cached_prefix read (~$0.025 + ~6
    seconds latency) as a 25-item batch, so absorbing avoids wasteful
    tiny tails. 26 items in one batch is well within Sonnet's
    max_tokens budget (32K vs ~640 tokens/item average).
    """
    batches = [items[i : i + size] for i in range(0, len(items), size)]
    if len(batches) >= 2 and len(batches[-1]) < size // 2:
        batches[-2].extend(batches[-1])
        batches.pop()
    return batches


def build_cached_prefix(
    *,
    profile_json: str,
    kb_context: str,
    teaming_partners_json: str,
    decisions_text: str,
) -> str:
    return _CACHED_PREFIX_TEMPLATE.format(
        profile_json=profile_json,
        teaming_partners_json=teaming_partners_json,
        decisions_text=decisions_text,
        kb_context=kb_context,
    )


def _format_requirements(requirements: list[dict]) -> str:
    lines: list[str] = []
    for r in requirements:
        line = (
            f"REQ-ID: {r['requirement_id']}\n"
            f"  Text: {r['requirement_text']}\n"
            f"  Type: {r['requirement_type']}, Category: {r['category']}"
        )
        if r.get("weight"):
            line += f", Weight: {r['weight']}"
        src = r.get("source_doc", "")
        if r.get("source_section"):
            src += f" §{r['source_section']}"
        if r.get("source_page"):
            src += f" p.{r['source_page']}"
        if src:
            line += f"\n  Source: {src}"
        lines.append(line)
    return "\n".join(lines)


def analyze_compliance_batch(
    *,
    proposal_id: int,
    requirements: list[dict],
    cached_prefix: str,
) -> list[ShortfallItem]:
    """Run the Shortfall Strategist on one batch of compliance requirements.

    `cached_prefix` is the static profile + KB context — built once per
    proposal and passed unchanged to every batch. Anthropic's ephemeral
    prompt cache makes the prefix cheap to reuse across batches in the
    same proposal.
    """
    settings = get_settings()
    client = get_anthropic()

    user_prompt = _USER_TEMPLATE.format(
        n_items=len(requirements),
        requirements_text=_format_requirements(requirements),
    )

    # max_tokens is a cap, not a budget — costs scale with actual output.
    # 16000 was tight: a 25-item batch with rich mitigation_options can
    # exceed 16K and silently truncate (stop_reason='max_tokens', tool
    # input arrives empty, the whole batch returns 0 analyses). 32000
    # gives ~1280 tokens/item headroom; well under Sonnet's streaming
    # ceiling.
    tool_input, usage = client.call_tool(
        model=settings.model_drafter,  # Sonnet
        system=_SYSTEM,
        cached_prefix=cached_prefix,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=32000,
        agent_name="shortfall_strategist",
        proposal_id=proposal_id,
    )

    raw_items = tool_input.get("items", [])
    log.info(
        "shortfall_strategist: %d requirements -> %d analyses, %s stop=%s",
        len(requirements),
        len(raw_items),
        fmt_llm_usage(usage),
        usage.get("stop_reason"),
    )

    # Truncation detection. If the model hit max_tokens AND the parsed
    # tool_use is empty, the JSON arguments were chopped mid-stream and
    # we got nothing usable. Fail loudly so the intake job's error path
    # surfaces a stage message (instead of silently dropping ~$0.27 of
    # work and N missing gap analyses). Raising here rolls the batch up
    # to the caller, which logs and can decide whether to skip-and-
    # continue or abort. Empty-but-clean returns (stop != max_tokens)
    # are valid — the model legitimately judged 0 gaps.
    if usage.get("stop_reason") == "max_tokens" and not raw_items and len(requirements) > 0:
        raise RuntimeError(
            f"shortfall_strategist: batch of {len(requirements)} items "
            f"truncated at max_tokens (out={usage['output_tokens']}). "
            f"No analyses parsed. Reduce batch size or raise max_tokens."
        )

    extracted: list[ShortfallItem] = []
    for item in raw_items:
        try:
            extracted.append(
                ShortfallItem(
                    requirement_id=str(item["requirement_id"]),
                    verdict=str(item.get("verdict", "gap")),
                    current_state=str(item.get("current_state", "")),
                    evidence_citations=list(item.get("evidence_citations") or []),
                    gap_severity=item.get("gap_severity"),
                    mitigation_options=list(item.get("mitigation_options") or []),
                    recommended_mitigation_index=item.get("recommended_mitigation_index"),
                    no_bid_recommended=bool(item.get("no_bid_recommended", False)),
                )
            )
        except (KeyError, TypeError) as exc:
            log.warning("shortfall_strategist: skipping malformed item %r: %s", item, exc)
    return extracted
