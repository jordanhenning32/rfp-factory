"""Decision-ledger auto-capture.

When the user resolves a [NEEDS_HUMAN] placeholder with the same
value they've supplied N or more times before (across all
proposals), we offer to save the answer as a cross-RFP decision
so the writer auto-applies it on future proposals. Compounds — the
more proposals run through the system, the fewer placeholders
each subsequent draft has.

Detection is post-resolution: the user types a value, the
resolve_placeholder service writes the draft change, and THEN we
check if this is the Nth instance. If yes, surface a follow-up
dialog. The user can review the suggested topic / applies-to
phrasing and click Save Decision (or Skip).

Only 'edit' resolutions are candidates — signatures are already
auto-resolved by the deterministic pass, and rejections aren't
decisions, they're 'this doesn't apply here'.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.db.session import session_scope
from app.models import ProposalSection

log = logging.getLogger(__name__)


# Min resolutions of the same value before we surface the prompt.
# 2 means "this is the second time" — caught early. Could raise to
# 3+ if users find the prompt noisy.
DEFAULT_MIN_COUNT = 2


def _normalize(value: str) -> str:
    """Case-insensitive trimmed match key. Whitespace runs collapse
    to single space so 'Within 30 days of award' matches
    'within  30  days  of  award'."""
    import re

    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip()).lower()


def _existing_decision_covers(value: str) -> bool:
    """True iff data/decisions.json already has a decision whose
    text matches `value` (case-insensitive). Used to suppress
    repeat prompts after the user has already saved this answer."""
    from app.core.decisions import get_decisions_list

    target = _normalize(value)
    if not target:
        return False
    for d in get_decisions_list():
        if _normalize(d.get("decision") or "") == target:
            return True
    return False


def detect_decision_candidate(
    value: str,
    *,
    kind: str = "edit",
    min_count: int = DEFAULT_MIN_COUNT,
) -> dict[str, Any] | None:
    """Scan all resolved [NEEDS_HUMAN] placeholders across all
    proposals; return a candidate dict when `value` has been used
    `min_count` or more times AND no existing decision already
    covers it. Returns None otherwise.

    The candidate dict shape (consumed by the UI 'Save as Decision'
    dialog):

      value                 the canonical value (verbatim, trimmed)
      matches               list of {proposal_id, section_id,
                            marker_text} — every prior instance
                            including the one that just landed
      n_matches             len(matches)
      suggested_topic       short noun phrase derived from markers
      suggested_applies_to  pipe-separated marker patterns
    """
    if not value or not value.strip():
        return None
    if kind != "edit":
        # signatures + rejections aren't decision material
        return None
    if _existing_decision_covers(value):
        return None

    target = _normalize(value)
    matches: list[dict[str, Any]] = []
    with session_scope() as db:
        sections = db.execute(select(ProposalSection)).scalars().all()
        for sec in sections:
            for ph in sec.needs_human_placeholders_json or []:
                if not ph.get("resolved"):
                    continue
                if ph.get("resolution_kind") != kind:
                    continue
                v_norm = _normalize(ph.get("resolution_value") or "")
                if v_norm != target:
                    continue
                matches.append(
                    {
                        "proposal_id": sec.proposal_id,
                        "section_id": sec.section_id or "",
                        "marker_text": (ph.get("marker_text") or "").strip(),
                    }
                )

    if len(matches) < min_count:
        return None

    # Distinct marker_texts in first-seen order, used to suggest
    # the 'applies_to_gaps_like' field. Keep the value verbatim
    # in 'value' so the prompt shows exactly what the user typed.
    distinct_markers: list[str] = []
    for m in matches:
        mt = m["marker_text"]
        if mt and mt not in distinct_markers:
            distinct_markers.append(mt)

    suggested_topic = _derive_topic_from_markers(distinct_markers)
    suggested_applies_to = " | ".join(distinct_markers[:5])

    return {
        "value": value.strip(),
        "matches": matches,
        "n_matches": len(matches),
        "suggested_topic": suggested_topic,
        "suggested_applies_to": suggested_applies_to,
    }


def _derive_topic_from_markers(markers: list[str]) -> str:
    """Heuristic topic suggestion. Uses the shortest marker as the
    seed (long markers tend to be the more verbose 'confirm X by
    submission' variants; short markers like 'kickoff date' are
    closer to a clean noun phrase). Title-cased, capped at 60
    chars."""
    if not markers:
        return "Captured decision"
    # Sort by length — shortest first for a punchier topic.
    by_len = sorted(markers, key=len)
    seed = by_len[0]
    if len(seed) > 60:
        seed = seed[:57] + "..."
    # Title-case the first word; leave the rest alone (might
    # contain product names or codes that shouldn't be touched).
    if seed and seed[0].isalpha():
        seed = seed[0].upper() + seed[1:]
    return seed


__all__ = [
    "DEFAULT_MIN_COUNT",
    "detect_decision_candidate",
]
