"""Tests for Writer Team held-certification allowlist rendering."""

from __future__ import annotations

from app.agents.writer_team import format_held_certifications_block


def testformat_held_certifications_block_lists_profile_certs(inmemory_db) -> None:
    block = format_held_certifications_block({"certifications": ["ISO 27001"]})

    assert "ISO 27001" in block
    assert "claim ONLY these" in block


def testformat_held_certifications_block_empty_profile_returns_empty(inmemory_db) -> None:
    assert format_held_certifications_block({}) == ""
