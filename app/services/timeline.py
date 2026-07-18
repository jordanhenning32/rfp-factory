"""Timeline persistence + helpers for the per-proposal implementation
schedule shown on the Timeline tab.

State lives in a single JSON document on `proposals.timeline_json`
(migration 0031). Document shape:

    {
        "anchor_date": "YYYY-MM-DD" | null,
        "phases": [
            {
                "id": str (uuid4),
                "phase_name": str,
                "start_offset": int (days from project start, >= 0),
                "duration": int (days, >= 1),
                "deliverable": str,
                "owner": str,
                "color": str (hex like "#1F3A5F"),
                "order": int (manual reorder within same start_offset)
            },
            ...
        ]
    }

The service is the only writer of this column; the Timeline tab
reads through `get_timeline()` and mutates through the helpers
defined here. Direct JSON edits from the UI are not supported (and
would defeat the schema validation).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.db.session import session_scope
from app.models import Proposal

log = logging.getLogger(__name__)

# Default brand color for new phases — Quadratic Digital navy.
# Matches `_theme.NAVY` used by the page-frame header. Kept as a
# literal here (rather than imported from _theme) to avoid coupling
# the service layer to the UI module.
DEFAULT_PHASE_COLOR = "#1F3A5F"


# ---- Read paths ----------------------------------------------------------


def get_timeline(proposal_id: int) -> dict:
    """Return the full timeline document for the proposal. Always
    returns a well-formed dict, even when the column is NULL or the
    JSON is malformed — callers can iterate `result["phases"]`
    without guarding for None / KeyError.

    Phases are returned sorted by (start_offset, order, id) so the UI
    renders the Gantt rows in a stable visual order.
    """
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        raw = p.timeline_json if p is not None else None

    if not raw:
        return {"anchor_date": None, "phases": []}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(
            "timeline: proposal %d has malformed timeline_json — "
            "returning empty document. The next mutation will overwrite "
            "the bad data.",
            proposal_id,
        )
        return {"anchor_date": None, "phases": []}

    if not isinstance(data, dict):
        return {"anchor_date": None, "phases": []}

    anchor = data.get("anchor_date")
    if not isinstance(anchor, str):
        anchor = None

    phases_raw = data.get("phases") or []
    if not isinstance(phases_raw, list):
        phases_raw = []

    phases = [_normalize_phase(p) for p in phases_raw if isinstance(p, dict)]
    phases.sort(
        key=lambda p: (p["start_offset"], p["order"], p["id"]),
    )
    return {"anchor_date": anchor, "phases": phases}


def _normalize_phase(p: dict) -> dict:
    """Heal a phase dict so the UI doesn't have to guard every field.
    Defaults are conservative — any missing/invalid value becomes a
    sensible no-op, never None / NaN. Idempotent."""
    return {
        "id": str(p.get("id") or uuid.uuid4()),
        "phase_name": str(p.get("phase_name") or "Untitled phase"),
        "start_offset": max(0, _coerce_int(p.get("start_offset"), 0)),
        "duration": max(1, _coerce_int(p.get("duration"), 1)),
        "deliverable": str(p.get("deliverable") or ""),
        "owner": str(p.get("owner") or ""),
        "color": str(p.get("color") or DEFAULT_PHASE_COLOR),
        "order": _coerce_int(p.get("order"), 0),
    }


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def total_duration(phases: list[dict]) -> int:
    """Total project span in days = max(start_offset + duration) across
    all phases. Returns 0 for an empty timeline so the UI can branch
    on falsy. Used for Gantt-bar percentage calculations."""
    if not phases:
        return 0
    return max(p["start_offset"] + p["duration"] for p in phases)


# ---- Write paths ---------------------------------------------------------


def _save(proposal_id: int, anchor_date: str | None, phases: list[dict]) -> None:
    """Internal helper — persist the full document. All public mutators
    funnel through here so we always write a normalized blob."""
    payload = {
        "anchor_date": anchor_date,
        "phases": [_normalize_phase(p) for p in phases],
    }
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            raise ValueError(f"proposal {proposal_id} not found")
        p.timeline_json = json.dumps(payload, ensure_ascii=False)


def add_phase(
    proposal_id: int,
    *,
    phase_name: str,
    start_offset: int,
    duration: int,
    deliverable: str = "",
    owner: str = "",
    color: str = DEFAULT_PHASE_COLOR,
) -> dict:
    """Append a new phase. Returns the normalized phase dict (with its
    freshly-generated `id`). `order` defaults to len(phases) so new
    phases sort to the end within their start_offset group."""
    doc = get_timeline(proposal_id)
    new_phase = _normalize_phase(
        {
            "id": str(uuid.uuid4()),
            "phase_name": phase_name,
            "start_offset": start_offset,
            "duration": duration,
            "deliverable": deliverable,
            "owner": owner,
            "color": color,
            "order": len(doc["phases"]),
        }
    )
    doc["phases"].append(new_phase)
    _save(proposal_id, doc["anchor_date"], doc["phases"])
    return new_phase


def update_phase(
    proposal_id: int,
    phase_id: str,
    **fields: Any,
) -> dict | None:
    """Update specified fields on a phase. Returns the updated phase
    dict, or None if no phase with that id was found.

    Allowed fields: phase_name, start_offset, duration, deliverable,
    owner, color, order. Unknown fields are silently ignored — the
    Timeline tab is the only consumer and it doesn't pass anything else.
    """
    allowed = {
        "phase_name",
        "start_offset",
        "duration",
        "deliverable",
        "owner",
        "color",
        "order",
    }
    doc = get_timeline(proposal_id)
    for ph in doc["phases"]:
        if ph["id"] == phase_id:
            for k, v in fields.items():
                if k in allowed:
                    ph[k] = v
            updated = _normalize_phase(ph)
            ph.update(updated)
            _save(proposal_id, doc["anchor_date"], doc["phases"])
            return updated
    return None


def delete_phase(proposal_id: int, phase_id: str) -> bool:
    """Remove the phase with this id. Returns True if a phase was
    deleted, False if no match was found."""
    doc = get_timeline(proposal_id)
    before = len(doc["phases"])
    doc["phases"] = [p for p in doc["phases"] if p["id"] != phase_id]
    if len(doc["phases"]) == before:
        return False
    _save(proposal_id, doc["anchor_date"], doc["phases"])
    return True


def set_anchor_date(proposal_id: int, anchor_date: str | None) -> None:
    """Set or clear the absolute-date anchor. `anchor_date` is a
    YYYY-MM-DD string; pass None to clear. The UI uses this to compute
    "Jun 1 – Jun 30" labels in addition to the offset-based ones."""
    doc = get_timeline(proposal_id)
    _save(proposal_id, anchor_date, doc["phases"])


def clear_timeline(proposal_id: int) -> None:
    """Reset the timeline to empty. Used by 'Start over' on the tab."""
    _save(proposal_id, None, [])


# ---- Import from cost-analyst phases -------------------------------------


def import_from_cost_phases(proposal_id: int) -> int:
    """Seed the timeline from the proposal's PricingPackage
    phase_breakdown_json. Picks the MEDIUM scenario by default
    (it's the analyst's recommended target); falls back to whichever
    scenario has the longest phase_breakdown if MEDIUM is empty.

    Cost analyst phases are expressed in months
    (`start_month`, `duration_months`); we convert to days by ×30.
    Phase names + descriptions carry over; owner is left blank.
    Existing timeline phases are REPLACED, not merged — the user
    confirmed via the dialog before this runs.

    Returns the number of phases imported (0 if no cost phases exist).
    """
    from app.models import PricingPackage

    with session_scope() as db:
        # Pull the active package's phase_breakdown. MEDIUM is the
        # recommended target so it's the default seed source.
        candidates = db.query(PricingPackage).filter(PricingPackage.proposal_id == proposal_id).all()
        # Prefer MEDIUM, then HIGH, then LOW — match the analyst's
        # primary-recommendation order.
        scenario_priority = {"MEDIUM": 0, "HIGH": 1, "LOW": 2, "CUSTOM": 3}
        candidates.sort(
            key=lambda pkg: scenario_priority.get(
                pkg.scenario.value if hasattr(pkg.scenario, "value") else str(pkg.scenario),
                99,
            ),
        )
        breakdown = None
        for pkg in candidates:
            if pkg.phase_breakdown_json:
                breakdown = list(pkg.phase_breakdown_json)
                break

    if not breakdown:
        return 0

    # Convert each cost-phase to a timeline phase. Cost phases use
    # months, timeline uses days — we standardize on 30 days/month for
    # display consistency. Users can refine durations after import.
    new_phases: list[dict] = []
    for idx, cp in enumerate(breakdown):
        if not isinstance(cp, dict):
            continue
        start_month = _coerce_int(cp.get("start_month"), idx)
        duration_months = _coerce_int(cp.get("duration_months"), 1)
        new_phases.append(
            _normalize_phase(
                {
                    "id": str(uuid.uuid4()),
                    "phase_name": str(cp.get("name") or f"Phase {idx + 1}"),
                    "start_offset": start_month * 30,
                    "duration": max(1, duration_months * 30),
                    "deliverable": str(cp.get("description") or ""),
                    "owner": "",
                    "color": DEFAULT_PHASE_COLOR,
                    "order": idx,
                }
            )
        )

    doc = get_timeline(proposal_id)
    _save(proposal_id, doc["anchor_date"], new_phases)
    log.info(
        "timeline: proposal %d imported %d phases from cost analyst (replaced %d existing)",
        proposal_id,
        len(new_phases),
        len(doc["phases"]),
    )
    return len(new_phases)


def has_importable_cost_phases(proposal_id: int) -> bool:
    """Cheap check for whether the 'Import from cost build' button
    should be enabled. Returns True iff at least one PricingPackage
    has a non-empty phase_breakdown_json."""
    from app.models import PricingPackage

    with session_scope() as db:
        return (
            db.query(PricingPackage)
            .filter(
                PricingPackage.proposal_id == proposal_id,
                PricingPackage.phase_breakdown_json.is_not(None),
            )
            .count()
        ) > 0


__all__ = [
    "DEFAULT_PHASE_COLOR",
    "add_phase",
    "clear_timeline",
    "delete_phase",
    "get_timeline",
    "has_importable_cost_phases",
    "import_from_cost_phases",
    "set_anchor_date",
    "total_duration",
    "update_phase",
]
