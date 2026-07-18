"""Final Polish Applier — surgically edits ONE section's draft to
resolve a cross-section issue surfaced by the Final Polish Detector.

Why a separate agent (not just `re.sub` in service code):
  - The detector quotes the problematic text verbatim, but real-world
    LLM-quoted text occasionally drifts by whitespace, line wrap, or
    a single mismatched character. A literal regex substitution
    fails silently in that case; a small Sonnet call recognises the
    intent and edits the right span anyway.
  - Some fixes (voice/voice-drift, redundant repetition trim) require
    judgment about how to maintain the surrounding paragraph's flow.
    Pure substitution would leave grammatically broken seams.
  - Sonnet's structured output is reliable for "return the new
    section markdown" — Gemini's empty-tool-call quirk on drafter
    duties (per the project's documented anti-pattern) makes it the
    wrong choice for this leg.

Output: the FULL new section markdown. Caller persists via
persist_section_draft (bumps revision number, the standard pattern).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agents.final_polish_detector import PolishIssue
from app.config import get_settings
from app.services.llm import fmt_llm_usage, get_anthropic

log = logging.getLogger(__name__)


_TOOL: dict = {
    "name": "report_polished_section",
    "description": (
        "Return the section's new markdown after applying the polish "
        "edit. Output must preserve the entire section — only the "
        "minimal span needed to fix the issue should differ from the "
        "input."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "polished_markdown": {
                "type": "string",
                "description": (
                    "The full updated section draft. Identical to the "
                    "input EXCEPT for the surgically-edited span(s) "
                    "needed to apply the suggested fix. Do NOT "
                    "rewrite paragraphs you weren't asked to touch. "
                    "Do NOT add new commitments / numbers / names "
                    "that weren't in the original or the suggested "
                    "fix."
                ),
            },
            "edit_applied": {
                "type": "boolean",
                "description": (
                    "TRUE when you found the problematic_text (or a "
                    "whitespace/quote-style variant of it) and "
                    "applied the fix. FALSE when the text is "
                    "genuinely absent from the current draft (e.g., "
                    "the section was regenerated since the detector "
                    "ran) — in that case return the input markdown "
                    "unchanged and set edit_applied=false."
                ),
            },
            "edit_summary": {
                "type": "string",
                "description": (
                    "One short sentence describing what you changed, "
                    "for the user-visible activity log. E.g., "
                    "'Aligned FTE count from 4 to 3.5 in the "
                    "staffing paragraph to match SEC-007 + the "
                    "approved roster.' Empty string when "
                    "edit_applied=false."
                ),
            },
        },
        "required": [
            "polished_markdown",
            "edit_applied",
            "edit_summary",
        ],
    },
}


_SYSTEM = """You are the Final Polish Applier for a Quadratic Digital federal proposal. You edit ONE section's draft markdown to apply ONE consistency fix surfaced by the Polish Detector.

YOUR ONLY OUTPUT IS THE TOOL CALL. NO PROSE, NO PREAMBLE.

WHAT YOU DO:

1. Locate the `problematic_text` inside the current section markdown. Match verbatim where possible; tolerate minor whitespace / line-wrap / smart-quote variants.
2. Replace it with `suggested_fix`, adjusting surrounding punctuation only as needed to keep the sentence grammatical.
3. Return the FULL updated section markdown. The output must be identical to the input EXCEPT for the edited span(s).

WHAT YOU DO NOT DO:

- Do NOT rewrite paragraphs that aren't part of the edit. Even if you spot other issues, leave them alone — they'll be handled by other detector findings or other passes.
- Do NOT add new commitments, numbers, names, or claims. The fix is bounded to what's in `suggested_fix` plus the cross-section evidence the detector cited.
- Do NOT trim the section to fit a length budget. Length is fixed; only the span you're editing changes.
- Do NOT translate, reformat, or restructure markdown. Headings stay headings, lists stay lists, citation links stay intact.

WHEN problematic_text IS NOT IN THE DRAFT:

The section may have been regenerated between the detector's pass and yours. If you genuinely cannot find the problematic_text (or a clear variant) in the current draft, set:
  - edit_applied = false
  - polished_markdown = the input markdown unchanged
  - edit_summary = ""

Do NOT fabricate an edit just to satisfy the schema. The orchestrator handles edit_applied=false gracefully (logs it, moves on, doesn't bump the section revision).

WHITESPACE / QUOTE-STYLE TOLERANCE:

These count as the same span:
- "3.5 FTE" vs "3.5 FTE" (different whitespace)
- "Quadratic's solution" vs "Quadratic’s solution" (smart vs straight apostrophe)
- "5-day SLA" vs "5 day SLA" (hyphen vs space)

Match the surface form already in the draft, then edit. Don't introduce a new whitespace/quote convention into prose that uses a different one.

OUTPUT DISCIPLINE:

The polished_markdown must be the FULL section. The orchestrator literal-string-replaces draft_text_markdown with whatever you return. If you return only the edited paragraph, the rest of the section is destroyed."""


_USER_TEMPLATE = """Apply this consistency fix to the section below.

=== ISSUE ===
section_id: {section_id}
issue_type: {issue_type}
severity: {severity}
rationale: {rationale}

problematic_text:
\"\"\"
{problematic_text}
\"\"\"

suggested_fix:
\"\"\"
{suggested_fix}
\"\"\"

cross_section_evidence:
{cross_section_evidence}

=== CURRENT SECTION DRAFT ({section_id}) ===
{current_markdown}

=== END SECTION DRAFT ===

Call report_polished_section now with the full updated markdown."""


@dataclass
class PolishApplyResult:
    """Outcome of applying ONE polish issue to ONE section."""

    polished_markdown: str
    edit_applied: bool
    edit_summary: str
    cost_usd: float


def apply_polish_issue(
    *,
    proposal_id: int,
    issue: PolishIssue,
    current_markdown: str,
) -> PolishApplyResult:
    """Apply one detector-surfaced issue to the section's current
    draft. Returns the new markdown + metadata.

    Caller decides whether to persist (typically: persist when
    edit_applied=True; skip-and-log otherwise).
    """
    settings = get_settings()
    client = get_anthropic()

    user_prompt = _USER_TEMPLATE.format(
        section_id=issue.section_id,
        issue_type=issue.issue_type,
        severity=issue.severity,
        rationale=issue.rationale or "(none provided)",
        problematic_text=issue.problematic_text,
        suggested_fix=issue.suggested_fix,
        cross_section_evidence=(issue.cross_section_evidence or "(none provided)"),
        current_markdown=current_markdown or "(empty)",
    )

    tool_input, usage = client.call_tool(
        model=settings.model_polish_applier,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=16000,
        agent_name="final_polish_applier",
        proposal_id=proposal_id,
    )

    polished = str(tool_input.get("polished_markdown") or "")
    edit_applied = bool(tool_input.get("edit_applied"))
    edit_summary = str(tool_input.get("edit_summary") or "").strip()
    cost = float(usage.get("cost_usd") or 0.0)

    log.info(
        "final_polish_applier: section %s issue=%s severity=%s edit_applied=%s %s",
        issue.section_id,
        issue.issue_type,
        issue.severity,
        edit_applied,
        fmt_llm_usage(usage),
    )

    return PolishApplyResult(
        polished_markdown=polished,
        edit_applied=edit_applied,
        edit_summary=edit_summary,
        cost_usd=cost,
    )


__all__ = [
    "PolishApplyResult",
    "apply_polish_issue",
]
