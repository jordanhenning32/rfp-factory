"""Persistence helpers for CostReviewFinding rows.

The Cost Reviewer's findings persist as cost_review_findings rows
keyed by pricing_package_id (per-scenario FK). Findings that affect
multiple scenarios get one row per affected scenario — keeps the
schema simple and lets the UI surface findings on each scenario's
detail panel.

Re-running the reviewer replaces findings that disappeared while carrying
human triage forward for logically identical findings. Each scenario's
CASCADE delete via PricingPackage relationship handles cleanup if the cost
build is regenerated.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.agents.cost_reviewer import CostReviewResult
from app.db.session import session_scope
from app.models import CostReviewFinding, PricingPackage, Proposal
from app.services.proposal_access import ensure_proposal_mutable

log = logging.getLogger(__name__)


_VALID_USER_ACTIONS = {"pending", "accepted", "rejected"}


def _normalize_identity_part(value: Any) -> str:
    """Normalize harmless model-output formatting drift for identity checks."""
    return " ".join(str(value or "").split()).casefold()


def _logical_finding_key(
    severity: Any,
    category: Any,
    finding_text: Any,
) -> tuple[str, str, str]:
    """Match the UI's logical finding identity, with text normalization."""
    return (
        _normalize_identity_part(severity),
        _normalize_identity_part(category),
        _normalize_identity_part(finding_text),
    )


def _triage_state(row: CostReviewFinding) -> dict[str, Any]:
    action = str(row.user_action or "pending").strip().lower()
    if action not in _VALID_USER_ACTIONS:
        action = "pending"
    return {
        "user_action": action,
        "user_note": row.user_note,
        "auto_actioned": bool(row.auto_actioned and action == "accepted"),
    }


def _triage_priority(state: dict[str, Any]) -> tuple[int, int, int]:
    """Prefer explicit human triage if legacy scenario rows disagree."""
    actioned = state["user_action"] in {"accepted", "rejected"}
    human_actioned = actioned and not state["auto_actioned"]
    return (
        int(human_actioned),
        int(actioned),
        int(bool(state["user_note"])),
    )


def upsert_cost_review_findings(
    *,
    proposal_id: int,
    result: CostReviewResult,
) -> int:
    """Replace stale cost-review findings while preserving matching triage.

    Returns the number of CostReviewFinding rows written (one per finding ×
    affected scenario). A logical finding is identified the same way the UI
    groups it: severity + category + finding text, normalized for harmless
    whitespace/case drift. Scenario membership and generated row IDs are not
    identity, so accepted/rejected state and user notes survive those changes.

    Findings affecting multiple scenarios are written as multiple
    rows so each scenario's detail panel can surface its own
    findings without joining across pricing packages."""
    with session_scope() as db:
        ensure_proposal_mutable(
            db, proposal_id, operation="replace cost review findings",
        )
        # Pull the proposal's pricing packages so we can look up
        # PricingPackage.id by scenario name.
        pkgs = (
            db.execute(
                select(PricingPackage).where(
                    PricingPackage.proposal_id == proposal_id,
                )
            )
            .scalars()
            .all()
        )
        if not pkgs:
            log.warning(
                "upsert_cost_review_findings: no pricing packages for proposal %d — cannot persist findings",
                proposal_id,
            )
            return 0
        by_scenario_id: dict[str, int] = {p.scenario: p.id for p in pkgs}

        # Snapshot triage before replacing the rows. Scenario rows belonging
        # to one logical finding normally have identical state because the UI
        # updates them together. If legacy rows disagree, prefer an explicit
        # human action over an auto-action or pending state.
        existing = db.execute(
            select(CostReviewFinding).where(
                CostReviewFinding.pricing_package_id.in_(
                    [p.id for p in pkgs]
                )
            )
            .order_by(CostReviewFinding.id)
        ).scalars().all()
        triage_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for ex in existing:
            key = _logical_finding_key(
                ex.severity,
                ex.category,
                ex.finding_text,
            )
            candidate = _triage_state(ex)
            current = triage_by_key.get(key)
            if (
                current is None
                or _triage_priority(candidate) > _triage_priority(current)
            ):
                triage_by_key[key] = candidate

        # Clear existing findings across all scenarios. Findings absent from
        # the new result intentionally disappear; matching state is applied to
        # the replacement rows below.
        for ex in existing:
            db.delete(ex)
        if existing:
            db.flush()

        n_written = 0
        for f in result.findings:
            alternatives_persisted = [
                {
                    "label": a.label,
                    "total_price_usd": a.total_price_usd,
                    "rationale": a.rationale,
                    "margin_delta_usd": a.margin_delta_usd,
                }
                for a in f.alternative_scenarios
            ]
            # Compose finding_text with a leading subject line so it
            # reads cleanly in the UI without needing a separate
            # subject column on the model. Keeps the model schema
            # untouched while preserving the structured subject.
            persisted_text = (
                f"[{f.subject}] {f.finding_text}".strip()
                if f.subject and f.subject.strip()
                else f.finding_text
            )
            triage = triage_by_key.get(
                _logical_finding_key(
                    f.severity,
                    f.category,
                    persisted_text,
                ),
                {
                    "user_action": "pending",
                    "user_note": None,
                    "auto_actioned": False,
                },
            )

            for scenario in f.scenarios_affected:
                pkg_id = by_scenario_id.get(scenario)
                if pkg_id is None:
                    log.warning(
                        "upsert_cost_review_findings: scenario %s has "
                        "no pricing_package_id; skipping finding %r",
                        scenario,
                        f.subject,
                    )
                    continue
                db.add(CostReviewFinding(
                    pricing_package_id=pkg_id,
                    finding_text=persisted_text,
                    severity=f.severity,
                    category=f.category,
                    alternative_scenarios_json=alternatives_persisted,
                    recommended_change=(
                        f.recommended_change.strip()
                        if f.recommended_change else None
                    ),
                    user_action=triage["user_action"],
                    user_note=triage["user_note"],
                    auto_actioned=triage["auto_actioned"],
                ))
                n_written += 1

        db.flush()

    log.info(
        "cost_review: persisted %d finding-rows for proposal %d (%d distinct findings)",
        n_written,
        proposal_id,
        len(result.findings),
    )
    return n_written


def get_cost_review_findings_snapshot(
    proposal_id: int,
) -> list[dict[str, Any]]:
    """Read-only snapshot of all cost-review findings for the
    proposal across all scenarios. Returns plain dicts so callers
    can use the data after the session closes."""
    with session_scope() as db:
        # Find all pricing packages for the proposal, then their
        # findings. Single query via JOIN.
        rows = db.execute(
            select(CostReviewFinding, PricingPackage.scenario)
            .join(
                PricingPackage,
                CostReviewFinding.pricing_package_id == PricingPackage.id,
            )
            .where(PricingPackage.proposal_id == proposal_id)
            .order_by(
                # CRITICAL > MAJOR > MINOR ordering using the natural
                # severity strings; alphabetic CRITICAL < MAJOR < MINOR
                # also happens to match by accident, but explicit is
                # safer.
                CostReviewFinding.severity,
                CostReviewFinding.category,
            )
        ).all()
        return [
            {
                "id": f.id,
                "pricing_package_id": f.pricing_package_id,
                "scenario": scenario,
                "finding_text": f.finding_text,
                "severity": f.severity,
                "category": f.category,
                "alternative_scenarios": list(f.alternative_scenarios_json or []),
                "recommended_change": f.recommended_change,
                "user_action": f.user_action or "pending",
                "user_note": f.user_note,
                "auto_actioned": bool(f.auto_actioned),
                "created_at": f.created_at,
                "updated_at": f.updated_at,
            }
            for f, scenario in rows
        ]


def update_cost_review_finding_action(
    *,
    finding_ids: list[int],
    user_action: str,
    user_note: str | None = None,
) -> int:
    """Update user_action / user_note across a set of CostReviewFinding
    rows that all belong to the same logical finding (one row per
    affected scenario). Returns the number of rows updated.

    Used by Cost Review tab Accept / Reject / Edit buttons. Caller
    passes the list of finding IDs (typically all rows for one
    logical finding) so the action updates uniformly. Always
    flips auto_actioned to False — once the user clicks any of
    the action buttons they have personally reviewed the row,
    so the AUTO chip should disappear from the UI.
    """
    if not finding_ids:
        return 0
    if user_action not in ("pending", "accepted", "rejected"):
        raise ValueError(f"user_action must be pending/accepted/rejected; got {user_action!r}")
    with session_scope() as db:
        rows = db.execute(
            select(CostReviewFinding).where(
                CostReviewFinding.id.in_(finding_ids)
            )
        ).scalars().all()
        proposal_ids = set(
            db.execute(
                select(PricingPackage.proposal_id)
                .join(
                    CostReviewFinding,
                    CostReviewFinding.pricing_package_id == PricingPackage.id,
                )
                .where(CostReviewFinding.id.in_(finding_ids))
            ).scalars().all()
        )
        for proposal_id in proposal_ids:
            ensure_proposal_mutable(
                db, proposal_id, operation="triage cost review findings",
            )
        for r in rows:
            r.user_action = user_action
            # User has reviewed the row — clear the AUTO marker.
            r.auto_actioned = False
            # Only set user_note when caller provides a value.
            # Passing None preserves whatever was there before
            # (avoids accidentally clearing a saved note on Accept).
            if user_note is not None:
                r.user_note = user_note.strip() or None
        n = len(rows)
    log.info(
        "cost_review: updated %d row(s) to user_action=%s",
        n,
        user_action,
    )
    return n


def auto_accept_consensus_findings(proposal_id: int) -> int:
    """Auto-accept CRITICAL and MAJOR consensus findings post-upsert.

    Behavior:
      - Consensus findings (BOTH reviewers agreed; identified by the
        ABSENCE of the '[Single-reviewer flag from ...]' prefix the
        consolidator stamps onto LLM-tagged minorities) at CRITICAL
        or MAJOR severity → user_action='accepted', auto_actioned=True
      - Minority findings (single-reviewer, prefix present) → left at
        pending; the user judges these manually since they lack
        cross-reviewer corroboration
      - MINOR consensus findings → left at pending; low severity is
        better triaged manually
      - Anything already non-pending (user previously actioned) →
        skipped, even if cost reviewer was re-run

    Returns the count of findings flipped to auto-accepted. Called
    from run_cost_reviewer right after upsert_cost_review_findings.
    """
    n_accepted = 0
    minority_prefix = "[Single-reviewer flag from "
    high_severities = {"CRITICAL", "MAJOR"}
    with session_scope() as db:
        ensure_proposal_mutable(
            db, proposal_id, operation="triage cost review findings",
        )
        rows = db.execute(
            select(CostReviewFinding)
            .join(
                PricingPackage,
                CostReviewFinding.pricing_package_id == PricingPackage.id,
            )
            .where(PricingPackage.proposal_id == proposal_id)
            .where(CostReviewFinding.user_action == "pending")
            .scalars()
            .all()
        )
        for f in rows:
            sev = (f.severity.value if hasattr(f.severity, "value") else str(f.severity or "")).upper()
            if sev not in high_severities:
                continue
            text = (f.finding_text or "").lstrip()
            if text.startswith(minority_prefix):
                continue
            f.user_action = "accepted"
            f.auto_actioned = True
            n_accepted += 1
    if n_accepted:
        log.info(
            "auto_accept_consensus_findings: proposal=%d "
            "auto-accepted %d CRITICAL/MAJOR consensus finding(s); "
            "minorities + MINOR consensus left pending for user review",
            proposal_id,
            n_accepted,
        )
    return n_accepted


def save_cost_review_strategy(
    proposal_id: int,
    markdown: str,
    findings_count: int,
) -> None:
    """Cache the synthesized cost-review strategy on the Proposal so
    the user can re-open it without paying Sonnet again. Overwrites
    any prior cached strategy."""
    from datetime import datetime as _dt

    with session_scope() as db:
        p = ensure_proposal_mutable(
            db, proposal_id, operation="save cost review strategy",
        )
        if p is None:
            log.warning(
                "save_cost_review_strategy: proposal %d not found",
                proposal_id,
            )
            return
        p.cost_review_strategy_markdown = markdown
        p.cost_review_strategy_generated_at = _dt.utcnow()
        p.cost_review_strategy_findings_count = findings_count
    log.info(
        "save_cost_review_strategy: cached %d chars for proposal %d (based on %d active findings)",
        len(markdown or ""),
        proposal_id,
        findings_count,
    )


def get_cost_review_strategy(proposal_id: int) -> dict | None:
    """Read the cached strategy. Returns None when never generated.

    Returns a dict with:
      - markdown: str
      - generated_at: datetime
      - findings_count_at_gen: int (snapshot of active count when
        the strategy was synthesized; UI flags staleness if the
        current active count diverges materially)
    """
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None or not p.cost_review_strategy_markdown:
            return None
        return {
            "markdown": p.cost_review_strategy_markdown,
            "generated_at": p.cost_review_strategy_generated_at,
            "findings_count_at_gen": (p.cost_review_strategy_findings_count),
        }


__all__ = [
    "auto_accept_consensus_findings",
    "get_cost_review_findings_snapshot",
    "get_cost_review_strategy",
    "save_cost_review_strategy",
    "update_cost_review_finding_action",
    "upsert_cost_review_findings",
]
