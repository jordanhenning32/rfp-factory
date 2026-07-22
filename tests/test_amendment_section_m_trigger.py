from __future__ import annotations


def test_new_evaluation_criterion_triggers_section_m_refresh() -> None:
    from app.agents.compliance_matrix import (
        ComplianceExtractionResult,
        ExtractedComplianceItem,
    )
    from app.jobs.amendment import _delta_touches_evaluation_criteria

    delta = ComplianceExtractionResult(
        new_items=[
            ExtractedComplianceItem(
                requirement_id="REQ-NEW",
                requirement_text="Technical approach will be worth 40 points.",
                requirement_type="evaluation_criterion",
                category="technical",
            )
        ]
    )

    assert _delta_touches_evaluation_criteria(delta)


def test_evaluation_word_in_category_does_not_mask_wrong_type() -> None:
    from app.agents.compliance_matrix import (
        ComplianceExtractionResult,
        ExtractedComplianceItem,
    )
    from app.jobs.amendment import _delta_touches_evaluation_criteria

    delta = ComplianceExtractionResult(
        new_items=[
            ExtractedComplianceItem(
                requirement_id="REQ-NEW",
                requirement_text="Provide a technical narrative.",
                requirement_type="shall",
                # Not a valid production category, but verifies the trigger
                # reads the correct axis rather than accepting drift.
                category="evaluation_criterion",
            )
        ]
    )

    assert not _delta_touches_evaluation_criteria(delta)


def test_incomplete_amendment_extraction_fails_before_apply() -> None:
    import pytest

    from app.agents.compliance_matrix import ComplianceExtractionResult
    from app.jobs.amendment import _require_complete_amendment_extraction

    delta = ComplianceExtractionResult(
        coverage_state="partial",
        source_chunks_total=2,
        source_chunks_completed=1,
        failed_chunk_labels=["chunk 2/2"],
        incomplete_reasons=["chunk_failed"],
    )

    with pytest.raises(RuntimeError, match="no changes were applied"):
        _require_complete_amendment_extraction(delta)
