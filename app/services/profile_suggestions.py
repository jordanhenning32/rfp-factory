"""Apply (or reject) ProfileSuggestion rows.

Approving a suggestion writes back to data/company_profile.json with a
_meta.version patch bump. Atomic write via temp file + rename so the JSON
is never half-written. The in-memory profile cache is invalidated after
each apply so subsequent agent calls see the new value.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import COMPANY_PROFILE_PATH
from app.core.company_profile import get_company_profile, reload_company_profile
from app.models import ProfileSuggestion

log = logging.getLogger(__name__)


def _bump_patch(version: str) -> str:
    """Bump the patch component of a semver-ish '1.0.0' string. Falls back to
    the current ISO timestamp if the version doesn't look semver-ish."""
    try:
        parts = version.strip().split(".")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            major, minor, patch = (int(x) for x in parts)
            return f"{major}.{minor}.{patch + 1}"
    except Exception:
        pass
    return datetime.now(UTC).strftime("%Y.%m.%d.%H%M%S")


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


# ---------- Apply per operation ----------------------------------------------


def _find_named_entry(items: list[dict], match_key: str, name_field: str) -> dict | None:
    target = match_key.strip().lower()
    for entry in items:
        if (entry.get(name_field) or "").strip().lower() == target:
            return entry
    return None


def _apply_to_profile(profile: dict, suggestion: ProfileSuggestion) -> None:
    """Mutate `profile` in place with the suggestion's effect. Raises on
    structural problems so the caller can leave the suggestion pending."""
    op = suggestion.operation
    section = suggestion.section
    proposed = suggestion.proposed_value_json

    # NAICS lives at profile["naics"][bucket]; section is "naics.opportunistic".
    if section.startswith("naics."):
        bucket = section.split(".", 1)[1]
        naics = profile.setdefault("naics", {})
        if op == "append":
            existing = naics.setdefault(bucket, [])
            if proposed not in existing:
                existing.append(proposed)
            return
        raise ValueError(f"unsupported op {op!r} for {section}")

    # All other sections live at profile[section] — list of strings or dicts,
    # or a list of objects with name/project keys.
    if op == "append":
        target = profile.setdefault(section, [])
        if isinstance(proposed, dict):
            target.append(proposed)
        else:
            if proposed not in target:
                target.append(proposed)
        return

    if op == "merge":
        target = profile.get(section)
        if not isinstance(target, list):
            raise ValueError(f"cannot merge into {section}: profile section is not a list")
        # Pick the name field by section.
        name_field = "name" if section == "key_personnel" else "project"
        entry = _find_named_entry(target, suggestion.match_key or "", name_field)
        if entry is None:
            raise ValueError(f"merge target not found in {section}: {suggestion.match_key!r}")
        if not isinstance(proposed, dict):
            raise ValueError(f"merge expects a dict of fields; got {type(proposed)}")
        for k, v in proposed.items():
            entry[k] = v
        return

    if op == "set":
        profile[section] = proposed
        return

    raise ValueError(f"unsupported operation {op!r}")


# ---------- Public API --------------------------------------------------------


def apply_suggestion(
    db: Session,
    suggestion_id: int,
    *,
    reviewed_by: str | None = "local",
) -> dict[str, Any]:
    """Approve and apply a single suggestion. Bumps profile version, writes
    company_profile.json atomically, marks the suggestion approved.

    Returns: {applied: bool, new_version: str|None, error: str|None}
    """
    suggestion = db.get(ProfileSuggestion, suggestion_id)
    if suggestion is None:
        return {"applied": False, "error": "not_found"}
    if suggestion.status != "pending":
        return {"applied": False, "error": f"status is {suggestion.status}"}

    profile = get_company_profile()
    # Deep-copy via JSON round-trip — we mutate freely then write back.
    working = json.loads(json.dumps(profile))

    try:
        _apply_to_profile(working, suggestion)
    except Exception as exc:
        log.exception("apply_suggestion: failed to mutate profile")
        return {"applied": False, "error": str(exc)}

    # Bump version + meta.
    meta = working.setdefault("_meta", {})
    new_version = _bump_patch(str(meta.get("version", "1.0.0")))
    meta["version"] = new_version
    meta["effective_from"] = datetime.now(UTC).date().isoformat()

    try:
        _atomic_write_json(COMPANY_PROFILE_PATH, working)
    except Exception as exc:
        log.exception("apply_suggestion: write failed")
        return {"applied": False, "error": f"write_failed: {exc}"}

    reload_company_profile()

    suggestion.status = "approved"
    suggestion.reviewed_at = datetime.now(UTC)
    suggestion.reviewed_by = reviewed_by
    db.flush()

    log.info(
        "applied profile suggestion #%d (%s on %s) — new version %s",
        suggestion.id,
        suggestion.operation,
        suggestion.section,
        new_version,
    )
    return {"applied": True, "new_version": new_version, "error": None}


def reject_suggestion(db: Session, suggestion_id: int, *, reviewed_by: str | None = "local") -> bool:
    suggestion = db.get(ProfileSuggestion, suggestion_id)
    if suggestion is None or suggestion.status != "pending":
        return False
    suggestion.status = "rejected"
    suggestion.reviewed_at = datetime.now(UTC)
    suggestion.reviewed_by = reviewed_by
    db.flush()
    return True
