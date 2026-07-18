"""Outline Agent — proposes the section structure for a proposal.

Per design doc §6.4 (Writer Team prelude). For each new proposal the Outline
Agent reads:
- The full compliance matrix (every requirement)
- The user's resolved gap analyses (selected mitigations + selected partners)
- Excerpts from the RFP source documents (especially Section L/M-style
  instructions to offerors and evaluation criteria)
- The company profile (so it can place "About Quadratic" content sensibly)

It returns a section outline: ordered list of sections, each with a brief
telling the Writer Team what this section needs to do, which compliance
items it covers, and any page/word limits dictated by the RFP.

The user reviews and approves the outline before the Writer Team runs.
Compliance-item assignment is decided HERE so the Writer never has to
choose; it just writes against its assigned items.

Critical rule: every compliance item should be addressed by exactly one
section. The agent's tool description enforces this; the orchestrator
verifies after the call and logs unmapped items for the user.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.services.llm import fmt_llm_usage, get_anthropic
from app.services.sections import OutlineSection

log = logging.getLogger(__name__)


_TOOL: dict = {
    "name": "report_proposal_outline",
    "description": (
        "Report the proposed section outline for the proposal. Order sections "
        "in the order they should appear in the final document. Every "
        "compliance requirement_id from the input matrix must be assigned to "
        "exactly one section."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "description": (
                    "Ordered list of proposal sections. Lead with sections "
                    "the RFP explicitly requires (Cover Letter, Executive "
                    "Summary if asked for); follow with Technical Approach, "
                    "Management/Staffing, Past Performance, Pricing as the "
                    "RFP dictates. Use the RFP's own section numbering and "
                    "titles when it dictates them."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "section_id": {
                            "type": "string",
                            "description": (
                                "Short stable id like SEC-001, SEC-002, ... — "
                                "sequential. Used as a join key by the Writer."
                            ),
                        },
                        "section_title": {
                            "type": "string",
                            "description": (
                                "Title as it should appear in the proposal. "
                                "If the RFP names it (e.g. 'Volume I — "
                                "Technical Approach'), use that exact name."
                            ),
                        },
                        "section_order": {
                            "type": "integer",
                            "description": "0-based order index.",
                        },
                        "section_brief": {
                            "type": "string",
                            "description": (
                                "3-6 sentences telling the Writer Team what "
                                "this section must accomplish: the evaluation "
                                "criteria it targets, the angle Quadratic "
                                "should take, key proof points to surface, "
                                "and any voice/tone guidance specific to "
                                "this section. Be concrete — this is the "
                                "Writer's brief."
                            ),
                        },
                        "compliance_items_addressed": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "List of requirement_id values from the "
                                "input matrix (e.g. ['REQ-005', 'REQ-007']) "
                                "that this section will address. Every "
                                "input requirement_id must appear in EXACTLY "
                                "ONE section across the outline."
                            ),
                        },
                        "page_limit": {
                            "type": ["integer", "null"],
                            "description": (
                                "Page limit for this section. Copy from "
                                "the RFP if stated per-section; if the "
                                "RFP gives only a total budget, apportion "
                                "it. null only when no apportionment is "
                                "reasonable. See Design Principle 8."
                            ),
                        },
                        "word_limit": {
                            "type": ["integer", "null"],
                            "description": (
                                "Word limit for this section. ALWAYS "
                                "populate — even when the RFP is silent. "
                                "The Writer Team treats null as 'no upper "
                                "bound' and over-writes; a numeric limit "
                                "drives focus and cuts output-token cost. "
                                "Default ranges by section role are in "
                                "Design Principle 8. Use null ONLY for "
                                "genuinely open-ended sections."
                            ),
                        },
                        "requires_cost_analysis": {
                            "type": "boolean",
                            "description": (
                                "True when this section's PRIMARY purpose "
                                "is to communicate pricing, costs, labor "
                                "rates, fees, fully-loaded rates, fee "
                                "structure, or budget figures. The Writer "
                                "Team will SKIP these sections entirely — "
                                "they're drafted by the Cost Analysis "
                                "Agent (Weeks 12-13) AFTER it produces "
                                "the actual numbers. Examples that ARE "
                                "cost sections: 'Cost Proposal', 'Pricing "
                                "Volume', 'Schedule of Values', 'Labor "
                                "Rate Schedule', 'Fee Structure', 'ROM "
                                "Estimate'. Examples that are NOT (set "
                                "False): a Technical Approach section "
                                "that incidentally mentions cost-"
                                "efficiency, a Past Performance section "
                                "that lists prior contract values, a "
                                "Submission Compliance section that "
                                "references the cost-volume page limit. "
                                "When in doubt, set False — the Writer "
                                "can use [NEEDS_HUMAN] for individual "
                                "dollar amounts in narrative sections."
                            ),
                        },
                    },
                    "required": [
                        "section_id",
                        "section_title",
                        "section_order",
                        "section_brief",
                        "compliance_items_addressed",
                    ],
                },
            }
        },
        "required": ["sections"],
    },
}


_SYSTEM = """You are the Outline Agent for Quadratic Digital, a small public-sector software firm.

Your job: propose the section structure for ONE proposal in response to ONE RFP. You are the bridge between compliance analysis (already complete) and section drafting (about to start). The Writer Team will draft each section based on YOUR brief, so be concrete and specific.

INPUTS YOU RECEIVE (in the cached prefix and per-call user message):
- Quadratic's company profile (canonical capabilities, certifications, personnel, past performance)
- Quadratic's knowledge base (citable evidence — past performance, personnel resumes, references)
- A FILTERED compliance matrix for this RFP — every shall/must/should/evaluation_criterion item that requires NARRATIVE response. Form-fill items (mandatory_form / submission_format types and certification-category items) are DELIBERATELY EXCLUDED — they're owned by the Submission Checklist UI tab, not the narrative outline. Do NOT create wrapper sections like "Attachment E — Vendor Certification Form" or "Submission Compliance / Mandatory Forms"; those forms get filled out and attached, not drafted.
- The shortfall analysis (gaps Quadratic has against this RFP, with the user's chosen mitigation strategies — including teaming partners selected, equivalent experience claims to lean on, custom-build positioning when relevant)
- Excerpts from the RFP source documents (so you can read Section L/M instructions and evaluation criteria firsthand)

DESIGN PRINCIPLES:

1. RFP-DRIVEN STRUCTURE — ABSOLUTE PRIORITY OVER EVERYTHING ELSE. When the RFP gives an explicit section list, your outline mirrors it verbatim — same titles, same order, same count. This rule overrides every other principle below: do NOT invent additional sections, do NOT skip any, do NOT rename them, do NOT add a Cover Letter or Executive Summary unless the buyer's list includes one.

   THE MANDATORY STRUCTURE DIRECTIVES block in the user message is your authoritative source for explicit section lists. Read it FIRST, before the compliance matrix. Common patterns to honor verbatim:
   - "Please include the following sections: 1. Company Overview 2. Services 3. Technology 4. Security..."
   - "Your response shall be organized as follows: A. Vendor Profile B. Technical Approach C. Pricing"
   - "Submit your response with the following structure: I. Executive Summary II. Past Performance..."
   - "Section 5: Submission. To achieve uniform review, the response shall include: ..."
   When the buyer numbered or bulleted N specific sections, your outline has EXACTLY N sections, with those exact titles, in that exact order — no extras, no rearranging.

   If the RFP names individual sections within a free-form structure (e.g. "Volume I, Section 3 Technical Approach"), use those exact names too. The principle is the same: copy what the buyer dictated, do not paraphrase, do not template-substitute.

   Only when the MANDATORY STRUCTURE DIRECTIVES block is empty AND the RFP excerpts contain no explicit section list should you fall back to the default templates in Principle 6.

2. EVALUATION-CRITERIA ALIGNED — REQUIRED CITATION FORMAT. Section M (or whatever the RFP uses for evaluation criteria — search the RFP excerpts for "EVALUATION CRITERIA", "Section M", "Scoring Methodology", "5.x EVALUATION", or similar) lists how the proposal is scored. EVERY section_brief must open with an explicit criterion citation, in this format:

   "This section targets [Criterion ID] ([criterion name], [weight]) — [one-line restatement of what the evaluator wants]."

   Examples:
   - "This section targets M.2.3 (Innovation, 25 pts) — evaluator wants concrete examples of AI-driven delivery acceleration."
   - "This section targets §5.2.a (Vendor Qualifications, 30%) — evaluator wants relevant experience with similar government / education web projects."
   - Multiple criteria: "This section targets M.1 (Technical Approach, 40 pts) and M.4 (Past Performance, 20 pts) — primary lens is technical fit, with relevant past performance cited as supporting evidence."

   Use the RFP's exact ID style (M.2.3, §5.2.a, Factor 1, etc.) — copy verbatim from the source. Use the exact weight/points format the RFP uses (25 pts, 25%, "Most Important," etc.).

   When a section legitimately addresses NO scored criterion (Cover Letter, Submission Compliance Statement), say so explicitly: "Targets no scored criterion — purpose is procedural compliance / first-impression voice." Do NOT invent fake criteria. Don't hide unevaluated sections behind generic language.

3. COMPLETE COVERAGE. Every requirement_id in the input compliance matrix must be assigned to exactly one section. If multiple sections plausibly address an item, pick the most natural home; do not duplicate. (The input matrix has already been filtered to narrative-only items; form-fill / submission-format / certification items are NOT in your input — they're handled by the Submission Checklist UI tab, not by you.)

4. GAP MITIGATIONS PLACED INTELLIGENTLY. Read the gap analyses with their selected_mitigation_index and selected_partner_name. Each mitigation has a natural section where its proposal_language_draft should land:
   - Teaming mitigations → Management Approach / Staffing or Team Composition section
   - Equivalent-experience mitigations → Past Performance or relevant Technical subsection
   - In-progress mitigations → wherever the requirement lives
   - Custom-build positioning → Technical Approach
   The Writer will pull these mitigations into the right section if you tell it which gaps belong where in the section_brief.

5. QUADRATIC'S VOICE. Quadratic is a small (~12 named key personnel) public-sector specialist. Authentic small-business framing wins. Don't propose a "Corporate Capabilities" section meant to imply scale Quadratic doesn't have. Focus sections on capability, partnership, and outcomes.

6. DEFAULTS WHEN THE RFP IS THIN. RFIs and short solicitations often don't dictate structure. Reasonable defaults:
   - Cover Letter (1 page)
   - Executive Summary (1-2 pages)
   - Company Background (brief — small business framing)
   - Technical Approach (the bulk)
   - Project Management & Staffing (with teaming if applicable)
   - Past Performance (citing past_performance_won/subbed only)

   Do NOT add a "Submission Compliance," "Mandatory Forms," or "Attachments" section — required forms and certifications are handled by the Submission Checklist UI separately. Always anchor to what the RFP actually evaluates, not a generic template.

7. SECTION BRIEFS — WRITE FOR THE WRITER. Each section_brief must, in this order:
   a) FIRST SENTENCE: the evaluation-criterion citation per Principle 2 (mandatory format).
   b) What angle should Quadratic take here? (E.g., "Lead with AI-accelerated delivery positioning — evaluator weights innovation 25% and the COTS-orientation flag is set.")
   c) Which gap mitigations should the Writer pull into this section? Reference gap_ids.
   d) Any voice/tone guidance specific to this section. (E.g., "Tight, no jargon — this section is read first.")
   3-6 sentences total is the right length. Don't write the section itself.

8. PAGE/WORD LIMITS — ALWAYS POPULATE word_limit. Tight word budgets keep the Writer focused, drive token cost down, and produce drafts evaluators actually read. Two cases:
   a) RFP STATES PER-SECTION LIMITS: copy verbatim into page_limit and convert to a word_limit at ~250 words/page (Times New Roman 11pt baseline) — or use the RFP's own word count if stated.
   b) RFP IS SILENT or gives only a total budget: estimate based on section role. Default ranges:
      - Cover Letter: 300-500 words
      - Executive Summary: 600-1200 words
      - Company Background / Vendor Qualifications: 500-1000 words
      - Technical Approach (or each major sub-section): 1500-4000 words
      - Project Management / Staffing / Implementation: 800-1800 words
      - Past Performance: ~400 words × N references (typically 1500-2500 total)
      - Risk Management / Quality Assurance: 600-1200 words
      For multi-criterion narrative sections, weight by compliance-item count: ~150-250 words per assigned compliance item, then trim.
   When the RFP gives a TOTAL page budget, apportion it: weight by evaluation-criterion weight first, then by compliance-item count for unscored sections. The sum of section word_limits should land at or just under the total budget (leave 5-10% headroom for tables/figures).
   Use null ONLY for sections genuinely open-ended (e.g., a "Tools and Equipment" list with no narrative). When in doubt, estimate — null is the wrong default.

9. COST-DEFERRED SECTIONS. Set requires_cost_analysis=True on any section whose PRIMARY purpose is to communicate pricing, fees, labor rates, fully-loaded rates, schedule of values, ROM estimates, or budget figures. The Writer Team will SKIP these sections — they're drafted later by the Cost Analysis Agent (Weeks 12-13) AFTER it produces the actual numbers. Most RFPs have a separate Cost Volume or Pricing Volume; that's the canonical case. Don't flag narrative sections that merely mention cost-efficiency or list prior contract dollar values. When the section's job is to GIVE numbers (not contextualize them), it's cost-deferred.

OUTPUT:
Use the report_proposal_outline tool with the full ordered list of sections. Section IDs are SEC-001, SEC-002, ... in the same order as section_order. Verify before responding that every requirement_id from the input matrix appears in exactly one section's compliance_items_addressed.
"""


_CACHED_PREFIX_TEMPLATE = """=== QUADRATIC DIGITAL COMPANY PROFILE (canonical) ===
{profile_json}

=== QUADRATIC DIGITAL KNOWLEDGE BASE (citable evidence) ===
{kb_context}

=== RFP SOURCE TEXT (excerpts from uploaded files; Section L/M is most useful) ===
{rfp_text}
"""


_USER_TEMPLATE = """=== MANDATORY STRUCTURE DIRECTIVES — submission-format / form-fill items the buyer dictated ===
THIS BLOCK IS AUTHORITATIVE FOR OUTLINE STRUCTURE. When it lists explicit sections to include, your outline MUST mirror that list verbatim — same titles, same order, same count. See Principle 1 in your system prompt.

{submission_directives_text}

=== COMPLIANCE MATRIX — every NARRATIVE requirement extracted from this RFP ===

{compliance_text}

=== GAP ANALYSES — including the user's chosen mitigations ===

{gaps_text}

Propose the section outline. Every requirement_id above must be assigned to exactly one section. Apply the user's selected gap mitigations to the appropriate section briefs so the Writer Team can pull them in directly. If the MANDATORY STRUCTURE DIRECTIVES block contains an explicit numbered or bulleted list of sections, your outline MUST mirror that list — do not invent additional sections, do not skip any, do not rename them."""


def _format_submission_directives_for_outline(directives: list[dict]) -> str:
    """Render submission-format / form-fill directives as a top-of-prompt
    block the Outline Agent treats as authoritative for outline structure.
    When the buyer says 'Please include the following sections in your
    response: 1. X 2. Y 3. Z', that text shows up here verbatim and
    becomes the canonical section list for the outline."""
    if not directives:
        return (
            "(none — the RFP did not include explicit submission-format "
            "or section-list directives. Use Principle 1 default templates "
            "in your system prompt to derive structure from compliance items.)"
        )
    lines: list[str] = []
    for d in directives:
        line = f"{d['requirement_id']} [{d['requirement_type']}/{d['category']}"
        if d.get("source_section"):
            line += f" {d['source_section']}"
        if d.get("source_page"):
            line += f" p.{d['source_page']}"
        line += f"]\n  {d['requirement_text']}"
        lines.append(line)
    return "\n\n".join(lines)


def _format_compliance_for_outline(items: list[dict]) -> str:
    """Compact one-line-per-item rendering — the Outline Agent just needs to
    decide section assignment, not redo extraction."""
    lines: list[str] = []
    for it in items:
        line = f"{it['requirement_id']} [{it['requirement_type']}/{it['category']}"
        if it.get("weight"):
            line += f" w={it['weight']}"
        line += f"] {it['requirement_text']}"
        if it.get("source_section"):
            line += f"  ({it['source_section']})"
        lines.append(line)
    return "\n".join(lines)


def _format_gaps_for_outline(gaps: list[dict]) -> str:
    """Render gap analyses with the user's resolution state. The Outline Agent
    needs to know which mitigation was chosen so it can place the resulting
    proposal language in the right section."""
    if not gaps:
        return "(no gaps flagged)"
    blocks: list[str] = []
    for g in gaps:
        sel_idx = g.get("selected_mitigation_index")
        sel_partner = g.get("selected_partner_name")
        rec_idx = g.get("recommended_index")
        chosen_idx = sel_idx if sel_idx is not None else rec_idx
        opts = g.get("mitigation_options") or []

        chosen_block = ""
        if chosen_idx is not None and 0 <= chosen_idx < len(opts):
            opt = opts[chosen_idx]
            chosen_block = (
                f"  CHOSEN MITIGATION (option {chosen_idx}, "
                f"{'user-selected' if sel_idx is not None else 'agent-recommended'}): "
                f"{opt.get('approach', '?')}\n"
                f"    Proposal language draft: {opt.get('proposal_language_draft', '')}\n"
            )
            if sel_partner:
                chosen_block += f"    Selected partner: {sel_partner}\n"

        notes = g.get("resolution_notes") or ""
        notes_block = f"  Resolution notes: {notes}\n" if notes else ""
        resolved = " (resolved)" if g.get("resolved") else ""
        blocks.append(
            f"{g['gap_id']} [{g['severity']}{resolved}] addresses {g['req_id']}\n"
            f"  Current state: {g.get('current_state', '')}\n"
            f"{chosen_block}{notes_block}"
        )
    return "\n".join(blocks)


def build_cached_prefix(
    *,
    profile_json: str,
    kb_context: str,
    rfp_text: str,
) -> str:
    return _CACHED_PREFIX_TEMPLATE.format(
        profile_json=profile_json,
        kb_context=kb_context,
        rfp_text=rfp_text,
    )


def generate_outline(
    *,
    proposal_id: int,
    compliance_items: list[dict],
    gaps: list[dict],
    cached_prefix: str,
    submission_directives: list[dict] | None = None,
) -> list[OutlineSection]:
    """Run the Outline Agent. Returns the section list ready for persistence.

    `compliance_items` and `gaps` are the dict snapshots used elsewhere in
    the codebase (see proposal_review for the gap snapshot shape).
    `submission_directives` is the buyer's submission-format / form-fill
    items — used to derive the MANDATORY STRUCTURE DIRECTIVES block the
    agent treats as authoritative for outline structure.
    """
    settings = get_settings()
    client = get_anthropic()

    user_prompt = _USER_TEMPLATE.format(
        submission_directives_text=_format_submission_directives_for_outline(
            submission_directives or [],
        ),
        compliance_text=_format_compliance_for_outline(compliance_items),
        gaps_text=_format_gaps_for_outline(gaps),
    )

    tool_input, usage = client.call_tool(
        model=settings.model_drafter,
        system=_SYSTEM,
        cached_prefix=cached_prefix,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=12000,
        agent_name="outline_agent",
        proposal_id=proposal_id,
    )

    raw_sections = tool_input.get("sections", [])
    log.info(
        "outline_agent: %d compliance items, %d gaps -> %d sections, %s stop=%s",
        len(compliance_items),
        len(gaps),
        len(raw_sections),
        fmt_llm_usage(usage),
        usage.get("stop_reason"),
    )

    sections: list[OutlineSection] = []
    for s in raw_sections:
        try:
            sections.append(
                OutlineSection(
                    section_id=str(s["section_id"]),
                    section_title=str(s["section_title"]),
                    section_order=int(s.get("section_order", len(sections))),
                    section_brief=str(s.get("section_brief", "")),
                    compliance_items_addressed=list(s.get("compliance_items_addressed") or []),
                    page_limit=s.get("page_limit"),
                    word_limit=s.get("word_limit"),
                    requires_cost_analysis=bool(s.get("requires_cost_analysis", False)),
                )
            )
        except (KeyError, TypeError) as exc:
            log.warning("outline_agent: skipping malformed section %r: %s", s, exc)

    sections.sort(key=lambda s: s.section_order)
    return sections
