"""Tests verifying Reviewer A wiring with evaluation-criteria block."""

from __future__ import annotations

_SAMPLE_CRITERIA = {
    "evaluation_method": "trade_off",
    "factors": [
        {
            "factor_id": "F1",
            "factor_name": "Technical Approach",
            "weight_pct": 40,
            "weight_descriptive": None,
            "scoring_scale": "Exceptional/Acceptable/Marginal/Unacceptable",
            "evidence_required": "Detailed narrative",
            "subfactors": [
                {"name": "Software Architecture", "weight_pct": 15, "notes": None},
                {"name": "Testing", "weight_pct": 10, "notes": None},
            ],
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
            "weight_pct": None,
            "weight_descriptive": "least important",
            "scoring_scale": None,
            "evidence_required": None,
            "subfactors": [],
        },
    ],
    "section_l_to_m_map": {"REQ-001": ["F1", "F1.1"], "REQ-002": ["F2"]},
    "trade_off_language": "The Government will make award to the offeror whose proposal is most advantageous.",
    "lowest_price_clause": None,
    "extraction_notes": "Factor 3 has no numeric weight disclosed.",
}


def test_format_evaluation_criteria_block_includes_factors_and_weights(inmemory_db):
    """format_evaluation_criteria_block renders factor names and weights."""
    from app.services.evaluation_criteria import format_evaluation_criteria_block

    block = format_evaluation_criteria_block(_SAMPLE_CRITERIA)

    assert "=== EVALUATION CRITERIA — WHAT THE BUYER ACTUALLY SCORES ===" in block
    assert "trade_off" in block  # evaluation_method value
    assert "Technical Approach" in block
    assert "40%" in block
    assert "Past Performance" in block
    assert "30%" in block
    assert "Price" in block
    # Descriptive weight for Price
    assert "least important" in block


def test_format_evaluation_criteria_block_returns_empty_string_when_no_criteria(inmemory_db):
    """None and empty dict both return empty string."""
    from app.services.evaluation_criteria import format_evaluation_criteria_block

    assert format_evaluation_criteria_block(None) == ""
    assert format_evaluation_criteria_block({}) == ""


def test_build_cached_prefix_embeds_evaluation_criteria_block(inmemory_db):
    """build_cached_prefix includes the evaluation_criteria_block after gaps."""
    from app.agents.reviewer_a import build_cached_prefix

    ec_block = "=== EVALUATION CRITERIA — TEST ===\nFoo"

    prefix = build_cached_prefix(
        profile_json="{}",
        kb_context="(none)",
        outline_text="(none)",
        compliance_text="(none)",
        gaps_text="(no gaps)",
        evaluation_criteria_block=ec_block,
    )

    assert "=== EVALUATION CRITERIA — TEST ===" in prefix
    # The evaluation_criteria_block must appear AFTER the gaps block
    gaps_pos = prefix.index("GAP ANALYSES")
    ec_pos = prefix.index("=== EVALUATION CRITERIA — TEST ===")
    assert ec_pos > gaps_pos, "evaluation_criteria_block must appear after gaps_text"


def test_build_cached_prefix_works_without_evaluation_criteria_block(inmemory_db):
    """Default empty string means old callers don't break and block is absent."""
    from app.agents.reviewer_a import build_cached_prefix

    # Call WITHOUT evaluation_criteria_block — should use default ""
    prefix = build_cached_prefix(
        profile_json="{}",
        kb_context="(none)",
        outline_text="(none)",
        compliance_text="(none)",
        gaps_text="(no gaps)",
    )

    # Must build cleanly — no KeyError / ValueError
    assert isinstance(prefix, str)
    assert len(prefix) > 0
    # Must NOT contain the EVALUATION CRITERIA header
    assert "=== EVALUATION CRITERIA" not in prefix
