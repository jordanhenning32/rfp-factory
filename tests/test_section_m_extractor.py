"""Tests for app/agents/section_m_extractor.py."""

from __future__ import annotations


def test_extract_evaluation_criteria_parses_tool_output(monkeypatch, inmemory_db):
    """Stub the LLM boundary and verify EvaluationCriteria is built correctly."""
    from app.agents.section_m_extractor import EvaluationCriteria, extract_evaluation_criteria

    # Hand-built tool_input the stub returns
    stub_tool_input = {
        "evaluation_method": "trade_off",
        "factors": [
            {
                "factor_id": "F1",
                "factor_name": "Technical Approach",
                "weight_pct": 40,
                "weight_descriptive": None,
                "scoring_scale": "Exceptional/Acceptable/Marginal/Unacceptable",
                "evidence_required": "Detailed narrative",
                "subfactors": [],
            },
            {
                "factor_id": "F2",
                "factor_name": "Past Performance",
                "weight_pct": 30,
                "weight_descriptive": None,
                "scoring_scale": None,
                "evidence_required": None,
                "subfactors": [],
            },
            {
                "factor_id": "F3",
                "factor_name": "Price",
                "weight_pct": 30,
                "weight_descriptive": None,
                "scoring_scale": None,
                "evidence_required": None,
                "subfactors": [],
            },
        ],
        "section_l_to_m_map": {"REQ-001": ["F1"]},
        "trade_off_language": "The Government will award to the offeror whose proposal represents the best value.",
        "lowest_price_clause": None,
        "extraction_notes": "Factor weights sum to 100%; cross-reference confirmed for REQ-001.",
    }

    # Stub get_anthropic() — the real call_tool returns (tool_input_dict, usage_dict)
    class _FakeClient:
        def call_tool(self, **kwargs):
            return stub_tool_input, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    monkeypatch.setattr(
        "app.agents.section_m_extractor.get_anthropic",
        lambda: _FakeClient(),
    )

    text = (
        "Factor 1: Technical Approach (40 points). "
        "Factor 2: Past Performance (30 points). "
        "Factor 3: Price (30 points). Trade-off basis."
    )
    compliance_items = [{"requirement_id": "REQ-001", "requirement_text": "Submit technical approach."}]

    result = extract_evaluation_criteria(
        proposal_id=None,
        document_text=text,
        filename="rfp.pdf",
        compliance_items=compliance_items,
    )

    assert isinstance(result, EvaluationCriteria)
    assert result.evaluation_method == "trade_off"
    assert len(result.factors) == 3

    weights = {f["factor_name"]: f["weight_pct"] for f in result.factors}
    assert weights["Technical Approach"] == 40
    assert weights["Past Performance"] == 30
    assert weights["Price"] == 30

    assert "REQ-001" in result.section_l_to_m_map
    assert result.extraction_notes is not None
    assert len(result.extraction_notes) > 0


def test_extract_evaluation_criteria_handles_empty_text(inmemory_db, monkeypatch):
    """Empty document text returns unknown + empty criteria without LLM call."""
    from app.agents.section_m_extractor import EvaluationCriteria, extract_evaluation_criteria

    call_count = {"n": 0}

    class _FakeClient:
        def call_tool(self, **kwargs):
            call_count["n"] += 1
            return {}, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    monkeypatch.setattr(
        "app.agents.section_m_extractor.get_anthropic",
        lambda: _FakeClient(),
    )

    result = extract_evaluation_criteria(
        proposal_id=None,
        document_text="",
        filename="rfp.pdf",
    )

    assert isinstance(result, EvaluationCriteria)
    assert result.evaluation_method == "unknown"
    assert result.factors == []
    assert result.section_l_to_m_map == {}
    assert result.extraction_notes is not None
    # The early-return path must NOT invoke the LLM
    assert call_count["n"] == 0

    # Whitespace-only text also triggers early return
    result2 = extract_evaluation_criteria(
        proposal_id=None,
        document_text="   \n\t  ",
        filename="rfp.pdf",
    )
    assert result2.evaluation_method == "unknown"
    assert call_count["n"] == 0
