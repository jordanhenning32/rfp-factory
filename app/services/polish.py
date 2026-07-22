"""Final Polish service helpers — persist + read polish edits.

The Final Polish job calls `record_polish_edit` after each successful
applier so the Final Polish UI tab can render a human-readable
"what changed" list. Reads via `list_recent_polish_edits_grouped`
return rows clustered by polish run for grouped UI rendering.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db.session import SessionLocal, session_scope
from app.models import PolishEdit
from app.services.proposal_access import ensure_proposal_mutable

log = logging.getLogger(__name__)


def record_polish_edit(
    *,
    proposal_id: int,
    proposal_section_id: int,
    section_id_label: str,
    issue_type: str,
    severity: str,
    edit_summary: str,
    rationale: str | None,
    problematic_text: str | None,
    suggested_fix: str | None,
    applied_at: datetime,
    applied_in_run_at: datetime,
    cost_usd: float,
) -> int:
    """Persist one polish-edit audit row. Returns the new row id.

    Called from `run_final_polish` after each successful applier
    invocation. Does not validate the inputs — caller is the job
    orchestrator and already knows the edit landed (persist_section_
    draft succeeded).
    """
    with session_scope() as db:
        ensure_proposal_mutable(
            db, proposal_id, operation="record final polish edit",
        )
        row = PolishEdit(
            proposal_id=proposal_id,
            proposal_section_id=proposal_section_id,
            section_id_label=section_id_label,
            issue_type=issue_type,
            severity=severity.upper(),
            edit_summary=edit_summary,
            rationale=rationale,
            problematic_text=problematic_text,
            suggested_fix=suggested_fix,
            applied_at=applied_at,
            applied_in_run_at=applied_in_run_at,
            cost_usd=float(cost_usd or 0.0),
        )
        db.add(row)
        db.flush()
        new_id = row.id
    return new_id


def list_recent_polish_edits_grouped(
    proposal_id: int,
    *,
    run_limit: int = 10,
) -> list[dict[str, Any]]:
    """Read polish edits for a proposal, grouped by polish run.

    Returns a list of run-bundles, most-recent run first. Each bundle:
        {
          "run_at": datetime,            # applied_in_run_at
          "n_edits": int,
          "total_cost_usd": float,
          "by_severity": {"CRITICAL": N, "MAJOR": N, "MINOR": N},
          "edits": [ {edit_summary, section_id_label, severity,
                      issue_type, rationale, applied_at, cost_usd}, ... ]
        }

    `run_limit` caps the number of runs returned (newest first). The
    full edit list within each returned run is included; we don't
    paginate inside a run because polish runs are small (typically
    5-15 edits).
    """
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(PolishEdit)
                .where(PolishEdit.proposal_id == proposal_id)
                .order_by(
                    PolishEdit.applied_in_run_at.desc(),
                    PolishEdit.applied_at.asc(),
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return []
        snapshots = [
            {
                "id": r.id,
                "section_id_label": r.section_id_label,
                "issue_type": r.issue_type,
                "severity": (r.severity or "").upper(),
                "edit_summary": r.edit_summary,
                "rationale": r.rationale or "",
                "problematic_text": r.problematic_text or "",
                "suggested_fix": r.suggested_fix or "",
                "applied_at": r.applied_at,
                "applied_in_run_at": r.applied_in_run_at,
                "cost_usd": float(r.cost_usd or 0.0),
            }
            for r in rows
        ]

    # Group by applied_in_run_at while preserving newest-first order.
    bundles: list[dict[str, Any]] = []
    seen_runs: dict[datetime, dict[str, Any]] = {}
    for s in snapshots:
        run_at = s["applied_in_run_at"]
        bundle = seen_runs.get(run_at)
        if bundle is None:
            bundle = {
                "run_at": run_at,
                "n_edits": 0,
                "total_cost_usd": 0.0,
                "by_severity": {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0},
                "edits": [],
            }
            seen_runs[run_at] = bundle
            bundles.append(bundle)
        bundle["n_edits"] += 1
        bundle["total_cost_usd"] += s["cost_usd"]
        sev = s["severity"]
        if sev in bundle["by_severity"]:
            bundle["by_severity"][sev] += 1
        bundle["edits"].append(s)

    # Apply run_limit AFTER bundling so we don't truncate edits within
    # the most recent run. bundles is already sorted because the
    # underlying query is ORDER BY applied_in_run_at DESC.
    return bundles[:run_limit]


__all__ = [
    "record_polish_edit",
    "list_recent_polish_edits_grouped",
]
