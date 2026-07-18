"""Payment-Systems Cost Review helpers — read + mutate findings on
proposals.payment_cost_review_findings_json.

Mirrors the labor flow's services/cost_reviewer.py + services/findings.
py user-action mutators, but operates on a JSON blob instead of
relational rows. The labor flow persists findings to
cost_review_findings (FK PricingPackage); payment_systems has no
PricingPackage rows so findings live on the proposal directly.

User actions on findings (Accept / Reject / Edit / Refine with AI)
all flow through `update_payment_finding_action` — read the JSON,
mutate the matching finding's user_action / user_note, write back.
The orchestrator's persist path overwrites the blob in full on a
fresh review run; user mutations are single-finding patches.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.db.session import session_scope
from app.models import Proposal

log = logging.getLogger(__name__)


_VALID_ACTIONS = ("pending", "accepted", "rejected")


def get_payment_cost_review_data(proposal_id: int) -> dict[str, Any]:
    """Read the persisted Cost Reviewer output for a payment_systems
    proposal. Returns an empty dict when the reviewer hasn't run yet
    OR the JSON is malformed."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return {}
        raw = p.payment_cost_review_findings_json
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning(
            "payment_cost_review_findings_json invalid JSON on proposal %d",
            proposal_id,
        )
        return {}


def get_payment_cost_review_findings(
    proposal_id: int,
) -> list[dict[str, Any]]:
    """Convenience accessor — returns just the findings list with
    user_action / user_note defaults backfilled. Older payloads
    persisted before user-triage support default user_action='pending'
    so the UI doesn't break on legacy data."""
    data = get_payment_cost_review_data(proposal_id)
    findings = data.get("findings") or []
    for f in findings:
        f.setdefault("user_action", "pending")
        f.setdefault("user_note", None)
    return findings


def update_payment_finding_action(
    proposal_id: int,
    finding_id: str,
    *,
    action: str,
    user_note: str | None = None,
) -> dict[str, Any] | None:
    """Mutate one finding's user_action (+ optional user_note) inside
    the persisted JSON blob. Returns the updated finding dict, or
    None if the finding wasn't found.

    Validates action against _VALID_ACTIONS — unknown values raise
    ValueError. user_note semantics:
      - When action='accepted' AND user_note is set, user_note is the
        EDITED suggested_fix (overrides the agent's original).
      - When action='rejected' AND user_note is set, user_note is the
        rejection reason.
      - When user_note is None, the field is left unchanged.
    """
    target_action = (action or "").strip().lower()
    if target_action not in _VALID_ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {_VALID_ACTIONS}")

    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return None
        raw = p.payment_cost_review_findings_json
        if not raw or not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(
                "payment_cost_review_findings_json invalid JSON on proposal %d",
                proposal_id,
            )
            return None

        findings = data.get("findings") or []
        target = next(
            (f for f in findings if f.get("finding_id") == finding_id),
            None,
        )
        if target is None:
            return None

        target["user_action"] = target_action
        target.setdefault("user_note", None)
        if user_note is not None:
            target["user_note"] = user_note.strip() or None

        # SQLAlchemy JSON-column dirty-tracking gotcha (per architecture
        # invariant): we serialize the full dict back rather than
        # mutating in-place — the column is plain Text, so we always
        # re-serialize anyway.
        p.payment_cost_review_findings_json = json.dumps(
            data,
            indent=2,
            default=str,
        )
        return dict(target)


def update_payment_finding_user_note(
    proposal_id: int,
    finding_id: str,
    *,
    user_note: str,
) -> dict[str, Any] | None:
    """Edit-only mutation — sets user_note WITHOUT changing
    user_action. Used when the user wants to refine the suggested_fix
    text (via Edit dialog or Refine with AI) before deciding to
    accept / reject. To save the edit AND mark accepted in one shot,
    call `update_payment_finding_action(action='accepted', user_note=...)`."""
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return None
        raw = p.payment_cost_review_findings_json
        if not raw or not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        findings = data.get("findings") or []
        target = next(
            (f for f in findings if f.get("finding_id") == finding_id),
            None,
        )
        if target is None:
            return None

        target["user_note"] = (user_note or "").strip() or None
        target.setdefault("user_action", "pending")
        p.payment_cost_review_findings_json = json.dumps(
            data,
            indent=2,
            default=str,
        )
        return dict(target)


def get_payment_finding(
    proposal_id: int,
    finding_id: str,
) -> dict[str, Any] | None:
    """Read a single finding by ID. Used by the Edit / Refine dialogs
    to fetch the latest state when the dialog opens. Returns None if
    not found."""
    for f in get_payment_cost_review_findings(proposal_id):
        if f.get("finding_id") == finding_id:
            return f
    return None


def bulk_accept_pending_payment_findings(
    proposal_id: int,
    *,
    severity_floor: str | None = None,
) -> dict[str, int]:
    """Accept every pending finding in one shot. Mirrors the labor
    flow's services/findings.py `bulk_accept_pending_findings` API
    so the user-facing semantics are identical.

    `severity_floor` (optional) — when set, only findings at or above
    that severity get accepted. Levels: CRITICAL > MAJOR > MINOR.
    None = accept ALL pending regardless of severity.

    Returns counts: {accepted, skipped_already_actioned, skipped_below_floor}.
    """
    severity_rank = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}
    floor_rank = severity_rank.get((severity_floor or "").upper(), 0) if severity_floor else 0

    accepted = 0
    skipped_already = 0
    skipped_below = 0

    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return {
                "accepted": 0,
                "skipped_already_actioned": 0,
                "skipped_below_floor": 0,
            }
        raw = p.payment_cost_review_findings_json
        if not raw or not raw.strip():
            return {
                "accepted": 0,
                "skipped_already_actioned": 0,
                "skipped_below_floor": 0,
            }
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(
                "payment_cost_review_findings_json invalid JSON on proposal %d",
                proposal_id,
            )
            return {
                "accepted": 0,
                "skipped_already_actioned": 0,
                "skipped_below_floor": 0,
            }

        for f in data.get("findings") or []:
            f.setdefault("user_action", "pending")
            f.setdefault("user_note", None)
            current = (f.get("user_action") or "pending").lower()
            if current != "pending":
                skipped_already += 1
                continue
            sev = (f.get("severity") or "MINOR").upper()
            if severity_rank.get(sev, 0) < floor_rank:
                skipped_below += 1
                continue
            f["user_action"] = "accepted"
            accepted += 1

        if accepted:
            p.payment_cost_review_findings_json = json.dumps(
                data,
                indent=2,
                default=str,
            )

    return {
        "accepted": accepted,
        "skipped_already_actioned": skipped_already,
        "skipped_below_floor": skipped_below,
    }


def get_accepted_payment_findings_for_writer(
    proposal_id: int,
) -> list[dict[str, Any]]:
    """Return the accepted findings shaped for the Cost Volume
    Writer's cached prefix. Each entry carries everything the writer
    needs to apply the fix verbatim:

      - finding_id  — for citation in the regenerate audit trail
      - section_id / section_title — which section to apply to
      - severity / category — drives the writer's prioritization
        (CRITICAL fixes are non-negotiable; MINOR can be paraphrased
        if it improves narrative flow)
      - finding_text — what was wrong (lets the writer understand
        the intent, not just blindly substitute)
      - canonical_fix — the user's edited fix (user_note when set)
        OR the agent's original suggested_fix (when the user accepted
        the recommendation as-is). This is the directive text the
        writer applies.
      - cited_quote — the verbatim snippet the reviewer flagged in
        the prior draft. Lets the writer locate the exact text to
        change rather than rewriting the whole section.

    Returns [] when no findings exist or none are accepted.
    """
    out: list[dict[str, Any]] = []
    for f in get_payment_cost_review_findings(proposal_id):
        if (f.get("user_action") or "").lower() != "accepted":
            continue
        edited_fix = (f.get("user_note") or "").strip()
        canonical_fix = edited_fix or (f.get("suggested_fix") or "").strip()
        out.append(
            {
                "finding_id": f.get("finding_id") or "",
                "section_id": f.get("section_id") or "",
                "section_title": f.get("section_title") or "",
                "severity": (f.get("severity") or "MINOR").upper(),
                "category": f.get("category") or "OTHER",
                "finding_text": f.get("finding_text") or "",
                "canonical_fix": canonical_fix,
                "cited_quote": f.get("cited_quote") or "",
                "edited": bool(edited_fix),
            }
        )
    return out


def count_accepted_payment_findings(proposal_id: int) -> int:
    """Tally just the count — used by the UI button label without
    needing the full payload."""
    return sum(
        1
        for f in get_payment_cost_review_findings(proposal_id)
        if (f.get("user_action") or "").lower() == "accepted"
    )


__all__ = [
    "get_payment_cost_review_data",
    "get_payment_cost_review_findings",
    "update_payment_finding_action",
    "update_payment_finding_user_note",
    "get_payment_finding",
    "bulk_accept_pending_payment_findings",
    "get_accepted_payment_findings_for_writer",
    "count_accepted_payment_findings",
]
