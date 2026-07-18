"""Final Polish Detector — reads every drafted section as a single
corpus and surfaces cross-section inconsistencies that single-section
reviewers (Reviewer A + B) miss by definition.

Why a separate agent: Reviewer A reads ONE section and asks "is this
section internally consistent / compliant?". Reviewer B reads ONE
section and asks "does this section persuade?". Neither sees the
proposal as a whole, so neither catches:

  - Numerical drift ("Section 3 says 4 FTE; Section 7 says 3.5 FTE")
  - Terminology drift ("platform" vs "system" vs "solution"
    interchangeably for the same thing)
  - Voice drift (Section 2 formal/passive; Section 7 informal/active)
  - Commitment conflict ("5-day SLA" in 3.1, "7-day SLA" in 3.3)
  - Redundant repetition (the same point made 3 times across sections)
  - Naming inconsistency ("Quadratic" vs "Quadratic Digital" vs "QD")

Model: Gemini 2.5 Pro. The 2M-token context window matters here —
we can fit all 8 drafted sections + the cost narrative as one input
without summarization. Synthesis across sections is exactly Gemini's
strength.

Output: a list of structured issues that the polish APPLIER agent
(Sonnet 4.6) consumes one-by-one to surgically edit the affected
section's draft. Per the handoff's documented anti-pattern
('don't put Gemini on a drafter — empty-tool-call failure mode'),
Gemini does NOT edit drafts here; it only detects + suggests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


_ISSUE_TYPES = (
    "numerical_drift",
    "terminology_drift",
    "voice_drift",
    "commitment_conflict",
    "redundant_repetition",
    "naming_inconsistency",
)

_SEVERITIES = ("CRITICAL", "MAJOR", "MINOR")


_TOOL: dict = {
    "name": "report_polish_issues",
    "description": (
        "Report cross-section consistency issues found in the proposal "
        "draft corpus. Each issue must point at ONE section's "
        "problematic text plus the conflicting evidence from other "
        "sections. Empty array when the corpus is consistent."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "description": (
                    "Cross-section inconsistencies. Empty when none "
                    "found. Order by severity (CRITICAL first)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "section_id": {
                            "type": "string",
                            "description": (
                                "The section whose draft contains the "
                                "problematic text and should be edited "
                                "to fix it. Use the SEC-### format "
                                "exactly as it appears in the input."
                            ),
                        },
                        "issue_type": {
                            "type": "string",
                            "enum": list(_ISSUE_TYPES),
                            "description": (
                                "Bucket for the issue. "
                                "numerical_drift = different numbers "
                                "for the same thing across sections "
                                "(FTE, hours, dollars, percentages, "
                                "durations). "
                                "terminology_drift = different terms "
                                "for the same concept ('platform' vs "
                                "'system' vs 'solution'). "
                                "voice_drift = tone/voice mismatch "
                                "with the rest of the proposal. "
                                "commitment_conflict = conflicting "
                                "promises (different SLAs, response "
                                "times, deliverable counts). "
                                "redundant_repetition = the same "
                                "point made in 3+ sections. "
                                "naming_inconsistency = company / "
                                "product / role names spelled "
                                "differently across sections."
                            ),
                        },
                        "severity": {
                            "type": "string",
                            "enum": list(_SEVERITIES),
                            "description": (
                                "CRITICAL = numerical or commitment "
                                "conflict that an evaluator would "
                                "flag as cost-realism / compliance "
                                "issue. MAJOR = terminology / voice "
                                "drift noticeable in a careful read. "
                                "MINOR = naming / cosmetic."
                            ),
                        },
                        "problematic_text": {
                            "type": "string",
                            "description": (
                                "The EXACT verbatim text from the "
                                "section's current draft that needs "
                                "to be replaced. The downstream "
                                "applier does literal-string match + "
                                "replace, so this MUST be a verbatim "
                                "substring of the draft markdown. "
                                "Quote the smallest unit that captures "
                                "the issue; do not include surrounding "
                                "paragraphs."
                            ),
                        },
                        "suggested_fix": {
                            "type": "string",
                            "description": (
                                "What problematic_text should be "
                                "replaced with. Same prose register "
                                "and grammar so the surrounding "
                                "paragraph still reads correctly. "
                                "Do not invent new commitments — "
                                "align to the value already stated "
                                "elsewhere in the corpus."
                            ),
                        },
                        "cross_section_evidence": {
                            "type": "string",
                            "description": (
                                "The conflicting state from OTHER "
                                "sections that drove the issue. Cite "
                                "section_id + a short verbatim "
                                "snippet, e.g., 'SEC-007 says \"3.5 "
                                'FTE deployed across phases 1-2"; '
                                'SEC-003 says "a 4-FTE delivery '
                                "team\".' Used by the applier to "
                                "decide which value to align to."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "1-2 sentences on why this matters "
                                "to an evaluator. Drives the user-"
                                "visible activity log so the human "
                                "can audit auto-applied changes."
                            ),
                        },
                    },
                    "required": [
                        "section_id",
                        "issue_type",
                        "severity",
                        "problematic_text",
                        "suggested_fix",
                        "cross_section_evidence",
                        "rationale",
                    ],
                },
            }
        },
        "required": ["issues"],
    },
}


_SYSTEM = """You are the Final Polish Detector for a Quadratic Digital federal proposal. You read every drafted section + the cost narrative as a SINGLE corpus and surface cross-section inconsistencies that the per-section reviewers cannot see.

YOUR ONLY OUTPUT IS THE TOOL CALL. NO PROSE, NO PREAMBLE.

WHAT TO LOOK FOR:

1. NUMERICAL DRIFT — the same quantity stated differently in two or more sections.
   Examples:
   - FTE counts: "4 FTE" in SEC-003, "3.5 FTE" in SEC-007.
   - Hours: "1,950 billable hrs/yr" in SEC-008, "1,880 hrs/yr" in SEC-009.
   - Dollars: "$1.14M total" in SEC-002, "$1,140,000" in SEC-009 (formatting OK; conflicting values NOT OK).
   - Percentages: "25% margin" in SEC-009, "22% margin" implied by another section's math.
   - Durations: "12-month base year" in SEC-007, "10-month implementation" in SEC-009 if those should match.
   These are CRITICAL when they affect cost realism or compliance commitments; MAJOR otherwise.

2. COMMITMENT CONFLICT — different promises about the same deliverable / SLA / process.
   Examples:
   - "24-hour response" in SEC-003, "next-business-day response" in SEC-007.
   - "weekly status reports" in one section, "bi-weekly" in another.
   - "all sections written in active voice" claimed in methodology, but later section uses passive.
   Always CRITICAL — evaluators will flag.

3. TERMINOLOGY DRIFT — multiple terms for the same concept used inconsistently.
   Examples:
   - "platform" vs "system" vs "solution" used interchangeably for the proposed CMS.
   - "user" vs "end user" vs "constituent" for the public visitor.
   - "agency" vs "customer" vs "the State" for NCSBI.
   Pick the term that appears most often as the canonical and recommend the others align to it.
   Usually MAJOR, occasionally MINOR if the different terms are obviously synonymous.

4. VOICE DRIFT — one section's tone clashes with the rest.
   Examples:
   - Section 2 written formally / third-person / passive; Section 7 conversational / first-person / active.
   - One section heavy with marketing superlatives ("world-class", "best-in-class") that don't appear elsewhere.
   MAJOR. Flag the OUTLIER section, not the corpus norm.

5. REDUNDANT REPETITION — the same point made in 3+ sections in similar wording.
   Mostly MINOR. Only flag when the repetition crowds out unique content the section was supposed to deliver. Suggest trimming the duplicate occurrences in the LATER sections.

6. NAMING INCONSISTENCY — company / product / role / person names spelled or rendered differently.
   Examples:
   - "Quadratic" vs "Quadratic Digital" vs "Quadratic Digital LLC" — pick one (usually "Quadratic Digital" in body prose; "Quadratic Digital LLC" only on signature blocks).
   - "Jane Boyd" vs "Jane N. Boyd" — match to the company_profile.json key_personnel entry.
   - "PMO Manager" vs "PMO Lead" vs "Project Management Office Manager" — match to the team roster.
   MINOR — but they multiply in volume so they matter cumulatively.

CRITICAL RULES FOR PROBLEMATIC_TEXT:

The downstream applier does LITERAL STRING MATCH + REPLACE on the draft markdown. So:

- problematic_text MUST be a verbatim substring of the section's current draft. Quote it exactly — including punctuation, capitalization, and any markdown formatting around it.
- Quote the SMALLEST unit that captures the issue. If the issue is just a number, quote the number + minimal context to make it unique in the section (e.g., "3.5 FTE" not the whole sentence). If the issue is a paragraph-level voice shift, quote enough to bound the rewrite.
- NEVER quote multi-paragraph blocks unless the entire block is what needs replacing. The applier will mishandle large rewrites.
- If the same problematic_text appears multiple times in the section, the applier will replace ALL occurrences. Use that deliberately or pick a more unique substring.

CRITICAL RULES FOR SUGGESTED_FIX:

- Same prose register, same grammar, same surrounding context. The applier swaps text — the surrounding paragraph must still read correctly.
- Align to the value/term already stated elsewhere in the corpus. NEVER invent a new commitment, a new number, or a new name.
- For numerical drift / commitment conflict: pick the value that appears in the COST_BUILD or company_profile if available; otherwise the value that appears in the most sections.
- For terminology drift: pick the term that appears most often.
- For naming inconsistency: pick the form that matches company_profile.json key_personnel / team roster.

WHAT TO IGNORE:

- Within-section issues (those are Reviewer A + B's job — not yours).
- Stylistic preferences when the variation is intentional (e.g., headings can be terser than body prose).
- Compliance gaps already flagged as accepted ReviewerFindings (those will be addressed by the next writer pass).
- Any change that requires inventing data not present in the corpus.

If the corpus is consistent, return issues=[]. False positives waste budget and erode trust in auto-apply."""


_USER_TEMPLATE = """Read the entire proposal corpus below as a single document and identify cross-section inconsistencies per your instructions.

=== PROPOSAL CORPUS ===

{corpus}

=== END CORPUS ===

Call report_polish_issues now with every cross-section inconsistency you find. Empty array if the corpus is consistent."""


@dataclass
class PolishIssue:
    """One cross-section consistency issue surfaced by the detector."""

    section_id: str
    issue_type: str
    severity: str
    problematic_text: str
    suggested_fix: str
    cross_section_evidence: str
    rationale: str


def detect_polish_issues(
    *,
    proposal_id: int,
    corpus: str,
) -> list[PolishIssue]:
    """Run the detector against the assembled corpus. Returns a list of
    `PolishIssue` rows ordered by severity (CRITICAL first), or an
    empty list when the corpus is consistent.

    Caller is responsible for assembling the corpus (every drafted
    section + cost narrative + any other content the detector should
    consider). The agent does not load DB rows itself — keeps it
    testable.
    """
    settings = get_settings()
    user_prompt = _USER_TEMPLATE.format(corpus=corpus)

    tool_input, usage = call_tool_for_model(
        model=settings.model_polish_detector,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=8000,
        agent_name="final_polish_detector",
        proposal_id=proposal_id,
    )

    raw_issues = tool_input.get("issues") or []
    log.info(
        "final_polish_detector: proposal %d -> %d issue(s), %s",
        proposal_id,
        len(raw_issues),
        fmt_llm_usage(usage),
    )

    issues: list[PolishIssue] = []
    for f in raw_issues:
        try:
            issues.append(
                PolishIssue(
                    section_id=str(f["section_id"]).strip(),
                    issue_type=str(f["issue_type"]).strip(),
                    severity=str(f["severity"]).strip().upper(),
                    problematic_text=str(f["problematic_text"]),
                    suggested_fix=str(f["suggested_fix"]),
                    cross_section_evidence=str(f.get("cross_section_evidence") or ""),
                    rationale=str(f.get("rationale") or ""),
                )
            )
        except (KeyError, TypeError) as exc:
            log.warning(
                "final_polish_detector: skipping malformed issue %r: %s",
                f,
                exc,
            )

    # Severity sort — CRITICAL first so the applier addresses the most
    # consequential drifts before MINOR cosmetics.
    sev_rank = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
    issues.sort(key=lambda i: sev_rank.get(i.severity, 99))
    return issues


__all__ = [
    "PolishIssue",
    "detect_polish_issues",
]
