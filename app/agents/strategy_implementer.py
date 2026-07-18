"""Strategy Implementer — translates the cached cost-review strategy
into per-section directives that the Writer Team can apply.

User flow:
  1. User clicks 'Generate Strategy' on the Cost Review tab. Sonnet
     produces a markdown plan (free-form). The plan is cached on
     the Proposal row so the user can re-open it.
  2. User clicks 'Apply Strategy to Document'. THIS agent reads the
     cached strategy plus the outline / section briefs / findings /
     cost build summary, and emits ONE actionable directive per
     section the strategy actually implicates. Sections the
     strategy doesn't touch get NO directive — we don't busy-work
     the writer.
  3. User reviews the directives in a preview dialog (each editable
     before apply). On approval, each directive is forwarded to
     spawn_writer_for_section(..., user_directive=...) — the
     existing per-section regenerate path. Sections with already-
     resolved [NEEDS_HUMAN] placeholders survive thanks to the
     carry-forward pass we ship in services.needs_human.

Why a separate agent (vs. just feeding the strategy markdown as a
directive into every section)?
  - Strategies are document-wide; section writers only see one
    section's brief. Without the implementer, the Cover Letter
    writer would have to parse the entire 8K-char strategy to find
    the bullet that applies to it, AND every other section's
    writer would do the same parsing in parallel. Wasteful and
    error-prone.
  - The implementer scopes directives to relevant sections only.
    A strategy with two cost-positioning bullets becomes 2-3
    targeted directives, not 9 sections × full-strategy directive.
  - Cost-deferred sections (requires_cost_analysis=True) are
    skipped — those are drafted by the Cost Volume Writer, which
    already consumes the cost build directly. Excluded sections
    are skipped for the obvious reason.

Single Sonnet 4.6 tool call. ~$0.05-0.15 depending on prompt size.
Output is structured (tool input) so the UI can preview and the
job orchestrator can fan out cleanly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import get_settings
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


@dataclass
class StrategyDirective:
    """One per-section directive emitted by the implementer."""

    section_id: str
    directive: str
    rationale: str
    priority: str  # "high" | "medium" | "low"
    estimated_changes: str  # "minor" | "moderate" | "substantial"


@dataclass
class StrategyImplementerResult:
    directives: list[StrategyDirective] = field(default_factory=list)


_PRIORITIES = ("high", "medium", "low")
_CHANGE_SCALES = ("minor", "moderate", "substantial")


_TOOL: dict = {
    "name": "report_strategy_directives",
    "description": (
        "Emit one directive per section the cost-review strategy "
        "actually implicates. Sections the strategy does NOT touch "
        "must NOT appear — empty array is acceptable when the "
        "strategy is purely about cost-build mutations with no "
        "narrative implications. Each directive is forwarded "
        "verbatim into the Writer Team as a USER DIRECTIVE block, "
        "so it must be specific, actionable, and self-contained "
        "(do not reference 'the strategy' — restate the substance)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "directives": {
                "type": "array",
                "description": ("One entry per affected section. Skip sections with no relevant change."),
                "items": {
                    "type": "object",
                    "properties": {
                        "section_id": {
                            "type": "string",
                            "description": (
                                "Must match a section_id from the "
                                "ELIGIBLE SECTIONS list. Cost-"
                                "deferred and excluded sections "
                                "are NOT in that list and must NOT "
                                "be referenced."
                            ),
                        },
                        "directive": {
                            "type": "string",
                            "description": (
                                "1-3 sentences, written as USER "
                                "DIRECTIVE for the section writer. "
                                "Concrete and actionable: name the "
                                "specific change in tone, framing, "
                                "numbers, or emphasis. Cite "
                                "specific findings or strategy "
                                "bullets when they drive the "
                                "change. Do NOT say 'apply the "
                                "strategy' or 'follow the plan' — "
                                "restate the substance because "
                                "the writer does NOT see the "
                                "strategy markdown."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "1-2 sentences for the user "
                                "preview. Which strategy bullets "
                                "or findings drove this directive. "
                                "Helps the user decide whether to "
                                "apply or skip this section."
                            ),
                        },
                        "priority": {
                            "type": "string",
                            "enum": list(_PRIORITIES),
                            "description": (
                                "high = blocks competitive "
                                "submission if not addressed. "
                                "medium = meaningful improvement. "
                                "low = polish."
                            ),
                        },
                        "estimated_changes": {
                            "type": "string",
                            "enum": list(_CHANGE_SCALES),
                            "description": (
                                "minor = a sentence or two of "
                                "wording. moderate = a paragraph "
                                "or section reframe. substantial "
                                "= rewriting most of the section."
                            ),
                        },
                    },
                    "required": [
                        "section_id",
                        "directive",
                        "rationale",
                        "priority",
                        "estimated_changes",
                    ],
                },
            },
        },
        "required": ["directives"],
    },
}


_SYSTEM = """You are the Strategy Implementer for a federal-proposal team. The Cost Reviewer raised findings. The Cost Strategist synthesized those findings into one coherent narrative strategy. Your job is to translate that strategy into specific, actionable directives for the proposal's section writers.

CORE DISCIPLINE:

1. SCOPE TIGHTLY. Emit a directive ONLY for a section the strategy actually implicates. If the strategy is entirely about cost-build mutations (margin, hours, ODCs) with no narrative implications, return an empty directives array. Do NOT generate filler directives to look productive.

2. EACH DIRECTIVE STANDS ALONE. The section writer does NOT see the strategy markdown — they see only their section brief plus your USER DIRECTIVE block. So restate the substance: instead of 'follow the strategy', say 'reframe pricing as deliberately competitive, citing the adjusted MEDIUM scenario at 20% margin (was 25%) which positions us at $1.21M, just inside the $1.2M market band high.' The writer must be able to act with no other context.

3. PER-SECTION FOCUS. The Cover Letter writer cares about positioning and audience. The Pricing-Approach writer cares about cost narrative. The Methodology writer cares about staffing and risk. Tailor each directive to what THAT section is responsible for. A single strategic decision (e.g., 'reduce margin to 20%') will produce DIFFERENT directives for different sections, each phrased for that section's role.

4. CITE FINDINGS WHEN RELEVANT. If a directive maps to a specific Cost Reviewer finding, name the finding (severity + subject) inside the rationale field so the user can verify alignment.

5. RESPECT WRITER GUARDRAILS. The Writer Team has hard rules: only KB-cited past performance, no invented numbers, no overstated FTE, [NEEDS_HUMAN] placeholders for uncommittable specifics. Your directive must NOT instruct the writer to violate any of these. If the strategy implies an honesty-violating claim (e.g., 'lead with FedRAMP High status' when the company doesn't have it), DO NOT pass that through — flag it in the rationale and lower the priority instead.

6. VALUE-FIRST POSITIONING — NON-NEGOTIABLE. Quadratic competes on VALUE, not price. NEVER emit a directive that asks the writer to acknowledge being "above market", concede competitive cost pressure, apologize for premium pricing, or reframe Quadratic's bid as "price-competitive." Pricing/margin decisions happen on the Cost tab — section narratives must position pricing as deliberate value investment (AI-accelerated delivery matching COTS timelines, deeper SME bench with named senior personnel, lower 5-year TCO with no licensing creep, fit-to-purpose customization). The proposal is a SALES DOCUMENT — the writer is creating WANT for the product, not justifying its cost to a skeptic.

If a strategy bullet implies a competitive concession in narrative form, REFRAME the directive to its value-justification equivalent. Examples:
  ✗ WRONG: "In the executive summary, acknowledge that our pricing is above the $1.2M market high and explain the value gap."
  ✓ RIGHT: "In the executive summary, lead with our AI-accelerated 12-month delivery and integrated security expertise that justify the investment level — frame the bid as fit-for-purpose total cost of ownership, not unit price."

  ✗ WRONG: "In the Pricing Approach narrative, address the cost concern by reducing apparent rate ceilings."
  ✓ RIGHT: "In the Pricing Approach narrative, present pricing as deliberate scope investment: the labor allocation buys authoring artifacts (SSP, ConMon design, training collateral) that COTS vendors expense as separate Tier-3 SOWs."

When the upstream strategy has correctly framed things as value-positioning, your directives translate that faithfully. When the strategy hasn't (rare, but possible if the strategist had limited context), YOU are the last line of defense before the writer team produces apologetic narrative.

7. SKIP COST-DEFERRED AND EXCLUDED SECTIONS. The ELIGIBLE SECTIONS list excludes them already. Do NOT reference any section_id outside that list. The Cost Volume Writer drafts cost-deferred sections directly from the cost build; it does not need a directive from you.

PRIORITY GUIDANCE:
  - high: addresses a CRITICAL or MAJOR finding, OR a positioning change that directly affects evaluator scoring.
  - medium: addresses a MAJOR or MINOR finding, OR meaningful tone/framing improvement.
  - low: polish that improves the section but doesn't move the bid.

ESTIMATED-CHANGES GUIDANCE:
  - minor: ~1-3 sentences of edits to the existing draft.
  - moderate: rewrite a paragraph or restructure one subsection.
  - substantial: rewrite most of the section.

The user pays per section regenerate (~$0.50/section), so be honest about which sections genuinely need a touch."""


_USER_TEMPLATE = """Translate this cost-review strategy into per-section directives.

=== COST-REVIEW STRATEGY (synthesized markdown) ===
{strategy_markdown}

=== ELIGIBLE SECTIONS (cost-deferred / excluded already filtered out) ===
{sections_block}

=== ACTIVE COST-REVIEW FINDINGS (after user accept/reject) ===
{findings_block}

=== COST BUILD SUMMARY (current numbers — directives can reference these) ===
{cost_build_summary}

Call report_strategy_directives now. Empty array is acceptable when the strategy is purely cost-build mutations with no narrative implications. Otherwise emit directives ONLY for sections the strategy materially implicates."""


def synthesize_directives(
    *,
    proposal_id: int,
    strategy_markdown: str,
    sections_block: str,
    findings_block: str,
    cost_build_summary: str,
    eligible_section_ids: set[str],
) -> StrategyImplementerResult:
    """Run the implementer. Returns a StrategyImplementerResult with
    one directive per implicated section. Validates that emitted
    section_ids belong to the eligible set; drops any that don't
    (defensive — the prompt says they must, but LLMs occasionally
    invent ids)."""
    settings = get_settings()
    user_prompt = _USER_TEMPLATE.format(
        strategy_markdown=strategy_markdown.strip() or "(no strategy cached)",
        sections_block=sections_block or "(no eligible sections)",
        findings_block=findings_block or "(no active findings)",
        cost_build_summary=cost_build_summary or "(no cost build)",
    )

    tool_input, usage = call_tool_for_model(
        model=settings.model_strategy_implementer,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tool=_TOOL,
        max_tokens=8000,
        agent_name="strategy_implementer",
        proposal_id=proposal_id,
    )

    if usage.get("stop_reason") in ("max_tokens", "length"):
        n_partial = len(tool_input.get("directives") or [])
        raise RuntimeError(
            f"strategy_implementer: output truncated at "
            f"max_tokens (in={usage['input_tokens']}, "
            f"out={usage['output_tokens']}). Got {n_partial} "
            f"partial directive(s) before truncation."
        )

    directives: list[StrategyDirective] = []
    n_dropped_unknown = 0
    n_dropped_malformed = 0
    for d in tool_input.get("directives") or []:
        try:
            section_id = str(d["section_id"]).strip()
            if section_id not in eligible_section_ids:
                n_dropped_unknown += 1
                log.warning(
                    "strategy_implementer: dropping directive for section_id=%r (not in eligible set)",
                    section_id,
                )
                continue
            directive_text = str(d.get("directive") or "").strip()
            rationale = str(d.get("rationale") or "").strip()
            priority = str(d.get("priority") or "medium").lower()
            if priority not in _PRIORITIES:
                priority = "medium"
            est = str(d.get("estimated_changes") or "moderate").lower()
            if est not in _CHANGE_SCALES:
                est = "moderate"
            if not directive_text:
                n_dropped_malformed += 1
                log.warning(
                    "strategy_implementer: dropping empty directive for section_id=%r",
                    section_id,
                )
                continue
            directives.append(
                StrategyDirective(
                    section_id=section_id,
                    directive=directive_text,
                    rationale=rationale,
                    priority=priority,
                    estimated_changes=est,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            n_dropped_malformed += 1
            log.warning(
                "strategy_implementer: skipping malformed directive %r: %s",
                d,
                exc,
            )

    log.info(
        "strategy_implementer: %d directive(s) for proposal %d "
        "(dropped %d unknown section_id, %d malformed) (%s)",
        len(directives),
        proposal_id,
        n_dropped_unknown,
        n_dropped_malformed,
        fmt_llm_usage(usage),
    )
    return StrategyImplementerResult(directives=directives)


__all__ = [
    "StrategyDirective",
    "StrategyImplementerResult",
    "synthesize_directives",
]
