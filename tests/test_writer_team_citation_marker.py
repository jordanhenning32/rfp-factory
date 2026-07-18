"""Regression guard for Writer Team citation marker schema."""

from __future__ import annotations

import app.agents.writer_team as writer_team


def test_writer_team_citation_schema_requires_marker_not_citation_marker(
    inmemory_db,
) -> None:
    required = writer_team._TOOL["input_schema"]["properties"]["citations"]["items"]["required"]

    assert "marker" in required
    assert "citation_marker" not in required
