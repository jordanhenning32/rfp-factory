"""Reviewer A — adversarial compliance & risk check (Opus by default).

Per design doc §6.5. Reviewer A reads ONE drafted section plus the full
context of the proposal and reports findings about:
- Citation legitimacy: every past-performance claim must trace to a
  past_performance_won / past_performance_subbed KB doc OR an entry in
  company_profile.past_performance. Pending or lost prior proposals
  CANNOT be cited as completed work.
- Hallucinations: any claim (cert, clearance, customer, dollar amount,
  date, named person) not present in the cached inputs.
- Overcommitments: language Quadratic shouldn't promise unilaterally
  (specific schedule dates, FTE counts, dollar figures, partner-confirmed
  status without [NEEDS_HUMAN]).
- Compliance coverage: every requirement_id this section was assigned
  in the outline must actually be addressed in the prose.
- Shortfall overreach: gap mitigations applied beyond what the user
  selected, or "equivalent experience" framing that's not defensible.

The model is Opus (claude-opus-4-7) by default — the stakes are FAR
debarment and lost bids, and this is exactly where Opus's reasoning
advantage over Sonnet shows up. Override via settings.model_reviewer_a.

Output: a list of findings with severity, category, finding_text, and
suggested_fix, persisted to the reviewer_findings table. The user
reviews and accepts/dismisses each finding from the Findings tab; the
Writer Team consumes accepted findings as a directive on the next
regenerate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings
from app.core.enums import FindingCategory, FindingSeverity
from app.services.lessons import format_reviewer_guidance
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


# Categories Reviewer A is responsible for. Reviewer B's category list is
# disjoint (persuasion / voice / evaluator-misalignment).
_REVIEWER_A_CATEGORIES = [
    FindingCategory.COMPLIANCE_GAP.value,
    FindingCategory.UNCITED_CLAIM.value,
    FindingCategory.HALLUCINATION.value,
    FindingCategory.OVERCOMMITMENT.value,
    FindingCategory.SHORTFALL_OVERREACH.value,
    FindingCategory.FORMAT_VIOLATION.value,
]


_TOOL: dict = {
    "name": "report_findings",
    "description": (
        "Report compliance / risk findings against this drafted section. "
        "If the section is clean, return an empty findings array. Do NOT "
        "fabricate findings to look thorough — false positives waste the "
        "user's time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "description": (
                    "List of findings. Empty array if the section is clean. "
                    "Order doesn't matter — the UI groups by severity."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": [s.value for s in FindingSeverity],
                            "description": (
                                "CRITICAL = would lose the bid or trigger FAR "
                                "debarment if submitted as-is (uncited past-"
                                "performance claim, hallucinated cert, "
                                "overcommitment Quadratic can't honor). "
                                "MAJOR = would weaken the proposal noticeably "
                                "(unaddressed compliance item, shortfall "
                                "overreach, equivalent-experience framing "
                                "that's a stretch). "
                                "MINOR = small inconsistency, citation "
                                "confidence too low, format nit."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": _REVIEWER_A_CATEGORIES,
                            "description": (
                                "uncited_claim = factual claim with no "
                                "[^cite-N] marker or invalid source class. "
                                "hallucination = claim of fact not in profile/KB. "
                                "overcommitment = unilateral promise (specific "
                                "schedule, FTE count, dollar amount, signed "
                                "partner) without [NEEDS_HUMAN]. "
                                "compliance_gap = a requirement_id assigned to "
                                "this section is not addressed in the prose. "
                                "shortfall_overreach = mitigation language "
                                "stretches beyond the user-selected option, OR "
                                "claims equivalent experience that doesn't "
                                "actually defend. "
                                "format_violation = page/word limit exceeded, "
                                "missing required heading."
                            ),
                        },
                        "finding_text": {
                            "type": "string",
                            "description": (
                                "1-3 sentences naming the specific issue. "
                                "Quote the offending text verbatim where "
                                'possible: "Quadratic delivered the CMS '
                                'modernization" — this claim has no citation.'
                            ),
                        },
                        "suggested_fix": {
                            "type": "string",
                            "description": (
                                "1-3 sentences telling the Writer Team what "
                                "to do. Be SPECIFIC. Either: a verbatim "
                                "replacement (\"Replace 'we will deliver in "
                                "90 days' with [NEEDS_HUMAN: confirm "
                                'schedule]"), OR a structural change '
                                '("Cite KB DOC #14 (past_performance_won) — '
                                'this matches the claim"), OR a deletion '
                                '("Remove the sentence; no honest '
                                'justification exists").'
                            ),
                        },
                    },
                    "required": [
                        "severity",
                        "category",
                        "finding_text",
                        "suggested_fix",
                    ],
                },
            }
        },
        "required": ["findings"],
    },
}


_SYSTEM = """You are Reviewer A for Quadratic Digital's RFP responses. Your job is ADVERSARIAL HONESTY VERIFICATION.

You read ONE drafted section against the full proposal context and surface every finding that could:
- Lose the bid (uncited past-performance claims, unaddressed compliance items, overcommitments).
- Trigger FAR debarment (false statements, hallucinated certifications, fabricated past performance).
- Embarrass the firm (inconsistencies between sections, defensible-on-paper claims that wouldn't survive scrutiny).

You are NOT a copy editor. Don't flag voice, persuasion, or stylistic issues — Reviewer B handles those.

YOUR CHECKS, IN PRIORITY ORDER:

1. CITATION LEGITIMACY (highest priority — design doc §7.1, non-negotiable):
   - Every claim of the form "Quadratic delivered X" / "Quadratic completed Y for Customer Z" / "Quadratic supported Project A" MUST cite a KB doc with class `past_performance_won` or `past_performance_subbed`, OR an entry in `company_profile.past_performance`.
   - NEVER acceptable: a past-performance claim citing a `prior_proposal_*` doc. Those are voice grounding only — citing them as completed work is FAR-actionable misrepresentation.
   - NEVER acceptable: a past-performance claim with NO citation at all.
   - Capability claims (we have personnel with X skill, we operate platform Y) cite either `company_profile.<field>` or a citable KB doc.
   - Citations array entries with confidence=LOW deserve scrutiny — flag if the claim is more confident than the evidence supports.

2. HALLUCINATIONS:
   - Any specific dollar amount, customer name, project name, year, person's name, or count that doesn't appear in the cached inputs (profile, KB context, RFP text) is a hallucination. Flag every instance.
   - Certifications, clearances, contract vehicles, set-asides, or NAICS codes not in the profile are hallucinations.
   - Be especially suspicious of round numbers (170+ offices, 50+ engineers) — confirm against the profile.

3. OVERCOMMITMENTS:
   - Specific schedule commitments tied to dates ("starting January 15, 2027") — flag unless [NEEDS_HUMAN] wraps them.
   - Specific FTE counts beyond what the profile / staffing plan supports.
   - Final teaming partner confirmation status — even if a partner is suggested in the gap mitigation, the section must say [NEEDS_HUMAN: confirm teaming agreement with X executed before submission].
   - Specific dollar amounts — those belong to the Cost Analysis Agent (Weeks 12-13).
   - SLA percentages, uptime guarantees, response-time commitments — these are pricing-adjacent; flag unless [NEEDS_HUMAN] defers them.

4. COMPLIANCE COVERAGE:
   - The cached prefix lists which requirement_ids this section was assigned in the outline.
   - For each assigned requirement_id, scan the section's prose. If it's not addressed, flag as compliance_gap (severity MAJOR).
   - "Addressed" means the section makes a substantive response to the requirement, not just mentions it.

5. SHORTFALL OVERREACH:
   - The cached prefix lists the gap analyses with the user's selected_mitigation_index. Read the chosen mitigation's `proposal_language_draft` — that's the user-approved framing.
   - If the section's prose stretches BEYOND the chosen mitigation (e.g., user picked "equivalent experience" but the prose claims direct experience), flag as shortfall_overreach.
   - If the section's "equivalent experience" framing isn't defensible (commercial UI ≠ Section 508 accessibility; commercial small business ≠ federal HUBZone), flag.
   - If "in progress" framing lacks a concrete plan, flag.

6. FORMAT (lowest priority):
   - Section's page_limit / word_limit — if the prose appears to exceed (rough heuristic on word count), flag.
   - Required headings dictated by the RFP that are missing.

WHAT NOT TO FLAG:
- Stylistic preferences (Reviewer B's job).
- "Could be more persuasive" (Reviewer B).
- Citation marker numbering issues (the writer manages those).
- Suggestions for additional content (you check what's there, not what could be).
- [NEEDS_HUMAN] placeholders themselves — they're the writer's mechanism for flagging things you'd otherwise complain about. Do NOT flag the existence of placeholders. DO flag a placeholder that's too vague to act on.

EVALUATION CRITERIA — WHAT THE BUYER ACTUALLY SCORES
The EVALUATION CRITERIA block in the cached prefix lists the factors the buyer will use to grade this proposal, with weights and scoring scales as published. For each finding you raise, consider: does this affect the section's ability to score on its assigned factor(s)? Findings that improve compliance but do not improve the factor score are LOW-priority. Findings that move a section from 'Acceptable' to 'Exceptional' on a high-weight factor are HIGH-priority. If the section's assigned factors are unknown (no Section M extracted), fall back to standard compliance/risk review.

AMENDMENT AWARENESS — If a compliance item the section addresses has amendment_origin set, the requirement was added or modified by an amendment after the original RFP. The section's draft may not yet reflect the change. Flag any paragraph that contradicts the current requirement_text as MAJOR/compliance_gap with category='amendment_drift'.

OUTPUT: call report_findings with one finding per issue. Empty array if the section is clean. Severity is your judgment — use CRITICAL sparingly, only for things that would lose the bid.
"""


_CACHED_PREFIX_TEMPLATE = """=== QUADRATIC DIGITAL COMPANY PROFILE (canonical) ===
{profile_json}

=== QUADRATIC DIGITAL KNOWLEDGE BASE (citable evidence — past performance citations may ONLY trace here or to the profile.past_performance array) ===
{kb_context}

=== PROPOSAL OUTLINE (every section, including the one being reviewed) ===
{outline_text}

=== COMPLIANCE MATRIX (every requirement) ===
{compliance_text}

=== GAP ANALYSES (with user-chosen mitigations — section drafts must NOT exceed the chosen mitigation's framing) ===
{gaps_text}
{evaluation_criteria_block}"""


_USER_TEMPLATE = """Review section {section_id} now.

Title: {section_title}
Page limit: {page_limit}
Word limit: {word_limit}
This section was assigned compliance items: {compliance_items}
This section was assigned gap mitigations for: {assigned_gaps}

DRAFTED MARKDOWN:
\"\"\"
{draft_markdown}
\"\"\"

STRUCTURED CITATIONS REPORTED BY THE WRITER (verify each):
{citations_text}

[NEEDS_HUMAN] PLACEHOLDERS REPORTED BY THE WRITER:
{needs_human_text}

GAP_IDS THE WRITER CLAIMS TO HAVE APPLIED:
{applied_gaps}

Use the report_findings tool. Empty findings array if the section is clean."""


_USER_AMENDED_BLOCK_TEMPLATE = """
AMENDED ITEMS (these requirements were added or modified by an amendment after the original RFP):
{amended_items_text}
"""


@dataclass
class ReviewerFindingDraft:
    """One finding before persistence — matches the writer schema."""

    severity: str
    category: str
    finding_text: str
    suggested_fix: str


def build_cached_prefix(
    *,
    profile_json: str,
    kb_context: str,
    outline_text: str,
    compliance_text: str,
    gaps_text: str,
    evaluation_criteria_block: str = "",
) -> str:
    return _CACHED_PREFIX_TEMPLATE.format(
        profile_json=profile_json,
        kb_context=kb_context,
        outline_text=outline_text,
        compliance_text=compliance_text,
        gaps_text=gaps_text,
        evaluation_criteria_block=evaluation_criteria_block,
    )


def _format_citations(citations: list[dict]) -> str:
    if not citations:
        return "(none)"
    lines = []
    for c in citations:
        marker = c.get("marker", "?")
        claim = c.get("claim", "")
        src = c.get("source_kb_doc", "")
        src_sec = c.get("source_section", "")
        conf = c.get("confidence", "")
        line = f"  [^{marker}] (confidence={conf}) {claim}\n      source: {src}"
        if src_sec:
            line += f" — {src_sec}"
        lines.append(line)
    return "\n".join(lines)


def _format_needs_human(placeholders: list[dict]) -> str:
    if not placeholders:
        return "(none)"
    return "\n".join(
        f"  [{ph.get('category', '?')}] {ph.get('marker_text', '')}: {ph.get('description', '')}"
        for ph in placeholders
    )


def review_section(
    *,
    proposal_id: int,
    section_id: str,
    section_title: str,
    page_limit: int | None,
    word_limit: int | None,
    compliance_item_ids: list[str],
    assigned_gap_ids: list[str],
    draft_markdown: str,
    citations: list[dict],
    needs_human_placeholders: list[dict],
    applied_gap_ids: list[str],
    cached_prefix: str,
    amended_items: list[dict] | None = None,
) -> list[ReviewerFindingDraft]:
    """Run Reviewer A on one section. Returns the raw findings list.

    The orchestrator (jobs/reviewer.py) persists these to reviewer_findings
    with reviewer_agent='A' and the current pass_number.

    `amended_items` is a list of dicts of the form
    `{requirement_id, requirement_text, amendment_origin}`. When non-empty,
    an AMENDED ITEMS block is appended to the user prompt so Reviewer A
    can flag paragraphs that contradict the current (post-amendment)
    requirement text.
    """
    settings = get_settings()

    user_prompt = _USER_TEMPLATE.format(
        section_id=section_id,
        section_title=section_title,
        page_limit=page_limit if page_limit is not None else "none",
        word_limit=word_limit if word_limit is not None else "none",
        compliance_items=", ".join(compliance_item_ids) if compliance_item_ids else "(none)",
        assigned_gaps=", ".join(assigned_gap_ids) if assigned_gap_ids else "(none)",
        draft_markdown=draft_markdown or "(empty)",
        citations_text=_format_citations(citations),
        needs_human_text=_format_needs_human(needs_human_placeholders),
        applied_gaps=", ".join(applied_gap_ids) if applied_gap_ids else "(none)",
    )

    if amended_items:
        amended_lines = [
            f"  {r.get('requirement_id', '?')} "
            f"[origin: {r.get('amendment_origin', '?')}]: "
            f"{r.get('requirement_text', '')}"
            for r in amended_items
        ]
        user_prompt += _USER_AMENDED_BLOCK_TEMPLATE.format(
            amended_items_text="\n".join(amended_lines),
        )

    # Routes by model name prefix (claude-* → Anthropic, gpt-*/o1-*/o3-* →
    # OpenAI, gemini-* → Google). Default is gpt-5.5 for provider diversity
    # vs the Writer Team (Opus / Anthropic).
    # Append learned reviewer-calibration rules + per-category dismiss-rate
    # stats so the model can suppress patterns the user has historically
    # rejected as false positives.
    system_with_guidance = _SYSTEM + format_reviewer_guidance(
        reviewer="A",
        categories=_REVIEWER_A_CATEGORIES,
    )
    tool_input, usage = call_tool_for_model(
        model=settings.model_reviewer_a,
        system=system_with_guidance,
        cached_prefix=cached_prefix,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=8000,
        agent_name="reviewer_a",
        proposal_id=proposal_id,
    )

    raw = tool_input.get("findings", []) or []
    log.info(
        "reviewer_a: section %s -> %d finding(s), %s",
        section_id,
        len(raw),
        fmt_llm_usage(usage),
    )

    findings: list[ReviewerFindingDraft] = []
    for f in raw:
        try:
            findings.append(
                ReviewerFindingDraft(
                    severity=str(f["severity"]),
                    category=str(f["category"]),
                    finding_text=str(f["finding_text"]),
                    suggested_fix=str(f.get("suggested_fix") or ""),
                )
            )
        except (KeyError, TypeError) as exc:
            log.warning("reviewer_a: skipping malformed finding %r: %s", f, exc)
    return findings
