"""Reviewer B — persuasion & evaluator psychology check (Gemini by default).

Per design doc §6.5. Reviewer B asks: "Will this section actually win the
evaluation criteria?" The training distribution intentionally differs from
Reviewer A's — different blind spots, different lens, second-opinion role.

Categories Reviewer B owns (disjoint from A):
- weak_persuasion: claims state facts without making the case for WHY they
  matter to this evaluator.
- voice_inconsistency: tone/register clashes with Quadratic's small-firm
  authentic voice or with adjacent sections.
- evaluator_misalignment: section doesn't speak to the actual Section M
  scoring criteria; buries the lede; fails to differentiate.

Reviewer B does NOT check honesty / citations / compliance — that's
Reviewer A's job. If Reviewer B notices a hallucination it can be flagged,
but priority is the persuasion lens.

The model is Gemini (gemini-2.0-flash by default) for provider diversity.
Override via settings.model_reviewer_b.
"""

from __future__ import annotations

import logging

from app.agents.reviewer_a import ReviewerFindingDraft
from app.config import get_settings
from app.core.enums import FindingCategory, FindingSeverity
from app.services.lessons import format_reviewer_guidance
from app.services.llm import fmt_llm_usage, get_gemini

log = logging.getLogger(__name__)


_REVIEWER_B_CATEGORIES = [
    FindingCategory.WEAK_PERSUASION.value,
    FindingCategory.VOICE_INCONSISTENCY.value,
    FindingCategory.EVALUATOR_MISALIGNMENT.value,
]


_TOOL: dict = {
    "name": "report_findings",
    "description": (
        "Report persuasion / evaluator-psychology findings against this "
        "drafted section. Most government-RFP drafts have at least 2-4 "
        "improvements available — sharper lead paragraph, evaluator-"
        "language mirroring, weaker hedge words, voice drift. Empty array "
        "ONLY when the section is genuinely strong end-to-end on every "
        "dimension below; that's rare on a first pass."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": [s.value for s in FindingSeverity],
                            "description": (
                                "CRITICAL = section actively hurts the bid "
                                "(buries the most important point, contradicts "
                                "evaluator priorities, off-tone in a way the "
                                "evaluator will notice). "
                                "MAJOR = section is workable but leaves clear "
                                "evaluation points on the table. "
                                "MINOR = stylistic improvement, small voice "
                                "inconsistency."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": _REVIEWER_B_CATEGORIES,
                            "description": (
                                "weak_persuasion = states facts without "
                                "explaining why they matter to the evaluator. "
                                "voice_inconsistency = tone/register issue "
                                "(too marketing-y, too academic, inconsistent "
                                "with neighboring sections, off-key for "
                                "small-business framing). "
                                "evaluator_misalignment = doesn't speak to "
                                "Section M criteria, buries the lede, fails "
                                "to differentiate, ignores the agency's "
                                "stated priorities."
                            ),
                        },
                        "finding_text": {
                            "type": "string",
                            "description": (
                                "1-3 sentences naming the issue with a specific quote where applicable."
                            ),
                        },
                        "suggested_fix": {
                            "type": "string",
                            "description": (
                                "1-3 sentences telling the Writer what to "
                                "change. Be specific — name the paragraph, "
                                "the angle to lead with, or the reframing."
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


_SYSTEM = """You are Reviewer B for Quadratic Digital's RFP responses. Your job: PERSUASION + EVALUATOR PSYCHOLOGY.

MINDSET — this is the most important rule:
You are an ACTIVE CRITIC, not a rubber stamp. Government-RFP drafts almost always have improvement opportunities — sharper lead, better evaluator-language mirroring, weaker hedge words, voice drift, structural lede-burying. Your default expectation is that EVERY section has at least 2-4 findings (mostly MINOR + a few MAJOR). The cost of missing improvement opportunities is HIGHER than the cost of flagging too much — Reviewer A handles "is this true"; you handle "will this win." If you're tempted to return empty, run the explicit checklist at the bottom and reconsider.

You are the SECOND opinion. Reviewer A handles compliance, citations, hallucinations, overcommitments — DO NOT flag those (even if you spot one, leave it; trust Reviewer A). Your lens is purely persuasion + evaluator psychology.

WHAT TO FLAG — go through each lens:

1. EVALUATOR_MISALIGNMENT — does the section win the eval criteria?
   - Lead paragraph: does it open with the strongest point relative to Section M's weights, or does it open with generic context/setup? Buried lede = MAJOR finding.
   - Mirroring: does the section reuse the RFP's own phrasing where natural ("evidence-based approach", "innovative methodology", "scalable architecture")? Generic synonyms leave evaluator-recognition points on the table.
   - Differentiation: does anything explain why Quadratic specifically (vs. any other competent vendor) wins this? "We have X capability" is generic; "Quadratic uniquely combines X with Y, which means Z for NCCCS" is differentiating.
   - Outcome focus: does each capability claim connect to an agency-side outcome? "We use Kubernetes" is a feature; "We use Kubernetes so the platform auto-scales during enrollment surges, reducing student wait times" is an outcome.

2. WEAK_PERSUASION — common offenders:
   - Hedge words: "we believe", "we feel", "we are confident", "we strive to". Replace with direct assertion + evidence.
   - Defensive framing: "Although Quadratic is small..." or "Despite our size...". Lead with strength, not apology.
   - Bare capability lists with no prioritization: pick the 2-3 strongest and elevate them; bury or cut the rest.
   - Long unbroken paragraphs in a section that should scan: evaluators speed-read, so structure for scanning.
   - Closing that fizzles: section ends without a "so what for the agency" punch line.

3. VOICE_INCONSISTENCY — Quadratic's voice anchor is confident, direct, plain English, authentic small-firm. Flag any of:
   - Marketing puffery: "industry-leading", "world-class", "best-in-class", "cutting-edge", "synergies", "leverage" (overused), "robust", "seamless", "holistic".
   - Tone drift between sections — if neighboring sections in the outline have different registers (one breezy, one academic, one salesy), flag the outlier.
   - Generic vendor cliches that any firm could write — small-firm authentic > corporate template.

SEVERITY GUIDE:
- CRITICAL — section actively hurts the bid (evaluator will mark it down or skip it). Use SPARINGLY.
- MAJOR — clear evaluation points left on the table (buried lede, missing differentiation, defensive framing in a high-weight section).
- MINOR — sharper word choice, hedge cleanup, a single weak sentence. The bulk of your findings will be MINOR. That's expected.

WHAT NOT TO FLAG:
- Honesty / citation / compliance issues — Reviewer A's job.
- [NEEDS_HUMAN] placeholders themselves — they're intentional gaps.
- Things the section_brief explicitly told the writer to do (don't second-guess the outline).
- Pure preference ("I'd have used a different word") without a persuasion rationale.

EXPLICIT CHECKLIST — run through this before returning. If the answer to any is "could be sharper," that's a finding:
□ Does the lead paragraph open with the strongest argument for this section?
□ Does the prose mirror evaluator language from the RFP / Section M?
□ Does every capability claim connect to an agency-side outcome?
□ Are there hedge words ("believe", "strive", "feel", "confident") to remove?
□ Are there marketing cliches ("leverage", "world-class", "synergies", "robust") to cut?
□ Does the section end with a punch line, or does it fizzle?
□ Does the tone match neighboring sections in the outline?

OUTPUT: call report_findings with one entry per issue. Most sections produce 2-4 findings; some produce more. Empty array ONLY if every checklist item above genuinely passes — if you're returning empty, double-check the lead paragraph and the closing one more time.
"""


_CACHED_PREFIX_TEMPLATE = """=== QUADRATIC DIGITAL COMPANY PROFILE (canonical) ===
{profile_json}

=== PROPOSAL OUTLINE (every section — voice and tone anchor) ===
{outline_text}

=== COMPLIANCE MATRIX (especially evaluation_criterion items — these are what the agency scores) ===
{compliance_text}

=== RFP SOURCE EXCERPTS (Section L instructions + Section M evaluation criteria) ===
{rfp_text}
"""


_USER_TEMPLATE = """Review section {section_id} for persuasion and evaluator psychology.

Title: {section_title}
Page limit: {page_limit}
Word limit: {word_limit}

Section brief (what the outline told the writer to do):
{section_brief}

DRAFTED MARKDOWN:
\"\"\"
{draft_markdown}
\"\"\"

Use the report_findings tool. Empty findings array if the section is strong."""


def build_cached_prefix(
    *,
    profile_json: str,
    outline_text: str,
    compliance_text: str,
    rfp_text: str,
) -> str:
    return _CACHED_PREFIX_TEMPLATE.format(
        profile_json=profile_json,
        outline_text=outline_text,
        compliance_text=compliance_text,
        rfp_text=rfp_text,
    )


def review_section(
    *,
    proposal_id: int,
    section_id: str,
    section_title: str,
    section_brief: str,
    page_limit: int | None,
    word_limit: int | None,
    draft_markdown: str,
    cached_prefix: str,
) -> list[ReviewerFindingDraft]:
    """Run Reviewer B on one section. Returns the raw findings list."""
    settings = get_settings()
    client = get_gemini()

    user_prompt = _USER_TEMPLATE.format(
        section_id=section_id,
        section_title=section_title,
        page_limit=page_limit if page_limit is not None else "none",
        word_limit=word_limit if word_limit is not None else "none",
        section_brief=section_brief or "(none)",
        draft_markdown=draft_markdown or "(empty)",
    )

    # Append learned reviewer-calibration rules + per-category dismiss-rate
    # stats so the model can suppress patterns the user has historically
    # rejected as false positives.
    system_with_guidance = _SYSTEM + format_reviewer_guidance(
        reviewer="B",
        categories=_REVIEWER_B_CATEGORIES,
    )
    tool_input, usage = client.call_tool(
        model=settings.model_reviewer_b,  # gemini-2.0-flash by default
        system=system_with_guidance,
        cached_prefix=cached_prefix,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=4000,
        agent_name="reviewer_b",
        proposal_id=proposal_id,
    )

    raw = tool_input.get("findings", []) or []
    log.info(
        "reviewer_b: section %s -> %d finding(s), %s",
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
            log.warning("reviewer_b: skipping malformed finding %r: %s", f, exc)
    return findings
