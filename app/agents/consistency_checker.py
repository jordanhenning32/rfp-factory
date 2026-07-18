"""Cross-section consistency checker (Reviewer C).

Per-section reviewers (A and B) only see ONE section at a time. They
can't catch the failure mode where section 3 says "Quadratic's team
includes 12 named key personnel" and section 7 says "our 30-person
delivery team will…" — both sound plausible in isolation, but together
they're a credibility-destroying inconsistency.

This checker runs ONCE after all sections finish drafting (i.e., when
the auto-loop has converged or hit the cap). It receives every drafted
section's text in a single Haiku call and flags conflicts about:
- Quantities (staff counts, contract dollar amounts, durations)
- Dates (project start/end, certification dates, deadlines)
- Named entities (customer names spelled differently, person titles
  that conflict)
- Commitments (one section promises X, another implies not-X)

Findings persist as ReviewerAgent.C_CONSISTENCY with category
CROSS_SECTION_INCONSISTENCY. One finding per affected section, so the
user sees the conflict on each section's view in the Findings tab.

Severity is conservative — most cross-section conflicts can't be
auto-fixed (the writer would need to know which value is canonical),
so findings are persisted as MAJOR or MINOR. The auto-loop's
early-exit policy (CRITICAL/MAJOR only) means MAJOR will get auto-
revised IF the user re-runs the auto-loop after picking canonical
values. MINOR stays surfaced for human review.

Cost: one Haiku call with all section drafts as input. ~$0.05-0.10
per proposal end-to-end.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings
from app.core.enums import FindingSeverity
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


_TOOL: dict = {
    "name": "report_inconsistencies",
    "description": (
        "Report cross-section inconsistencies found in the drafted "
        "proposal. Each finding identifies a SPECIFIC factual conflict "
        "between two or more sections — different numbers for the same "
        "thing, different dates, different spellings of the same "
        "entity, contradictory commitments. Skip stylistic / voice "
        "differences (those are Reviewer B's job). Skip "
        "differences that are PROPERLY scoped to each section (e.g., "
        "section 3 talks about staffing, section 7 talks about "
        "subcontractors — different counts are fine). If the proposal "
        "is internally consistent, return an empty findings array. "
        "DO NOT fabricate findings to look thorough — false positives "
        "waste the user's time and erode trust."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "description": (
                    "Inconsistencies found. Empty array when the proposal is internally consistent."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": [s.value for s in FindingSeverity],
                            "description": (
                                "MAJOR — factual conflict that an "
                                "evaluator will notice and ding "
                                "(staff counts, dollar amounts, dates, "
                                "credentials, customer names). MINOR — "
                                "minor wording mismatch the user might "
                                "want to harmonize (e.g., one section "
                                "says 'cloud-native', another says "
                                "'cloud-based'). Use CRITICAL only when "
                                "the conflict is a hard credibility "
                                "killer (e.g., one section claims a "
                                "credential, another denies it)."
                            ),
                        },
                        "subject": {
                            "type": "string",
                            "description": (
                                "1-line summary of WHAT the conflict "
                                "is about — e.g., 'Quadratic team "
                                "size', 'project start date', 'CMS "
                                "customer name spelling'. Used as a "
                                "compact label."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "Full description with quoted "
                                "excerpts from each conflicting "
                                "section. Format: 'Section SEC-003 "
                                'says "...12 named key '
                                'personnel..." but Section SEC-007 '
                                'says "...30-person delivery team '
                                "will...\". These are inconsistent.'"
                            ),
                        },
                        "affected_section_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "List of section_id values (e.g., "
                                "['SEC-003', 'SEC-007']) that contain "
                                "the conflicting claims. At least 2."
                            ),
                        },
                        "suggested_resolution": {
                            "type": "string",
                            "description": (
                                "Concrete action the user can take. "
                                "Usually 'Pick one canonical value (X "
                                "or Y) and update the other "
                                "section(s) to match.' Suggest which "
                                "value is more likely correct if the "
                                "inputs make that obvious (e.g., one "
                                "section cited a KB doc, the other "
                                "didn't)."
                            ),
                        },
                    },
                    "required": [
                        "severity",
                        "subject",
                        "description",
                        "affected_section_ids",
                        "suggested_resolution",
                    ],
                },
            },
        },
        "required": ["findings"],
    },
}


_SYSTEM = """You are Reviewer C — the cross-section consistency checker for Quadratic Digital, a small public-sector software firm. The Writer Team has drafted every section of a proposal independently. Reviewer A (compliance/honesty, per-section) and Reviewer B (persuasion/voice, per-section) have already run. Your job is the one thing they can't do: spot conflicts BETWEEN sections.

WHAT TO FLAG:
- Quantities that don't match: staff counts, FTEs, contract dollar amounts, durations, percentages, dates.
- Named entities that don't match: customer / agency name spellings, person names + titles, partner / subcontractor names.
- Credentials / certifications that conflict: section 3 says "FedRAMP High authorized", section 11 says "we're pursuing FedRAMP authorization".
- Commitments that conflict: schedule dates, deliverable scope, SLA terms.
- Past-performance details that diverge: same project cited in two sections with different outcomes, dates, or contract values.

WHAT NOT TO FLAG:
- Stylistic differences (one section uses "cloud-native", another uses "cloud-based") UNLESS they look like a deliberate inconsistency.
- Differences that are properly scoped (section 3 covers prime staff, section 7 covers subcontractor staff — different counts are fine).
- Voice/tone variation across sections (Reviewer B's territory).
- Single-section issues — those are Reviewer A/B's territory.

SEVERITY GUIDE:
- CRITICAL: a hard credibility killer. One section claims X, another denies X. Submitting this gets the proposal scored down or rejected.
- MAJOR: factual conflict an attentive evaluator will catch. Different numbers for the same thing. Different dates for the same project.
- MINOR: minor mismatch the user might prefer to harmonize but won't kill the bid (e.g., the same idea phrased two slightly different ways).

OUTPUT DISCIPLINE:
- If the proposal is internally consistent, return an EMPTY findings array. Do not invent issues to look thorough — false positives erode trust in the system. Quadratic's drafts are typically pretty consistent; an honest "0 findings" is the expected result on most runs.
- Quote the actual conflicting language from each section (use ellipses for context). Vague descriptions like "the staff count seems different" are not actionable; "Section SEC-003 says '...12 named key personnel...' but Section SEC-007 says '...30-person delivery team will...'" is.
- List EVERY affected section_id in affected_section_ids. The system persists one finding per affected section so the user sees the conflict on each section's view.

Use the report_inconsistencies tool to return your findings."""


_USER_TEMPLATE = """Below is every drafted section of the proposal. Each section is preceded by its section_id (SEC-NNN) and section_title for citation purposes. Read all sections together, then call report_inconsistencies with any cross-section conflicts you find.

{sections_block}

Use the report_inconsistencies tool. Empty findings array when the proposal is internally consistent."""


@dataclass
class ConsistencyFinding:
    """One inconsistency. The orchestrator persists ONE ReviewerFinding
    row per affected_section_id (so each section's Findings tab shows
    the conflict)."""

    severity: str
    subject: str
    description: str
    affected_section_ids: list[str]
    suggested_resolution: str


# Total content-character budget the Haiku call will accept across all
# section drafts combined. Sized to keep this call well under Haiku's
# 200K-token context window while leaving room for the system prompt,
# tool schema, and response — and to keep input cost predictable
# (~$0.04 at Haiku pricing). Larger proposals get per-section
# truncation rather than failing or paying for an oversized call.
_TOTAL_INPUT_BUDGET_CHARS = 150_000

# Per-section floor when truncation kicks in. Below this, the truncated
# section is too small to carry any cross-reference signal, so we keep
# at least this many chars of the lead even if the proportional split
# would go lower. Picked to fit a section's intro paragraph + first
# commitment paragraph.
_MIN_SECTION_CHARS_AFTER_TRUNCATION = 1_500


def _truncate_for_budget(text: str, budget: int) -> tuple[str, int]:
    """Trim a section draft to `budget` chars, preserving the lead.
    The lead typically contains the section's main claims (entity
    references, dollar amounts, dates) — the tail is mostly transition
    / summary. Returns (truncated_text, chars_dropped). When the input
    fits within budget, returns the original unchanged."""
    if len(text) <= budget:
        return text, 0
    keep = max(budget, _MIN_SECTION_CHARS_AFTER_TRUNCATION)
    if len(text) <= keep:
        return text, 0
    head = text[:keep].rstrip()
    dropped = len(text) - len(head)
    return (
        head + f"\n[…{dropped:,} chars truncated for input-budget — "
        f"if a cross-section claim isn't visible, the section may "
        f"contradict another section beyond this point.]",
        dropped,
    )


def _format_sections_for_prompt(
    sections: list[dict],
) -> tuple[str, dict]:
    """Compact rendering — section_id + title header, then the draft
    markdown. Returns (formatted_block, stats). Stats:
        n_sections      — number of sections rendered
        total_chars     — total chars (post-truncation) sent to the LLM
        n_truncated     — count of sections that were truncated
        chars_dropped   — total chars dropped across all truncations

    Truncation kicks in only when the un-truncated total would exceed
    `_TOTAL_INPUT_BUDGET_CHARS`. When it does, every section gets a
    proportional cap so the cross-section visibility stays balanced
    instead of letting one giant section eat the whole budget.
    """
    drafts = [(s, (s.get("draft_md") or "").strip()) for s in sections]
    drafts = [(s, d) for s, d in drafts if d]
    if not drafts:
        return "", {
            "n_sections": 0,
            "total_chars": 0,
            "n_truncated": 0,
            "chars_dropped": 0,
        }

    total_unbounded = sum(len(d) for _, d in drafts)
    if total_unbounded <= _TOTAL_INPUT_BUDGET_CHARS:
        per_section_budget: int | None = None
    else:
        # Proportional split. With many sections this can go below
        # _MIN_SECTION_CHARS_AFTER_TRUNCATION; the floor in
        # _truncate_for_budget protects each section from being
        # trimmed below useful size, with the side effect that the
        # actual total may exceed the budget on very-many-section
        # proposals. That's acceptable — Haiku's context window has
        # ample headroom and the alternative is sending uselessly
        # short stubs.
        per_section_budget = max(
            _TOTAL_INPUT_BUDGET_CHARS // len(drafts),
            _MIN_SECTION_CHARS_AFTER_TRUNCATION,
        )

    blocks: list[str] = []
    n_truncated = 0
    chars_dropped = 0
    for s, draft in drafts:
        if per_section_budget is None:
            rendered = draft
        else:
            rendered, dropped = _truncate_for_budget(
                draft,
                per_section_budget,
            )
            if dropped:
                n_truncated += 1
                chars_dropped += dropped
        sec_id = s.get("section_id") or "SEC-???"
        title = s.get("section_title") or "(untitled)"
        blocks.append(f"=== {sec_id}: {title} ===\n{rendered}")

    formatted = "\n\n".join(blocks)
    stats = {
        "n_sections": len(drafts),
        "total_chars": len(formatted),
        "n_truncated": n_truncated,
        "chars_dropped": chars_dropped,
    }
    return formatted, stats


def check_proposal_consistency(
    *,
    proposal_id: int,
    sections: list[dict],
) -> list[ConsistencyFinding]:
    """Run Reviewer C against all drafted sections of a proposal.

    `sections` is a list of dicts each with at least: section_id,
    section_title, draft_md. Sections without a draft are skipped.
    Returns the list of inconsistencies; the orchestrator persists.

    Single Haiku call regardless of section count — the input is
    bounded by total draft text (~30-80k chars on a typical proposal,
    well within Haiku's context window).
    """
    settings = get_settings()

    drafted = [s for s in sections if (s.get("draft_md") or "").strip()]
    if len(drafted) < 2:
        # Nothing to compare against; consistency is vacuously true.
        log.info(
            "consistency_checker: %d drafted section(s) — fewer than 2, skipping cross-section check.",
            len(drafted),
        )
        return []

    sections_block, fmt_stats = _format_sections_for_prompt(drafted)
    if fmt_stats["n_truncated"]:
        log.warning(
            "consistency_checker: input over budget on proposal %d — "
            "truncated %d/%d section(s), dropped %d chars total. "
            "Cross-section conflicts beyond the truncation point "
            "won't be detected this run.",
            proposal_id,
            fmt_stats["n_truncated"],
            fmt_stats["n_sections"],
            fmt_stats["chars_dropped"],
        )

    user_prompt = _USER_TEMPLATE.format(sections_block=sections_block)

    tool_input, usage = call_tool_for_model(
        model=settings.model_light_extraction,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        # 4000 was too tight on 9+ sections — Haiku ran out mid-tool-call
        # and the empty `findings` array silently looked like "0 issues."
        # 12000 gives ~10-15 verbose findings of headroom; worst-case extra
        # output cost is ~$0.06 at Haiku pricing.
        max_tokens=12000,
        agent_name="consistency_checker",
        proposal_id=proposal_id,
    )

    if usage.get("stop_reason") == "max_tokens":
        # Truncated mid-tool-call. An empty `findings` array here means
        # Haiku ran out of output budget, NOT that the proposal is
        # consistent — don't let this become a silent-zero. Match the
        # failure shape of the "no tool_use block" path in llm.py so the
        # orchestrator records FAILED and surfaces the error.
        n_partial = len(tool_input.get("findings", []) or [])
        raise RuntimeError(
            f"consistency_checker: output truncated at max_tokens "
            f"(in={usage['input_tokens']}, out={usage['output_tokens']}). "
            f"Got {n_partial} partial finding(s) before truncation. "
            f"Bump max_tokens or split the input."
        )

    raw = tool_input.get("findings", []) or []
    trunc_tag = (
        f", truncated {fmt_stats['n_truncated']}/{fmt_stats['n_sections']} "
        f"sections (-{fmt_stats['chars_dropped']:,} chars)"
        if fmt_stats["n_truncated"]
        else ""
    )
    log.info(
        "consistency_checker: proposal %d — %d section(s) checked%s, %d inconsistency(ies) found, %s",
        proposal_id,
        len(drafted),
        trunc_tag,
        len(raw),
        fmt_llm_usage(usage),
    )

    findings: list[ConsistencyFinding] = []
    for f in raw:
        try:
            affected = list(f.get("affected_section_ids") or [])
            if len(affected) < 2:
                # A consistency finding that doesn't span sections is
                # malformed — Reviewer A/B should have caught it.
                log.debug(
                    "consistency_checker: dropping single-section finding (subject=%r)",
                    f.get("subject", "?"),
                )
                continue
            findings.append(
                ConsistencyFinding(
                    severity=str(f["severity"]).upper(),
                    subject=str(f.get("subject") or "(no subject)"),
                    description=str(f.get("description") or ""),
                    affected_section_ids=[str(s) for s in affected],
                    suggested_resolution=str(f.get("suggested_resolution") or ""),
                )
            )
        except (KeyError, TypeError) as exc:
            log.warning(
                "consistency_checker: skipping malformed finding %r: %s",
                f,
                exc,
            )

    return findings


__all__ = ["ConsistencyFinding", "check_proposal_consistency"]
