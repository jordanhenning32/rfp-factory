"""Loader for the teaming partner library.

Read at agent context-build time so the Shortfall Strategist can suggest
specific named partners for teaming-based mitigations instead of generic "X".
Partners with confirmed=false are still surfaced but flagged [NEEDS_HUMAN]
in mitigation language.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.config import DATA_DIR

TEAMING_PARTNERS_PATH = DATA_DIR / "teaming_partners.json"


@lru_cache(maxsize=1)
def get_teaming_partners() -> dict[str, Any]:
    if not TEAMING_PARTNERS_PATH.exists():
        return {"_meta": {"version": "missing"}, "partners": []}
    with TEAMING_PARTNERS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def reload_teaming_partners() -> dict[str, Any]:
    get_teaming_partners.cache_clear()
    return get_teaming_partners()


def get_partners_list() -> list[dict[str, Any]]:
    return get_teaming_partners().get("partners", [])


def add_partner(
    *,
    name: str,
    capability_focus: str | None,
    fit_rationale: str | None,
    confirmed: bool = False,
    profile: dict | None = None,
) -> dict[str, Any]:
    """Append a new partner entry to teaming_partners.json. Atomic write
    (tempfile + rename). Skips if a partner with the same name (case-
    insensitive) already exists. Returns {added: bool, reason: str}.

    `profile` is the agent's full profile dict (overview, capabilities,
    contact, etc.) — when provided, its fields seed the library entry so
    the user doesn't have to re-enter what the agent already knew.
    """
    import os
    import tempfile

    name_clean = (name or "").strip()
    if not name_clean:
        return {"added": False, "reason": "name is required"}

    data = reload_teaming_partners()  # always read fresh from disk
    partners = data.setdefault("partners", [])
    if any((p.get("name") or "").strip().lower() == name_clean.lower() for p in partners):
        return {"added": False, "reason": "already in library"}

    profile = profile or {}
    contact = profile.get("contact") or {}

    entry = {
        "name": name_clean,
        "confirmed": bool(confirmed),
        "relationship_summary": (
            profile.get("overview")
            or ("Suggested by Shortfall Strategist; needs outreach" if not confirmed else "")
        ),
        "core_capabilities": list(profile.get("key_capabilities") or []),
        "certifications_set_asides": list(profile.get("certifications_set_asides") or []),
        "geographic_presence": ([contact["primary_location"]] if contact.get("primary_location") else []),
        "good_fit_for": ([capability_focus.strip()] if capability_focus and capability_focus.strip() else []),
        "contact": {
            "website": contact.get("website") or "",
            "primary_location": contact.get("primary_location") or "",
            "general_email": contact.get("general_email") or "",
            "linkedin": contact.get("linkedin") or "",
            "notes": "",
        },
        "_added_via_strategist": True,
        "_added_with_fit_rationale": fit_rationale or "",
    }
    partners.append(entry)

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{TEAMING_PARTNERS_PATH.name}.",
        suffix=".tmp",
        dir=str(TEAMING_PARTNERS_PATH.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, TEAMING_PARTNERS_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise

    reload_teaming_partners()
    return {"added": True, "reason": "ok"}
