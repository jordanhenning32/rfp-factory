"""Tests for app/agents/compliance_matrix.py — delta-mode extraction.

Verifies that:
  (a) delta_mode=True returns a ComplianceExtractionResult with populated
      new_items / modified_items / removed_items and an empty .items.
  (b) delta_mode=False (legacy path) returns the dataclass with populated
      .items and empty delta lists.

Stubs the Anthropic client the same way test_section_m_extractor.py does:
client.call_tool returns a (tool_input_dict, usage_dict) TUPLE.
"""

from __future__ import annotations


def test_delta_mode_returns_populated_delta_fields(monkeypatch, inmemory_db):
    """delta_mode=True → ComplianceExtractionResult with new / modified /
    removed items populated and .items empty."""
    from app.agents.compliance_matrix import (
        ComplianceExtractionResult,
        ExtractedComplianceItem,
        extract_compliance_items,
    )

    stub_tool_input = {
        "new_items": [
            {
                "requirement_id": "REQ-X",  # placeholder; apply layer reassigns
                "requirement_text": "The contractor shall submit weekly reports.",
                "requirement_type": "shall",
                "category": "technical",
                "source_section": "Amendment 1, Section 2",
                "source_page": 1,
                "weight": None,
            },
        ],
        "modified_items": [
            {
                "existing_id": "REQ-001",
                "new_text": "Page limit raised from 25 pages to 30 pages.",
                "change_summary": "Page limit raised from 25 to 30.",
            },
        ],
        "removed_items": [
            {
                "existing_id": "REQ-002",
                "reason": "No longer required per amendment.",
            },
        ],
    }

    class _FakeClient:
        def call_tool(self, **kwargs):
            return stub_tool_input, {
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "stop_reason": "tool_use",
            }

    monkeypatch.setattr(
        "app.agents.compliance_matrix.get_anthropic",
        lambda: _FakeClient(),
    )

    result = extract_compliance_items(
        document_text="Amendment 1 changes the page limit and adds a new requirement.",
        filename="Amendment_0001.pdf",
        proposal_id=1,
        existing_items=[
            {
                "requirement_id": "REQ-001",
                "requirement_text": "Submit a 25-page technical narrative.",
            },
            {
                "requirement_id": "REQ-002",
                "requirement_text": "Submit a separate cost narrative.",
            },
        ],
        delta_mode=True,
    )

    assert isinstance(result, ComplianceExtractionResult)
    # Delta-mode path leaves .items empty
    assert result.items == []
    # All three delta fields populated
    assert len(result.new_items) == 1
    assert len(result.modified_items) == 1
    assert len(result.removed_items) == 1

    # new_items contains ExtractedComplianceItem instances
    new = result.new_items[0]
    assert isinstance(new, ExtractedComplianceItem)
    assert new.requirement_type == "shall"
    assert new.category == "technical"
    assert "weekly reports" in new.requirement_text

    # modified_items entries are dicts with the documented keys
    mod = result.modified_items[0]
    assert mod["existing_id"] == "REQ-001"
    assert "30" in mod["new_text"]
    assert "change_summary" in mod
    assert "25" in mod["change_summary"]

    # removed_items entries are dicts with existing_id + reason
    rem = result.removed_items[0]
    assert rem["existing_id"] == "REQ-002"
    assert "reason" in rem
    assert rem["reason"]


def test_legacy_mode_returns_items_field(monkeypatch, inmemory_db):
    """delta_mode=False → ComplianceExtractionResult with .items populated
    and all three delta lists empty.

    Re-uses the same fake-client pattern but returns a legacy tool_input
    shape (top-level `items` array).
    """
    from app.agents.compliance_matrix import (
        ComplianceExtractionResult,
        ExtractedComplianceItem,
        extract_compliance_items,
    )

    stub_tool_input = {
        "items": [
            {
                "requirement_id": "REQ-001",
                "requirement_text": "The contractor shall provide weekly status reports.",
                "requirement_type": "shall",
                "category": "management",
                "source_section": "Section 3.2",
                "source_page": 5,
                "weight": None,
            },
            {
                "requirement_id": "REQ-002",
                "requirement_text": "Proposals must be submitted by 5 PM EST.",
                "requirement_type": "submission_format",
                "category": "administrative",
                "source_section": "Section 4.1",
                "source_page": 7,
                "weight": None,
            },
        ],
    }

    class _FakeClient:
        def call_tool(self, **kwargs):
            return stub_tool_input, {
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "stop_reason": "tool_use",
            }

    monkeypatch.setattr(
        "app.agents.compliance_matrix.get_anthropic",
        lambda: _FakeClient(),
    )

    # Use --- Page N --- marker so the small-doc path runs cleanly.
    doc_text = (
        "--- Page 1 ---\n"
        "The contractor shall provide weekly status reports.\n"
        "Proposals must be submitted by 5 PM EST."
    )

    result = extract_compliance_items(
        document_text=doc_text,
        filename="rfp.pdf",
        proposal_id=1,
        # No existing_items / delta_mode — both default to None/False
    )

    assert isinstance(result, ComplianceExtractionResult)
    # Legacy path populates .items
    assert len(result.items) == 2
    # All three delta lists are empty on the legacy path
    assert result.new_items == []
    assert result.modified_items == []
    assert result.removed_items == []

    # .items entries are ExtractedComplianceItem instances
    assert isinstance(result.items[0], ExtractedComplianceItem)
    assert result.items[0].requirement_type == "shall"
    assert result.items[1].requirement_type == "submission_format"
