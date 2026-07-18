"""Lessons-Learned service.

Turns user accept/dismiss actions on reviewer findings into durable
guidance rules the agents pick up on the next run.

Two channels:
1. Accept → writer-avoidance rule. The user agreed the finding was real,
   so the writer should not produce that pattern again.
2. Dismiss-with-reason → reviewer-calibration rule. The user said the
   reviewer was wrong, so the reviewer should not flag that pattern.

Extraction is best-effort: a small Haiku call generates a one-line rule
from the finding + suggested fix (or dismissal reason). Failures are
logged but don't break the user's accept/dismiss flow.

Rules start as `draft` and only get injected after the user approves
them from the Learned Guidance tab. This is the curation gate that
prevents one bad accept/dismiss from poisoning every future draft.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select

from app.config import get_settings
from app.core.enums import FindingCategory
from app.db.session import session_scope
from app.models import (
    LearnedRule,
    Proposal,
    ProposalOutcome,
    ProposalSection,
    ReviewerFinding,
)
from app.services.llm import call_tool_for_model, fmt_llm_usage

log = logging.getLogger(__name__)


# --- Extraction tool spec --------------------------------------------------

_EXTRACT_TOOL: dict = {
    "name": "report_learned_rule",
    "description": (
        "Distill a single, generalizable guidance rule from this user "
        "action on a reviewer finding. The rule will be injected into "
        "future agent prompts, so it must read as durable guidance — not "
        "as a comment on this one finding."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rule_text": {
                "type": "string",
                "description": (
                    "ONE OR TWO sentences. Imperative voice. State the "
                    "rule directly without referring to 'this finding' or "
                    "'in this case'. Generalize from the specific instance "
                    "to the underlying pattern. Examples: 'Do not include "
                    "specific numbered delivery days (day 40, week 6) in "
                    "narrative text — wrap them in [NEEDS_HUMAN] if "
                    "intentional.' / 'Do not flag opening paragraphs that "
                    "lead with a domain-shift acknowledgement when the "
                    "section_brief explicitly calls for that framing.'"
                ),
            },
            "extractable": {
                "type": "boolean",
                "description": (
                    "False if no generalizable rule exists (the finding "
                    "was too specific, contradictory with itself, or "
                    "impossible to generalize without losing meaning). "
                    "When false, rule_text may be empty. The system will "
                    "skip persisting the rule."
                ),
            },
        },
        "required": ["rule_text", "extractable"],
    },
}


_ACCEPT_SYSTEM = """You distill durable writing rules from user-accepted reviewer findings on government RFP responses.

The user has confirmed that a reviewer's finding is correct — meaning the writer DID produce content that should not have been produced. Your job: extract a one-line GENERALIZED rule the writer should follow on every future section, not just this one.

GUIDELINES:
- Be specific enough to act on, general enough to apply across many sections.
- Prefer "Do not <behavior>" or "Wrap <pattern> in [NEEDS_HUMAN]" framings.
- Refer to the underlying pattern, NOT the specific section/proposal/quote.
- One sentence is best; two if necessary. Never more.
- If the finding is genuinely one-off (peculiar to this proposal's context), set extractable=false.

Output via the report_learned_rule tool."""


_DISMISS_SYSTEM = """You distill reviewer-calibration rules from user-DISMISSED reviewer findings on government RFP responses.

The user has rejected a reviewer's finding — meaning the reviewer flagged something it should NOT have flagged. The user's dismissal reason explains why. Your job: extract a one-line GENERALIZED rule the reviewer should follow next time, so the same false positive doesn't recur.

GUIDELINES:
- Frame as "Do NOT flag <pattern>" with a brief justification.
- Refer to the pattern + the reason it's acceptable, NOT the specific section/proposal/quote.
- The user's dismissal reason is the gold here — base the rule on it.
- One sentence is best; two if necessary. Never more.
- If the dismissal reason is too thin or specific to extract a general rule, set extractable=false.

Output via the report_learned_rule tool."""


_ACCEPT_USER_TEMPLATE = """The user accepted this reviewer finding (so the writer must not repeat the pattern).

Reviewer: {reviewer}
Severity: {severity}
Category: {category}

Finding:
\"\"\"
{finding_text}
\"\"\"

Reviewer's suggested fix:
\"\"\"
{suggested_fix}
\"\"\"

Distill a one-line generalized rule for the WRITER. Use report_learned_rule."""


_DISMISS_USER_TEMPLATE = """The user dismissed this reviewer finding as a false positive.

Reviewer: {reviewer}
Severity: {severity}
Category: {category}

Finding:
\"\"\"
{finding_text}
\"\"\"

Reviewer's suggested fix (which the user rejected):
\"\"\"
{suggested_fix}
\"\"\"

User's dismissal reason:
\"\"\"
{dismissal_reason}
\"\"\"

Distill a one-line generalized calibration rule for the REVIEWER (so it doesn't flag this pattern again). Use report_learned_rule."""


# --- Internal helpers ------------------------------------------------------


def _snapshot_finding(finding_id: int) -> dict | None:
    """Pull the finding's fields out of the session before we hand it to a
    background thread. The thread can't safely share a session."""
    with session_scope() as db:
        f = db.get(ReviewerFinding, finding_id)
        if f is None:
            return None
        return {
            "id": f.id,
            "reviewer_agent": (
                f.reviewer_agent.value if hasattr(f.reviewer_agent, "value") else str(f.reviewer_agent)
            ),
            "severity": (f.severity.value if hasattr(f.severity, "value") else str(f.severity)),
            "category": (f.category.value if hasattr(f.category, "value") else str(f.category)),
            "finding_text": f.finding_text or "",
            "suggested_fix": f.suggested_fix or "",
            "dismissed_reason": f.dismissed_reason or "",
        }


def _persist_rule(
    *,
    kind: str,
    rule_text: str,
    finding: dict,
    action: str,
) -> int | None:
    """Insert a learned_rules row. Returns the row id or None if the rule
    text is empty."""
    rule_text = (rule_text or "").strip()
    if not rule_text:
        return None
    with session_scope() as db:
        row = LearnedRule(
            kind=kind,
            rule_text=rule_text,
            source_finding_id=finding["id"],
            source_action=action,
            source_category=finding["category"],
            source_severity=finding["severity"],
            source_reviewer=finding["reviewer_agent"],
            status="draft",
        )
        db.add(row)
        db.flush()
        return row.id


def _run_extraction(*, kind: str, finding: dict, action: str) -> int | None:
    """Call the small extraction model and persist the rule, if extractable.

    Returns the new rule id, or None if the model says "skip" or the call
    fails. All failures are caught and logged so the caller (a background
    thread) can ignore exceptions."""
    settings = get_settings()
    if action == "accept":
        system = _ACCEPT_SYSTEM
        user_prompt = _ACCEPT_USER_TEMPLATE.format(
            reviewer=finding["reviewer_agent"],
            severity=finding["severity"],
            category=finding["category"],
            finding_text=finding["finding_text"],
            suggested_fix=finding["suggested_fix"] or "(none)",
        )
    else:
        system = _DISMISS_SYSTEM
        user_prompt = _DISMISS_USER_TEMPLATE.format(
            reviewer=finding["reviewer_agent"],
            severity=finding["severity"],
            category=finding["category"],
            finding_text=finding["finding_text"],
            suggested_fix=finding["suggested_fix"] or "(none)",
            dismissal_reason=finding["dismissed_reason"] or "(none)",
        )

    try:
        tool_input, usage = call_tool_for_model(
            model=settings.model_light_extraction,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            tool=_EXTRACT_TOOL,
            max_tokens=400,
            agent_name="lesson_extractor",
            proposal_id=None,
        )
    except Exception:
        log.exception(
            "lesson extraction failed (kind=%s, finding_id=%s)",
            kind,
            finding["id"],
        )
        return None

    extractable = bool(tool_input.get("extractable", False))
    rule_text = str(tool_input.get("rule_text") or "").strip()
    if not extractable or not rule_text:
        log.info(
            "lesson extraction: not extractable (kind=%s, finding_id=%s)",
            kind,
            finding["id"],
        )
        return None
    rule_id = _persist_rule(kind=kind, rule_text=rule_text, finding=finding, action=action)
    log.info(
        "lesson extracted (kind=%s, finding_id=%s) -> rule_id=%s %s",
        kind,
        finding["id"],
        rule_id,
        fmt_llm_usage(usage),
    )
    return rule_id


# --- Public extraction triggers -------------------------------------------


def schedule_extract_on_accept(finding_id: int) -> None:
    """Spawn a background thread to extract a writer-avoidance rule from
    an accepted finding. Does not block the caller. Idempotent: if no
    finding exists, the thread no-ops."""
    finding = _snapshot_finding(finding_id)
    if finding is None:
        return
    t = threading.Thread(
        target=_run_extraction,
        kwargs={"kind": "writer_avoid", "finding": finding, "action": "accept"},
        name=f"lesson-accept-{finding_id}",
        daemon=True,
    )
    t.start()


def schedule_extract_on_dismiss(finding_id: int) -> None:
    """Spawn a background thread to extract a reviewer-calibration rule
    from a dismissed finding. Does nothing if the finding has no
    dismissal reason — without a reason, there's no signal to extract."""
    finding = _snapshot_finding(finding_id)
    if finding is None:
        return
    if not (finding.get("dismissed_reason") or "").strip():
        log.info(
            "lesson extraction skipped — dismissed without reason (finding_id=%s)",
            finding_id,
        )
        return
    t = threading.Thread(
        target=_run_extraction,
        kwargs={
            "kind": "reviewer_calibrate",
            "finding": finding,
            "action": "dismiss",
        },
        name=f"lesson-dismiss-{finding_id}",
        daemon=True,
    )
    t.start()


# --- CRUD for the UI ------------------------------------------------------


@dataclass
class LessonRow:
    id: int
    kind: str
    rule_text: str
    status: str
    hits: int
    source_finding_id: int | None
    source_action: str | None
    source_category: str | None
    source_severity: str | None
    source_reviewer: str | None
    created_at: datetime | None
    updated_at: datetime | None


def _row_to_lesson(r: LearnedRule) -> LessonRow:
    return LessonRow(
        id=r.id,
        kind=r.kind,
        rule_text=r.rule_text,
        status=r.status,
        hits=r.hits,
        source_finding_id=r.source_finding_id,
        source_action=r.source_action,
        source_category=r.source_category,
        source_severity=r.source_severity,
        source_reviewer=r.source_reviewer,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def list_rules(
    *,
    kind: str | None = None,
    status: str | None = None,
) -> list[LessonRow]:
    """List learned rules. Filters are optional; pass None to get all."""
    with session_scope() as db:
        q = select(LearnedRule)
        if kind:
            q = q.where(LearnedRule.kind == kind)
        if status:
            q = q.where(LearnedRule.status == status)
        q = q.order_by(LearnedRule.status.asc(), LearnedRule.id.desc())
        rows = db.execute(q).scalars().all()
        return [_row_to_lesson(r) for r in rows]


def count_rules_by_status() -> dict[str, int]:
    """Cheap aggregate: how many rules per status (used for tab badges)."""
    with session_scope() as db:
        rows = db.execute(
            select(LearnedRule.status, func.count(LearnedRule.id)).group_by(LearnedRule.status)
        ).all()
        return {status: int(count) for status, count in rows}


def approve_rule(rule_id: int) -> bool:
    with session_scope() as db:
        r = db.get(LearnedRule, rule_id)
        if r is None:
            return False
        r.status = "approved"
    return True


def archive_rule(rule_id: int) -> bool:
    with session_scope() as db:
        r = db.get(LearnedRule, rule_id)
        if r is None:
            return False
        r.status = "archived"
    return True


def delete_rule(rule_id: int) -> bool:
    with session_scope() as db:
        r = db.get(LearnedRule, rule_id)
        if r is None:
            return False
        db.delete(r)
    return True


def update_rule_text(rule_id: int, text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    with session_scope() as db:
        r = db.get(LearnedRule, rule_id)
        if r is None:
            return False
        r.rule_text = text
    return True


# --- Prompt-injection helpers (used by writer + reviewers) ----------------


def _bump_hits(rule_ids: list[int]) -> None:
    """Increment the hits counter on each rule. Best-effort — caller
    doesn't care if it fails."""
    if not rule_ids:
        return
    try:
        with session_scope() as db:
            db.execute(
                LearnedRule.__table__.update()
                .where(LearnedRule.id.in_(rule_ids))
                .values(hits=LearnedRule.hits + 1)
            )
    except Exception:
        log.exception("failed to bump hits for rules %s", rule_ids)


def format_writer_guidance(*, max_rules: int = 30) -> str:
    """Return an injectable text block of approved writer-avoidance rules.

    Empty string when there are no approved rules. Caller appends the
    block to the writer's system prompt.
    """
    with session_scope() as db:
        rows = (
            db.execute(
                select(LearnedRule)
                .where(
                    LearnedRule.kind == "writer_avoid",
                    LearnedRule.status == "approved",
                )
                .order_by(LearnedRule.id.desc())
                .limit(max_rules)
            )
            .scalars()
            .all()
        )
        rule_ids = [r.id for r in rows]
        rule_lines = [f"- {r.rule_text.strip()}" for r in rows]
    if not rule_lines:
        return ""
    _bump_hits(rule_ids)
    return (
        "\n\nLEARNED GUIDANCE — past user feedback. The user has "
        "previously confirmed these patterns are real problems. Avoid "
        "them in this draft:\n" + "\n".join(rule_lines)
    )


def format_reviewer_guidance(
    *,
    reviewer: str,
    categories: list[str] | None = None,
    max_rules: int = 30,
    include_calibration_stats: bool = True,
) -> str:
    """Return an injectable text block for one of the reviewers.

    `reviewer` is "A" or "B". The block contains:
    - Approved reviewer-calibration rules scoped to this reviewer (don't
      flag pattern X).
    - Optional category accept/dismiss stats line (cheap, no LLM call) to
      let the reviewer self-calibrate confidence.
    - When proposal_outcomes data is present, also includes per-category
      WON/LOST correlation guidance.
    """
    parts: list[str] = []

    with session_scope() as db:
        # Calibration rules: surfaced when scoped to this reviewer OR
        # untargeted (older rows that don't carry source_reviewer).
        q = select(LearnedRule).where(
            LearnedRule.kind == "reviewer_calibrate",
            LearnedRule.status == "approved",
        )
        # Filter by reviewer if we have any rule with that reviewer set;
        # otherwise show all (defensive — handles the boundary case where
        # source_reviewer is None on legacy rows).
        rows = (
            db.execute(
                q.where((LearnedRule.source_reviewer == reviewer) | (LearnedRule.source_reviewer.is_(None)))
                .order_by(LearnedRule.id.desc())
                .limit(max_rules)
            )
            .scalars()
            .all()
        )
        if categories:
            rows = [r for r in rows if not r.source_category or r.source_category in categories]
        rule_ids = [r.id for r in rows]
        rule_lines = [f"- {r.rule_text.strip()}" for r in rows]

    if rule_lines:
        _bump_hits(rule_ids)
        parts.append(
            "LEARNED GUIDANCE — past user feedback. The user previously "
            "DISMISSED findings of these shapes as false positives. Do "
            "NOT flag the same pattern unless this case is materially "
            "different:\n" + "\n".join(rule_lines)
        )

    if include_calibration_stats:
        stats_block = _build_category_calibration_block(categories=categories)
        if stats_block:
            parts.append(stats_block)

    # Outcome-correlation block (pipeline 3 — win/loss ledger feedback).
    # Returns "" when zero ProposalOutcome rows exist or no category
    # clears the threshold; in both cases the dismiss-rate-only output
    # shape is byte-identical to the prior behavior.
    outcome_block = _format_outcome_calibration(
        reviewer=reviewer,
        categories=categories,
    )
    if outcome_block:
        parts.append(outcome_block)

    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)


def _build_category_calibration_block(*, categories: list[str] | None = None) -> str:
    """Aggregate accept-vs-dismiss rate per finding category and format
    it as a calibration line for the reviewer prompt.

    Only includes categories with >= 5 user actions — below that, the
    rate is too noisy to be useful guidance.
    """
    rate = get_category_action_rates()
    lines: list[str] = []
    for cat, stats in sorted(rate.items()):
        if categories and cat not in categories:
            continue
        total_actions = stats["accepted"] + stats["dismissed"]
        if total_actions < 5:
            continue
        accept_pct = round(100 * stats["accepted"] / total_actions)
        dismiss_pct = 100 - accept_pct
        label = FindingCategory(cat).value if cat else cat
        lines.append(
            f"- {label}: dismissed {dismiss_pct}% / accepted {accept_pct}% "
            f"(over {total_actions} user actions)"
        )
    if not lines:
        return ""
    return (
        "USER FEEDBACK CALIBRATION — historical accept/dismiss rates per "
        "finding category. Categories with high dismissal rates have been "
        "false positives most of the time — be especially confident before "
        "raising one:\n" + "\n".join(lines)
    )


def _format_outcome_calibration(
    *,
    reviewer: str,
    categories: list[str] | None = None,
    min_observations: int = 5,
) -> str:
    """Render the outcome-correlation guidance block.

    For each category in `categories` (or all if None), count how many
    ReviewerFinding rows of that category were attached to sections of
    proposals whose ProposalOutcome.outcome is WON vs LOST. Emit a
    guidance line per category with at least `min_observations`
    occurrences (default 5). Returns "" when there are no ProposalOutcome
    rows OR no category clears the threshold.

    The `reviewer` argument is currently informational (reserved for
    future per-reviewer scoping); for v1 all qualifying categories are
    emitted regardless of which reviewer asked, because each reviewer's
    `categories` arg already scopes the output.
    """
    with session_scope() as db:
        rows = db.execute(
            select(
                ReviewerFinding.category,
                ProposalOutcome.outcome,
                func.count(ReviewerFinding.id),
            )
            .join(
                ProposalSection,
                ProposalSection.id == ReviewerFinding.proposal_section_id,
            )
            .join(Proposal, Proposal.id == ProposalSection.proposal_id)
            .join(
                ProposalOutcome,
                ProposalOutcome.proposal_id == Proposal.id,
            )
            .where(ProposalOutcome.outcome.in_(["won", "lost"]))
            .group_by(ReviewerFinding.category, ProposalOutcome.outcome)
        ).all()

    # Pivot into {category: {"won": n, "lost": n}}
    pivot: dict[str, dict[str, int]] = {}
    for cat, outcome, n in rows:
        cat_key = cat.value if hasattr(cat, "value") else str(cat) if cat else None
        out_key = outcome.value if hasattr(outcome, "value") else str(outcome)
        if not cat_key:
            continue
        pivot.setdefault(cat_key, {"won": 0, "lost": 0})[out_key] = int(n or 0)

    if not pivot:
        return ""

    lines: list[str] = []
    for cat_key in sorted(pivot.keys()):
        if categories and cat_key not in categories:
            continue
        won = pivot[cat_key].get("won", 0)
        lost = pivot[cat_key].get("lost", 0)
        total = won + lost
        if total < min_observations:
            continue
        if lost / total >= 0.7:
            lines.append(
                f"- category={cat_key}: present in {lost} LOST proposals "
                f"vs {won} WON. Flag aggressively — strong negative correlation."
            )
        elif won / total >= 0.7:
            lines.append(
                f"- category={cat_key}: present in {won} WON proposals "
                f"vs {lost} LOST. Common in winners — be conservative before flagging."
            )
        else:
            lines.append(
                f"- category={cat_key}: present in {lost} LOST and {won} WON — "
                f"signal-to-noise inconclusive; rely on dismiss-rate stats "
                f"alone for this category."
            )

    if not lines:
        return ""
    return (
        "OUTCOME CORRELATION — historical patterns from past WON vs LOST "
        "proposals. Bias your flagging accordingly:\n" + "\n".join(lines)
    )


def get_category_action_rates() -> dict[str, dict[str, int]]:
    """For each finding category, return how many findings the user has
    accepted vs. dismissed. Used both by the reviewer-prompt calibration
    block and by the Learned Guidance UI tab.

    The auto Review-Revise loop calls `accept_finding` and then
    `mark_findings_resolved` on every finding it processes — those rows
    have BOTH accepted_at and resolved_in_pass_number set, and they are
    not human signal. We exclude them here. The user flow never sets
    resolved_in_pass_number, so the filter is exact for accepts.
    Dismissals are always user-driven (the loop never dismisses).
    """
    accepted_expr = func.sum(
        case(
            (
                ReviewerFinding.accepted_at.isnot(None) & ReviewerFinding.resolved_in_pass_number.is_(None),
                1,
            ),
            else_=0,
        )
    )
    dismissed_expr = func.sum(case((ReviewerFinding.dismissed_at.isnot(None), 1), else_=0))
    with session_scope() as db:
        rows = db.execute(
            select(
                ReviewerFinding.category,
                accepted_expr.label("accepted"),
                dismissed_expr.label("dismissed"),
            ).group_by(ReviewerFinding.category)
        ).all()
        out: dict[str, dict[str, int]] = {}
        for cat, accepted, dismissed in rows:
            cat_key = cat.value if hasattr(cat, "value") else str(cat) if cat else ""
            if not cat_key:
                continue
            out[cat_key] = {
                "accepted": int(accepted or 0),
                "dismissed": int(dismissed or 0),
            }
    return out


__all__ = [
    "LessonRow",
    "approve_rule",
    "archive_rule",
    "count_rules_by_status",
    "delete_rule",
    "format_reviewer_guidance",
    "format_writer_guidance",
    "get_category_action_rates",
    "list_rules",
    "schedule_extract_on_accept",
    "schedule_extract_on_dismiss",
    "update_rule_text",
]
