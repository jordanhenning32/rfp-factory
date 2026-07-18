"""Cross-RFP decisions ledger.

Captures resolutions Quadratic has made about specific kinds of gaps so the
Shortfall Strategist inherits institutional memory across proposals. The
Strategist reads the entire ledger as part of its cached prefix on every
run; semantic match between past decisions and current gaps is left to the
LLM rather than implemented as a keyword pre-filter.

Storage: data/decisions.json (committed; this is canonical company knowledge).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date
from functools import lru_cache
from typing import Any

from app.config import DATA_DIR

DECISIONS_PATH = DATA_DIR / "decisions.json"

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_decisions() -> dict[str, Any]:
    if not DECISIONS_PATH.exists():
        return {"_meta": {"version": "1.0.0"}, "decisions": []}
    with DECISIONS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def reload_decisions() -> dict[str, Any]:
    get_decisions.cache_clear()
    return get_decisions()


def get_decisions_list() -> list[dict[str, Any]]:
    return get_decisions().get("decisions", [])


def _next_id(existing: list[dict]) -> str:
    nums: list[int] = []
    for d in existing:
        try:
            nums.append(int(str(d.get("id", "DEC-0")).replace("DEC-", "")))
        except (ValueError, AttributeError):
            pass
    nxt = (max(nums) + 1) if nums else 1
    return f"DEC-{nxt:03d}"


def _atomic_write(data: dict) -> None:
    DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{DECISIONS_PATH.name}.",
        suffix=".tmp",
        dir=str(DECISIONS_PATH.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, DECISIONS_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def add_decision(
    *,
    topic: str,
    decision: str,
    applies_to_gaps_like: str,
    source_proposal_id: int | None = None,
    source_gap_id: str | None = None,
) -> dict[str, Any]:
    """Append a decision. Atomic write. Skips (with reason) if a decision
    with the same topic already exists (case-insensitive)."""
    data = reload_decisions()
    decisions = data.setdefault("decisions", [])

    topic_clean = (topic or "").strip()
    if not topic_clean:
        return {"added": False, "reason": "topic is required"}
    decision_clean = (decision or "").strip()
    if not decision_clean:
        return {"added": False, "reason": "decision text is required"}

    if any((d.get("topic") or "").strip().lower() == topic_clean.lower() for d in decisions):
        return {"added": False, "reason": "a decision with this topic already exists"}

    new = {
        "id": _next_id(decisions),
        "topic": topic_clean,
        "decision": decision_clean,
        "applies_to_gaps_like": (applies_to_gaps_like or "").strip(),
        "established_on": date.today().isoformat(),
        "source_proposal_id": source_proposal_id,
        "source_gap_id": source_gap_id,
    }
    decisions.append(new)
    _atomic_write(data)
    reload_decisions()
    return {"added": True, "decision": new}


def delete_decision(decision_id: str) -> bool:
    data = reload_decisions()
    decisions = data.get("decisions", [])
    keep = [d for d in decisions if d.get("id") != decision_id]
    if len(keep) == len(decisions):
        return False
    data["decisions"] = keep
    _atomic_write(data)
    reload_decisions()
    return True


def format_decisions_for_prompt() -> str:
    """Render the ledger as a context block for the Strategist's cached prefix.
    Returns a friendly sentence when the ledger is empty so the agent doesn't
    invent its own structure."""
    decisions = get_decisions_list()
    if not decisions:
        return "(no past decisions recorded yet — this is the first run, or no gaps have been marked 'remember this decision' before)"
    lines: list[str] = []
    for d in decisions:
        src = ""
        if d.get("source_proposal_id"):
            src = f" (from proposal #{d['source_proposal_id']}"
            if d.get("source_gap_id"):
                src += f", {d['source_gap_id']}"
            src += ")"
        lines.append(
            f"[{d.get('id', '?')}] {d.get('topic', '')}\n"
            f"  Applies to gaps like: {d.get('applies_to_gaps_like', '')}\n"
            f"  Established: {d.get('established_on', '?')}{src}\n"
            f"  DECISION: {d.get('decision', '')}\n"
        )
    return "\n".join(lines)
