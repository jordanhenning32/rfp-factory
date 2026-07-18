"""Framing — the user's two strategic-posture answers that shape every
gap response and section draft.

Two columns on `proposals`:
  teaming_framing: NULL | "open" | "self_perform_only"
  build_framing:   NULL | "custom_build_first" | "self_perform_first"

NULL on either column means "decide per gap" — the writer behaves as
today (no framing block injected). Set via the Framing panel at the
top of the Gaps "Per gap" sub-tab.

This module owns:
  - Persistence (set_framing / get_framing)
  - Pure ranking (pick_mitigation_for_framing) — given an options list
    and framing answers, return the best index or None if no candidate.
  - Bulk-apply (apply_framing_to_unaddressed_gaps) — set selected_index
    on all gaps the user hasn't yet addressed.
  - Writer cached-prefix block (format_framing_block_for_writer) —
    follows the same pattern as format_team_block_for_writer /
    format_cost_build_block_for_writer.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.session import session_scope
from app.models.compliance import GapAnalysis
from app.models.proposal import Proposal

log = logging.getLogger(__name__)


_VALID_TEAMING = {None, "open", "self_perform_only"}
_VALID_BUILD = {None, "custom_build_first", "self_perform_first"}


def set_framing(
    proposal_id: int,
    *,
    teaming_framing: str | None,
    build_framing: str | None,
) -> None:
    """Persist the user's framing answers on the proposal. Either
    field may be None (= "decide per gap"). Raises ValueError on an
    unknown enum value so the UI can't accidentally write garbage."""
    if teaming_framing not in _VALID_TEAMING:
        raise ValueError(f"invalid teaming_framing: {teaming_framing!r}")
    if build_framing not in _VALID_BUILD:
        raise ValueError(f"invalid build_framing: {build_framing!r}")
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return
        p.teaming_framing = teaming_framing
        p.build_framing = build_framing


def get_framing(proposal_id: int) -> tuple[str | None, str | None]:
    """Return (teaming_framing, build_framing) for the proposal. Both
    None if the proposal doesn't exist."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return (None, None)
        return (p.teaming_framing, p.build_framing)


def pick_mitigation_for_framing(
    options: list[dict],
    *,
    teaming_framing: str | None,
    build_framing: str | None,
    recommended_index: int | None,
) -> int | None:
    """Pure ranking — given a gap's mitigation_options and the user's
    framing, return the best option index. None if no candidate matches
    (e.g. self-perform-only framing on a gap with only teaming options).

    Algorithm:
      1. Filter by teaming framing — drop teaming approaches when
         self_perform_only is set. If that empties the candidate list,
         return None.
      2. Apply build framing preference within remaining candidates —
         primary (matches preference) > secondary (the other build
         approach) > everything else.
      3. Tiebreak with recommended_index.
    """
    if not options:
        return None

    candidates: list[tuple[int, dict]] = list(enumerate(options))

    if teaming_framing == "self_perform_only":
        candidates = [(i, o) for i, o in candidates if (o.get("approach") or "").lower() != "teaming"]
        if not candidates:
            return None

    if build_framing in ("custom_build_first", "self_perform_first"):
        primary = "custom-build" if build_framing == "custom_build_first" else "self-perform"
        secondary = "self-perform" if build_framing == "custom_build_first" else "custom-build"
        for target in (primary, secondary):
            matches = [(i, o) for i, o in candidates if (o.get("approach") or "").lower() == target]
            if matches:
                # Prefer recommended within this tier when available
                for i, _ in matches:
                    if i == recommended_index:
                        return i
                return matches[0][0]
        # Fall through — no build-tier match in remaining candidates

    # Default: recommended if still a candidate, else first remaining
    cand_indices = [i for i, _ in candidates]
    if recommended_index in cand_indices:
        return recommended_index
    return cand_indices[0]


def apply_framing_to_unaddressed_gaps(proposal_id: int) -> dict:
    """Set selected_mitigation_index on every unresolved gap that has
    no current selection, using the proposal's saved framing. Returns
    counts: {applied, no_match, skipped, reason}.

    "skipped" — gap was already resolved or had a selection; left alone.
    "no_match" — framing excluded every option (e.g. teaming-only gap +
                 self_perform_only framing); user must address manually.
    "reason" — non-empty when no work was done (e.g. no framing set).
    """
    counts = {"applied": 0, "no_match": 0, "skipped": 0, "reason": ""}

    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            counts["reason"] = "proposal not found"
            return counts
        teaming_framing = p.teaming_framing
        build_framing = p.build_framing

        if not teaming_framing and not build_framing:
            counts["reason"] = "no framing set"
            return counts

        gaps = db.execute(select(GapAnalysis).where(GapAnalysis.proposal_id == proposal_id)).scalars().all()

        for g in gaps:
            if g.resolved or g.selected_mitigation_index is not None:
                counts["skipped"] += 1
                continue
            picked = pick_mitigation_for_framing(
                g.mitigation_options_json or [],
                teaming_framing=teaming_framing,
                build_framing=build_framing,
                recommended_index=g.recommended_mitigation_index,
            )
            if picked is None:
                counts["no_match"] += 1
                continue
            g.selected_mitigation_index = picked
            # Don't auto-clear partner — pick_mitigation_for_framing
            # operates on a fresh selection, so partner is None already.
            counts["applied"] += 1

    log.info(
        "framing applied to proposal %d: %s",
        proposal_id,
        counts,
    )
    return counts


def format_framing_block_for_writer(proposal_id: int) -> str:
    """Render the user's framing decisions as a text block for the
    writer's cached prefix. Empty string when no framing is set — the
    writer's behavior is unchanged in that case (matches the
    format_team_block_for_writer / format_cost_build_block_for_writer
    contract: empty when the gate isn't passed).
    """
    teaming_framing, build_framing = get_framing(proposal_id)
    if not teaming_framing and not build_framing:
        return ""

    lines: list[str] = [
        "=== APPROVED FRAMING (user-set strategic posture for this proposal) ===",
    ]
    if teaming_framing == "open":
        lines.append(
            "Teaming: OPEN — Quadratic is open to teaming with partners on this "
            "proposal where it strengthens the response. Use teaming language "
            "where the user has selected a teaming mitigation for a specific gap."
        )
    elif teaming_framing == "self_perform_only":
        lines.append(
            "Teaming: SELF-PERFORM ONLY — Quadratic will self-perform this "
            "proposal end-to-end. Do NOT introduce subcontractors, teaming "
            "partners, or 'we will partner with' language in any section. "
            "If a gap was originally framed as a teaming mitigation, rewrite "
            "the response around the chosen non-teaming alternative."
        )

    if build_framing == "custom_build_first":
        lines.append(
            "Capability gaps: CUSTOM-BUILD FIRST — where Quadratic lacks an "
            "off-the-shelf product capability, lead with a custom-built "
            "solution narrative (AI-accelerated delivery, IP ownership, "
            "tailored fit to the customer's workflow). Use COTS framing only "
            "as a secondary fallback when the RFP explicitly requires a "
            "named commercial product."
        )
    elif build_framing == "self_perform_first":
        lines.append(
            "Capability gaps: SELF-PERFORM FIRST — where Quadratic lacks a "
            "specific capability, lead with how Quadratic will deliver it "
            "directly using existing staff, equivalent experience, and "
            "in-progress investments. Reserve custom-build narratives for "
            "gaps that genuinely require net-new platform development."
        )

    lines.append(
        "Every section MUST be consistent with this framing. The framing "
        "supersedes per-gap defaults when there is any conflict."
    )
    return "\n".join(lines)
