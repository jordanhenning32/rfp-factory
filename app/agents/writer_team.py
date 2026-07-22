"""Writer Team — drafts ONE proposal section per call.

Per design doc §6.4. The Writer reads:
- A cached prefix shared across every section: profile + teaming + decisions
  + KB context + the full outline + every compliance item + every gap
  analysis with the user's chosen mitigations + RFP text excerpts
- A per-section user prompt that names the section and reminds the Writer
  which compliance items + gap mitigations belong here

It returns:
- draft_text_markdown — the section content in markdown, with [^cite-N]
  inline citation markers and [NEEDS_HUMAN: …] inline placeholders
- citations[] — structured list keyed to the markers in the draft
- needs_human_placeholders[] — structured list keyed to the inline markers
- shortfall_mitigations_applied[] — the gap_ids whose proposal_language
  this section actually used (so the user can verify their selections
  landed where they expected)

Hard constraints baked into the system prompt:
- Past performance citations may ONLY trace to KB docs of class
  past_performance_won / past_performance_subbed.
- Never invent personnel, certifications, customer names, dates, or numbers.
- Use [NEEDS_HUMAN] for any specific commitment Quadratic shouldn't make
  unilaterally (final pricing numbers, exact FTE counts, partner-confirmed
  status, schedule commitments tied to dates).
- Apply the user's selected gap mitigations verbatim where their
  proposal_language_draft fits; adapt for voice but don't override the
  honesty framing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings
from app.services.lessons import format_writer_guidance
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


def format_held_certifications_block(profile: dict) -> str:
    certs = profile.get("certifications") or []
    if not certs:
        return ""
    lines = ["=== HELD CERTIFICATIONS — ALLOWLIST (claim ONLY these, no exceptions) ==="]
    for c in certs:
        lines.append("- " + c)
    lines.append("")
    return "\n".join(lines)


_TOOL: dict = {
    "name": "report_section_draft",
    "description": (
        "Report the drafted section. Required: markdown text, citations, "
        "needs_human_placeholders, applied gap_ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "draft_text_markdown": {
                "type": "string",
                "description": (
                    "Section content as markdown. Standard headings "
                    "(##, ###), bullets, emphasis. Insert [^cite-N] at "
                    "factual claims and [NEEDS_HUMAN: brief description] "
                    "at uncommittable spots — both must have matching "
                    "array entries below."
                ),
            },
            "citations": {
                "type": "array",
                "description": (
                    "One entry per [^cite-N] in the draft. Number sequentially: cite-1, cite-2, ..."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "marker": {
                            "type": "string",
                            "description": ("Marker without brackets — must match a [^marker] in the draft."),
                        },
                        "claim": {
                            "type": "string",
                            "description": (
                                "Paraphrase of what's cited. Max 15 words. "
                                "Sentence fragments OK. Do not restate "
                                "the entire claim — the citation pairs "
                                "with the sentence in the draft."
                            ),
                        },
                        "source_kb_doc": {
                            "type": "string",
                            "description": (
                                "'KB DOC #N filename' or "
                                "'company_profile.<field>'. Past-perf "
                                "claims MUST cite past_performance_won "
                                "or past_performance_subbed."
                            ),
                        },
                        "source_section": {
                            "type": ["string", "null"],
                            "description": (
                                "OMIT (return null) unless you have an "
                                "exact section number or page reference "
                                "from the source. Do NOT paraphrase the "
                                "location. Most citations should be null."
                            ),
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["HIGH", "MEDIUM", "LOW"],
                            "description": (
                                "HIGH=source states it explicitly. MEDIUM=small inference. LOW=analogous."
                            ),
                        },
                    },
                    "required": ["marker", "claim", "source_kb_doc", "confidence"],
                },
            },
            "needs_human_placeholders": {
                "type": "array",
                "description": ("One entry per [NEEDS_HUMAN: …] marker in the draft."),
                "items": {
                    "type": "object",
                    "properties": {
                        "marker_text": {
                            "type": "string",
                            "description": ("Exact text inside the [NEEDS_HUMAN: …] brackets in the draft."),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "One short sentence: what input is "
                                "needed. No meta-explanation about why "
                                "you didn't fill it — that's implicit."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "pricing",
                                "schedule_commitment",
                                "teaming_confirmation",
                                "specific_personnel",
                                "specific_numbers",
                                "policy_decision",
                                "signature",
                                "other",
                            ],
                            "description": (
                                "Bucket for the Needs Human Input tab. "
                                "Use 'signature' for any wet/electronic "
                                "signature placeholder."
                            ),
                        },
                    },
                    "required": ["marker_text", "description", "category"],
                },
            },
            "shortfall_mitigations_applied": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of gap_id values (e.g. 'GAP-007') this "
                    "section actually applied. Empty array if none."
                ),
            },
        },
        "required": [
            "draft_text_markdown",
            "citations",
            "needs_human_placeholders",
            "shortfall_mitigations_applied",
        ],
    },
}


_SYSTEM = """You are a senior proposal writer for Quadratic Digital, a small public-sector software firm. You draft ONE section of an RFP response per call.

OUTPUT DISCIPLINE — NO PREAMBLE. Respond ONLY by calling the report_section_draft tool. Do not write "Here's the section…" or "I'll draft this now…" or any acknowledgment text. Do not narrate your reasoning. Every output token outside the tool call costs Sonnet's $15/MTok output rate for nothing — go straight to the tool. Inside the tool, keep claim paraphrases under 15 words, omit source_section unless you have a real section/page reference, and write needs_human descriptions as ONE short sentence.

The cached prefix gives you:
- Quadratic's canonical company profile
- The teaming partner library
- The cross-RFP decisions ledger (institutional memory)
- The full compliance matrix (every requirement — kept here for cross-section awareness so you can avoid duplicating coverage)

The PER-CALL user message gives you, scoped to THIS section:
- The section to draft and its brief
- A compact outline snippet (sibling sections — what other sections cover so you don't duplicate)
- Just the gaps assigned to this section, with the user's chosen mitigations
- A scoped knowledge base excerpt — only the past performance / personnel / references / corporate / compliance-evidence docs that pertain to this section's claims. If you need a citation that isn't in this scoped excerpt, lean on the compliance matrix (which carries source citations) rather than inventing language.
- A focused RFP excerpt — Section L/M-style governance plus paragraphs whose terms match this section's brief and assigned compliance items. Use this to mirror evaluator language and cite verbatim phrasing.

Per call you will be told WHICH section to draft. Stay strictly within that section's scope as defined in its section_brief.

WRITING STYLE — Quadratic's voice:
- Confident, direct, plain English. No marketing fluff.
- Authentic small-business framing. Quadratic has ~12 named key personnel; do NOT imply hundreds of staff.
- Lead with capability and outcome, not company history.
- "We" voice for Quadratic. Use third-person for partners ("Example Teaming Partner will…").
- Tight prose: short paragraphs, scannable bullet lists, headings. Evaluators are reading dozens of submissions.
- EVALUATION-CRITERION MIRRORING — required when the section_brief opens with a "This section targets [Criterion ID] (...)" sentence. Echo that criterion's language explicitly in the section's first paragraph or topic-sentence position, in Quadratic's voice. Example: brief says "This section targets M.2.3 (Innovation, 25 pts) — evaluator wants concrete examples of AI-driven delivery acceleration." Open with something like: "Quadratic addresses Section M.2.3's innovation focus through measured AI-driven delivery acceleration on each engagement: …" Evaluators scoring against a rubric scan for criterion language; mirroring it earns easy points. When the brief explicitly says the section targets no scored criterion, skip the mirror.

CITATIONS — every factual claim must be traceable:
1. Inline marker pattern: insert [^cite-N] at the end of each sentence containing a factual claim. Number sequentially per section: cite-1, cite-2, …
2. Every marker must have a matching entry in the citations array with claim, source_kb_doc, source_section (optional), confidence.
3. PAST-PERFORMANCE CLAIMS RULE — non-negotiable. Any claim of the form "Quadratic delivered X" or "Quadratic completed Y for Customer Z" must cite a KB doc of class past_performance_won or past_performance_subbed (visible in the cached KB context with [past_performance_won] or [past_performance_subbed] tag), OR the past_performance array in the company profile. NEVER cite a prior_proposal_* doc as completed work. The KB context provided to you EXCLUDES non-citable classes, but do not invent past performance from your training knowledge either — if it's not in the cited inputs, it doesn't exist for this proposal.
4. Capability claims (we have personnel with X skill, we operate platform Y, we hold cert Z) cite either company_profile.<field> or a KB doc.
5. NEVER cite a customer, project, dollar amount, year, or person's name that doesn't appear in the cached inputs.
6. If you can't cite a claim, REWRITE THE SENTENCE so it doesn't make that claim. Don't write the unprovable claim and skip the citation.

GAP MITIGATIONS — the user has already decided how to address each gap:
- Read the gap analyses in the cached prefix. Each has selected_mitigation_index (and possibly selected_partner_name and resolution_notes).
- The section_brief tells you which gap_ids belong to this section.
- For each assigned gap, pull the proposal_language_draft from the chosen mitigation_option (option index = selected_mitigation_index, or recommended_mitigation_index if none was selected). Adapt for flow and voice — but PRESERVE the honesty framing.
- DO NOT replace a teaming mitigation with a custom-build one or vice versa. The user's choice is binding.
- The CHOSEN MITIGATION's proposal_language_draft is the upper bound on what this section promises about the gap. Do NOT promote 'extended via a configurable module' to 'fully supported', 'in-progress' to 'certified', or 'subcontracted' to 'in-house'. Adapt for flow and voice, but PRESERVE the scope and honesty framing. If the proposal_language_draft feels too cautious, that is INTENTIONAL — the user explicitly picked the conservative framing. Stay within it.
- If a teaming partner was selected, name that partner specifically in the prose.
- After drafting, list the gap_ids you applied in shortfall_mitigations_applied.

APPROVED TEAM ROSTER — when an "=== APPROVED TEAM ROSTER ===" block appears in the cached prefix, USE the named persons, time-allocation percentages, labor categories, and bios DIRECTLY in your prose. The user has already approved this composition. Do NOT emit [NEEDS_HUMAN] for any staffing percentage, role assignment, named person, or labor category that the roster covers — that information is already committed and asking again wastes the user's time. Pull names verbatim, quote allocations as written ("Alex Rivera (50% PM)" not "[NEEDS_HUMAN: PM time allocation]"), and reference roster bios when describing qualifications. When the roster block is ABSENT, the user has not yet approved a team — fall back to the conservative NEEDS_HUMAN behavior below.

APPROVED COST BUILD — when an "=== APPROVED COST BUILD ===" block appears in the cached prefix, USE the proposed price, ODC line items, and lifecycle phases DIRECTLY in your prose. Quote the total proposed price verbatim ("our $1,250,000 proposed price" not "[NEEDS_HUMAN: total bid amount]"). Reference ODC items by name + amount ("cloud hosting at $60,000/year" not "[NEEDS_HUMAN: hosting cost]"). Use the named lifecycle phases and their month ranges in any project-narrative section. Do NOT emit [NEEDS_HUMAN] for anything the cost build has already committed.

PLACEHOLDER DISCIPLINE — REDUCE [NEEDS_HUMAN] BY USING WHAT'S ALREADY DECIDED. Before emitting any [NEEDS_HUMAN] placeholder, FIRST check whether the answer is already in your cached context: company profile (certifications, contract vehicles, key personnel, past performance, capability areas), past decisions ledger, approved team roster, approved cost build. If it's there, USE IT. Specifically:

- Pricing / ODCs / phase structure → cost build has it; use verbatim, NEVER placeholder.
- Staffing percentages / personnel names / labor categories → team roster has it; use verbatim.
- Generic schedule references → use TIME-RELATIVE phrasing ("within 30 days of contract award", "in the first 60 days") instead of asking for an absolute date.
- Pre-flight verification items (registrations, attached agreements, executed teaming) → these belong on the SUBMISSION CHECKLIST tab, not in narrative. Phrase the section as if the verification has already happened; do NOT request it inline as a [NEEDS_HUMAN].
- Compliance certifications → use ONLY what's in company_profile.certifications. If a certification is not in the profile, do NOT emit a [NEEDS_HUMAN] asking the user to confirm — REWRITE the sentence to make a claim that IS supportable.

HELD CERTIFICATIONS — ABSOLUTE RULE
The company's held certifications are listed verbatim in the HELD CERTIFICATIONS — ALLOWLIST block in the cached prefix. Claim ONLY those credentials as 'held'. NEVER invent. NEVER extrapolate. NEVER promote a target / in-progress / planned credential to 'certified' / 'compliant' / 'in compliance with'. If the RFP asks for a credential not on the allowlist, address it via the assigned gap mitigation and surface the lack via [NEEDS_HUMAN: confirm <cert> attestation status] when no mitigation on file. SOC 2, NIST 800-53, PCI-DSS, FISMA, FedRAMP High, ISO 27001 etc. are NOT held unless in the allowlist.

EXAMPLES OF WRONG vs RIGHT (apply these patterns rigorously — they cover the four placeholder buckets that drove the user's original complaint):

✗ WRONG: "We allocate [NEEDS_HUMAN: % time PM] of PM time to the engagement."
✓ RIGHT: "We allocate Alex Rivera (Project Manager III) at 50% time over the 12-month period of performance — sourced from the approved team roster."

✗ WRONG: "Our proposed price is [NEEDS_HUMAN: total bid amount]."
✓ RIGHT: "Our proposed price is $1,250,000 over the 12-month period of performance."

✗ WRONG: "Hosting will run on [NEEDS_HUMAN: confirm AWS GovCloud reseller margin and Microsoft license assumptions before submission]."
✓ RIGHT: "Cloud hosting is $60,000/year, confirmed in the cost build's ODC schedule. Licensing assumptions are documented in our cost narrative."

✗ WRONG: "We will commence delivery on [NEEDS_HUMAN: kickoff date]."
✓ RIGHT: "We will commence delivery within 30 days of contract award, with kickoff governance established in the first 10 business days."

✗ WRONG: "[NEEDS_HUMAN: verify buyer-portal registration before submission]"
✓ RIGHT: (No placeholder and no unsupported claim. This verification belongs only on the Submission Checklist.)

✗ WRONG: "Jamie Chen will lead the architecture ([NEEDS_HUMAN: confirm Jamie's background])."
✓ RIGHT: "Jamie Chen leads the architecture work — using the qualifications stated in the approved team-roster bio."

✗ WRONG: "Phase 1 will run [NEEDS_HUMAN: phase 1 duration]."
✓ RIGHT: "Phase 1: Discovery & Planning runs M1-M3 (3 months) per the approved cost build's phase structure."

The goal: a draft that lands AS IF the proposal is ready to submit, with [NEEDS_HUMAN] reserved ONLY for items genuinely uncommittable from existing context — externally-verified compliance attestations, partner confirmations not yet in the profile, named individual quotes/testimonials, and signatures (which the system auto-resolves to the CEO's name post-draft).

[NEEDS_HUMAN] PLACEHOLDERS — use these for things you shouldn't commit Quadratic to (and ONLY when the cached prefix doesn't already commit them):
- Specific dollar amounts or pricing figures → category "pricing" (Cost Analysis Agent decides these, Weeks 12-13).
- Specific calendar commitments tied to a date you can't verify → category "schedule_commitment". When the user has approved phase durations in the roster or outline, use those instead of asking.
- Specific FTE counts beyond what the company profile / staffing plan / approved team roster supports → category "specific_numbers".
- Final teaming partner confirmation status → category "teaming_confirmation". Even when a partner is suggested, write [NEEDS_HUMAN: confirm teaming agreement with Example Teaming Partner executed before submission].
- Named individual quotes or testimonials → category "specific_personnel". (Personnel NAMES from the approved team roster are NOT placeholders — only quotes/testimonials need confirmation.)
- Wet or electronic SIGNATURES (cover letter, transmittal page, certifications) → category "signature". The UI gives the user an inline "Sign" button for these — keep marker_text short and natural ("authorized representative signature", "CEO signature on cover page").
- Anything the user explicitly flagged in resolution_notes as needing follow-up → category "policy_decision" or "other".

Inline pattern: [NEEDS_HUMAN: short description of what's needed]. Every inline placeholder must have a matching entry in needs_human_placeholders with marker_text, description, category. The marker_text in the JSON must match the text inside the brackets in the draft EXACTLY — the UI does literal string replacement when the user resolves it.

PAST DECISIONS LEDGER — the cross-RFP institutional memory in the cached prefix may have decisions that apply to this section. If a decision's "applies_to_gaps_like" or scope describes language patterns, voice choices, or framing that fits this section, apply it. Reference the decision id (DEC-NNN) in a citation if you used it materially.

USER DIRECTIVE — if the per-call user prompt contains a "USER DIRECTIVE" block, treat it as binding revision guidance from the human reviewer. Apply it to your draft of THIS section while preserving the honesty constraints, citation rules, and the assigned compliance items. If the directive conflicts with an honesty rule (e.g., "claim we have FedRAMP High" when we don't), do NOT comply — instead surface the conflict in a [NEEDS_HUMAN] placeholder explaining why you didn't apply that part of the directive.

LENGTH — respect word_limit STRICTLY. The Outline Agent now sets word_limit on every section (the only nulls are genuinely-open-ended sections), so if a number is given, treat it as a hard cap. Land within ±10% of word_limit; over-writing is the most common reason drafts read as bloated and is also the most common cost-overrun. Concise wins; padding loses. If you cannot fit the required content under word_limit, drop lower-leverage sentences first; do NOT exceed it.

OUTPUT — call report_section_draft. No preamble; tool call is the entire response.
"""


_CACHED_PREFIX_TEMPLATE = """=== QUADRATIC DIGITAL COMPANY PROFILE (canonical) ===
{profile_json}

=== TEAMING PARTNER LIBRARY ===
{teaming_partners_json}
{held_certifications_block}{team_roster_block}{cost_build_block}{framing_block}
=== PAST DECISIONS LEDGER (cross-RFP institutional memory) ===
{decisions_text}

=== COMPLIANCE MATRIX (every requirement — stays in cache for cross-section awareness) ===
{compliance_text}
{orientation_block}"""


_COTS_ORIENTATION_BLOCK = """
=== PROPOSAL ORIENTATION FLAGS ===
proposal.cots_orientation = TRUE

This RFP requests COTS / off-the-shelf / commercial product solutions. The cots_positioning rule in company_profile._usage_notes_for_agents is now MANDATORY for every section that touches solution approach, delivery model, timeline, or risk discussion. Lead with timeline + risk parity, then differentiate on fit-to-workflow. Do not concede the lane to COTS competitors. Do not frame custom development as inherently riskier than COTS — Quadratic's AI-accelerated delivery model is the answer to that historical objection.
"""


_USER_TEMPLATE = """{directive_block}Draft section {section_id} now.

Title: {section_title}
Order: {section_order}
Page limit: {page_limit}
Word limit: {word_limit}

Section brief:
{section_brief}

This section is responsible for these compliance items: {compliance_items}
{prior_resolutions_block}
=== PROPOSAL OUTLINE (sibling sections — what other sections cover so you don't duplicate) ===
{outline_snippet}

=== GAPS ASSIGNED TO THIS SECTION ===
{gaps_for_section}

=== KNOWLEDGE BASE — citable evidence relevant to this section ===
{kb_context_excerpt}

=== RELEVANT RFP EXCERPTS (Section L/M-style governance + paragraphs matching this section's brief) ===
{rfp_excerpt}

Use the report_section_draft tool to return the markdown plus structured citations, needs_human placeholders, and the list of gap_ids whose mitigation language you actually applied."""


_DIRECTIVE_TEMPLATE = """USER DIRECTIVE — apply this revision guidance to your draft of this section:
\"\"\"
{directive}
\"\"\"

"""


def _format_prior_resolutions_block(prior_resolved: list[dict] | None) -> str:
    """Render the user's prior [NEEDS_HUMAN] resolutions as a prompt block
    so the writer bakes the values directly into the new prose instead of
    re-emitting the same markers. The user does NOT want to be asked
    twice for the same input — this block is the load-bearing instruction
    for that. The carry-forward post-processor in services.needs_human is
    the safety net for when the model ignores it anyway."""
    if not prior_resolved:
        return ""
    actionable = [
        ph
        for ph in prior_resolved
        if ph.get("resolved")
        and ph.get("resolution_kind") in ("edit", "signature", "reject")
        and ph.get("marker_text")
    ]
    if not actionable:
        return ""
    lines: list[str] = []
    for ph in actionable:
        kind = ph["resolution_kind"]
        marker = ph["marker_text"]
        category = ph.get("category") or "other"
        value = (ph.get("resolution_value") or "").strip()
        if kind == "reject":
            lines.append(
                f'- [{category}] "{marker}" — user DECLINED to provide this. '
                f"Rewrite the prose so it does not request or imply a value here."
            )
        elif kind == "signature":
            lines.append(
                f'- [{category}] "{marker}" — user supplied signature: '
                f'"{value}". Place this string verbatim where the signature belongs.'
            )
        else:
            lines.append(
                f'- [{category}] "{marker}" — user supplied: "{value}". '
                f"Bake this value into the prose verbatim."
            )
    return (
        "\n=== PREVIOUSLY RESOLVED HUMAN INPUTS — DO NOT RE-EMIT THESE MARKERS ===\n"
        "The user already provided answers for these placeholders in a prior "
        "draft of this section. Bake the supplied values into the markdown "
        "directly. Do NOT emit a [NEEDS_HUMAN: ...] marker that asks for the "
        "same information again — the user explicitly does not want to be "
        "asked twice. New placeholders are fine for genuinely new uncommittable "
        "items, but reuse these prior answers verbatim:\n\n" + "\n".join(lines) + "\n"
    )


@dataclass
class SectionDraft:
    draft_text_markdown: str
    citations: list[dict]
    needs_human_placeholders: list[dict]
    shortfall_mitigations_applied: list[str]


def build_cached_prefix(
    *,
    profile_json: str,
    teaming_partners_json: str,
    decisions_text: str,
    compliance_text: str,
    cots_orientation: bool = False,
    held_certifications_block: str = "",
    team_roster_block: str = "",
    cost_build_block: str = "",
    framing_block: str = "",
) -> str:
    """Build the SHARED cached prefix passed to every draft_section
    call within one Writer Team run. Holds only content that's truly
    common across sections — the full compliance matrix (for cross-
    section awareness), the company profile, the teaming library, the
    user-approved team roster (when present), the proposed-scenario
    cost build (when present), the user-set framing (when present),
    and the cross-RFP decisions ledger. Per-section content (KB scope,
    assigned gaps, sibling outline, RFP excerpt) lives in the user
    prompt instead, so the cached prefix is small enough that prefill
    cost is dominated by reads, not writes.

    `team_roster_block`, `cost_build_block`, and `framing_block` are
    rendered by their respective service helpers; each is empty when
    the user hasn't engaged with the corresponding upstream surface
    (Phase 2B reorder ensures the team and cost gates are populated
    before the writer runs; framing is optional and surfaces from
    the Gaps tab).
    """
    orientation_block = _COTS_ORIENTATION_BLOCK if cots_orientation else ""

    def _bracket(block: str) -> str:
        # Bracket non-empty blocks with blank lines so they read as
        # distinct sections; empty strings pass through cleanly.
        return "\n" + block.rstrip() + "\n" if block.strip() else ""

    return _CACHED_PREFIX_TEMPLATE.format(
        profile_json=profile_json,
        teaming_partners_json=teaming_partners_json,
        held_certifications_block=_bracket(held_certifications_block),
        team_roster_block=_bracket(team_roster_block),
        cost_build_block=_bracket(cost_build_block),
        framing_block=_bracket(framing_block),
        decisions_text=decisions_text,
        compliance_text=compliance_text,
        orientation_block=orientation_block,
    )


def draft_section(
    *,
    proposal_id: int,
    section_id: str,
    section_title: str,
    section_order: int,
    section_brief: str,
    compliance_item_ids: list[str],
    assigned_gap_ids: list[str],
    page_limit: int | None,
    word_limit: int | None,
    cached_prefix: str,
    rfp_excerpt: str = "",
    kb_context_excerpt: str = "",
    gaps_for_section: str = "",
    outline_snippet: str = "",
    user_directive: str | None = None,
    model: str | None = None,
    prior_resolved_placeholders: list[dict] | None = None,
) -> SectionDraft:
    """Run the Writer Team agent on one section. Returns the draft.

    `user_directive` is optional revision guidance from the human reviewer
    (e.g., 'make this more concise', 'lead with custom-build positioning').
    When provided it's prepended to the user prompt as a USER DIRECTIVE block.

    `model` overrides the default revision model (settings.model_writer_team).
    Used by the initial multi-section drafter to pick a cheaper model
    (settings.model_writer_team_initial) — revisions stick with the default.

    `prior_resolved_placeholders` is the resolved [NEEDS_HUMAN] entries
    from a prior pass on the same section. Surfaced to the model as a
    "PREVIOUSLY RESOLVED HUMAN INPUTS" prompt block so it bakes the
    user's answers into the new prose instead of asking again.
    """
    settings = get_settings()

    directive_block = ""
    if user_directive and user_directive.strip():
        directive_block = _DIRECTIVE_TEMPLATE.format(directive=user_directive.strip())

    user_prompt = _USER_TEMPLATE.format(
        directive_block=directive_block,
        section_id=section_id,
        section_title=section_title,
        section_order=section_order,
        section_brief=section_brief or "(none)",
        page_limit=page_limit if page_limit is not None else "none specified",
        word_limit=word_limit if word_limit is not None else "none specified",
        compliance_items=", ".join(compliance_item_ids) if compliance_item_ids else "(none)",
        prior_resolutions_block=_format_prior_resolutions_block(prior_resolved_placeholders),
        outline_snippet=outline_snippet or "(no sibling sections)",
        gaps_for_section=gaps_for_section or "(no gaps assigned to this section)",
        kb_context_excerpt=kb_context_excerpt or "(no scoped KB content for this section)",
        rfp_excerpt=rfp_excerpt or "(no RFP excerpt available)",
    )

    # Routes by model name prefix. Default: settings.model_writer_team
    # (Opus, for revisions). Initial multi-section drafter overrides via
    # the `model` param to use the cheaper initial-draft model.
    chosen_model = model or settings.model_writer_team
    # Append any approved learned-guidance rules to the system prompt. The
    # cached_prefix is the cached block (proposal-specific); the system is
    # un-cached, so changing it doesn't break prompt-cache reuse on the
    # proposal context. Empty string when there are no approved rules.
    system_with_guidance = _SYSTEM + format_writer_guidance()
    tool_input, usage = call_tool_for_model(
        model=chosen_model,
        system=system_with_guidance,
        cached_prefix=cached_prefix,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=16000,
        agent_name="writer_team",
        proposal_id=proposal_id,
    )

    log.info(
        "writer_team: %s '%s' -> %d markdown chars, %d citations, %d needs_human, %s stop=%s",
        section_id,
        section_title,
        len(tool_input.get("draft_text_markdown") or ""),
        len(tool_input.get("citations") or []),
        len(tool_input.get("needs_human_placeholders") or []),
        fmt_llm_usage(usage),
        usage.get("stop_reason"),
    )

    return SectionDraft(
        draft_text_markdown=str(tool_input.get("draft_text_markdown") or ""),
        citations=list(tool_input.get("citations") or []),
        needs_human_placeholders=list(tool_input.get("needs_human_placeholders") or []),
        shortfall_mitigations_applied=list(tool_input.get("shortfall_mitigations_applied") or []),
    )
