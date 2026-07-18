"""Loader for the canonical company profile.

The profile is read from data/company_profile.json on first access and cached.
Every agent gets it via get_company_profile(). Versioned by the _meta.version
field in the JSON.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.config import COMPANY_PROFILE_PATH


class CompanyProfileNotFoundError(FileNotFoundError):
    pass


@lru_cache(maxsize=1)
def get_company_profile() -> dict[str, Any]:
    if not COMPANY_PROFILE_PATH.exists():
        raise CompanyProfileNotFoundError(
            f"Company profile missing at {COMPANY_PROFILE_PATH}. "
            "This file is foundational — agents cannot run without it."
        )
    with COMPANY_PROFILE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def reload_company_profile() -> dict[str, Any]:
    """Bust the cache and re-read from disk. Use after editing the file."""
    get_company_profile.cache_clear()
    return get_company_profile()


def get_profile_version() -> str:
    return get_company_profile().get("_meta", {}).get("version", "unknown")


def get_labor_rate_card() -> dict[str, Any]:
    return get_company_profile().get("labor_rate_card", {})


def get_key_personnel() -> list[dict[str, Any]]:
    return get_company_profile().get("key_personnel", [])


def get_past_performance() -> list[dict[str, Any]]:
    return get_company_profile().get("past_performance", [])


def get_certifications() -> list[str]:
    return get_company_profile().get("certifications", [])


def get_clearances_inventory() -> list[dict[str, Any]]:
    """Return personnel with active clearances. Used by Shortfall Strategist."""
    cleared = []
    for person in get_key_personnel():
        clearances = person.get("clearances") or []
        if clearances:
            cleared.append(
                {
                    "name": person.get("name"),
                    "role": person.get("role"),
                    "clearances": clearances,
                }
            )
    return cleared


def get_capability_areas() -> list[dict[str, Any]]:
    return get_company_profile().get("capability_areas", [])


def get_deep_specializations() -> list[str]:
    return get_company_profile().get("deep_specializations", [])
