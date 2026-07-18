"""Tests for authoritative gap mitigation rendering."""

from __future__ import annotations

from app.jobs.writer import _format_gaps_for_writer


def test_format_gaps_marks_chosen_mitigation_authoritative(inmemory_db) -> None:
    rendered = _format_gaps_for_writer(
        [
            {
                "gap_id": "GAP-011",
                "severity": "technical",
                "req_id": "REQ-024",
                "current_state": "EMV/contactless support is not native.",
                "selected_mitigation_index": 0,
                "recommended_index": None,
                "mitigation_options": [
                    {
                        "approach": "configurable module",
                        "proposal_language_draft": (
                            "EMV/contactless via certified hardware APIs as a configurable module"
                        ),
                        "honesty_check": "Do not call this native support.",
                    }
                ],
            }
        ]
    )

    assert "AUTHORITATIVE GAP RESOLUTION" in rendered
    assert "DO NOT EXCEED" in rendered
    assert "EMV/contactless via certified hardware APIs as a configurable module" in rendered
