"""Stable identifiers for section-level proposal-review coverage.

Provider ``AgentRun`` rows prove that an individual model call happened, but
they do not identify the section/revision reviewed and they are written before
review findings are persisted.  The reviewer orchestrator therefore writes a
separate, zero-cost synthetic ``AgentRun`` for the composite operation:

    deterministic pre-flights + Reviewer A + Reviewer B + finding persistence

Submission readiness consumes only these coverage rows.  A completed marker
is valid for exactly one section revision, so edits fail closed until that
revision is reviewed again.
"""
from __future__ import annotations

REVIEW_COVERAGE_AGENT = "_review_coverage"
REVIEW_COVERAGE_VERSION = "sr1"


def review_coverage_prompt_version(section_pk: int, revision: int) -> str:
    """Return the compact AgentRun key for a section revision.

    ``AgentRun.prompt_version`` is limited to 40 characters.  Hex encoding
    keeps the key below that limit even for signed 64-bit ids/revisions.
    """
    section_pk = int(section_pk)
    revision = int(revision)
    if section_pk < 1:
        raise ValueError("section_pk must be positive")
    if revision < 0:
        raise ValueError("revision must be non-negative")
    return f"{REVIEW_COVERAGE_VERSION}:{section_pk:x}:{revision:x}"


__all__ = [
    "REVIEW_COVERAGE_AGENT",
    "REVIEW_COVERAGE_VERSION",
    "review_coverage_prompt_version",
]
