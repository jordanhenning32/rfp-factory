"""Coverage reporting for compliance extraction failure/data-loss paths."""
from __future__ import annotations

import pytest


def _usage(stop_reason: str = "tool_use") -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "stop_reason": stop_reason,
    }


def _item(requirement_id: str = "REQ-001", text: str = "Vendor shall comply.") -> dict:
    return {
        "requirement_id": requirement_id,
        "requirement_text": text,
        "requirement_type": "shall",
        "category": "technical",
        "source_page": 1,
    }


def test_valid_zero_item_response_is_complete(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {"items": []}, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text="--- Page 1 ---\nAgency background only.",
        filename="background.pdf",
        proposal_id=1,
    )

    assert result.items == []
    assert result.extraction_complete is True
    assert result.coverage_state == "complete"
    assert result.source_chunks_total == 1
    assert result.source_chunks_completed == 1
    assert result.failed_chunk_count == 0
    assert result.coverage_as_public_dict()["complete"] is True


def test_zero_items_at_max_tokens_is_failed_not_clean(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {"items": []}, _usage("max_tokens")

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text="--- Page 1 ---\nVendor shall provide a plan.",
        filename="truncated.pdf",
        proposal_id=1,
    )

    assert result.coverage_state == "failed"
    assert result.extraction_complete is False
    assert result.response_truncated is True
    assert result.source_chunks_completed == 0
    assert result.failed_chunk_labels == ["document"]
    assert "response_truncated_without_items" in result.incomplete_reasons


def test_item_cap_and_malformed_item_make_extraction_partial(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "items": [
                    _item("REQ-001", "Vendor shall provide a plan."),
                    {"requirement_id": "REQ-BROKEN"},
                    _item("REQ-003", "Vendor must provide a schedule."),
                ],
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    # The malformed second row is inside the cap and is therefore observed;
    # the third row proves that the model output itself was also capped.
    result = compliance_matrix.extract_compliance_items(
        document_text=(
            "--- Page 1 ---\nVendor shall provide a plan.\n"
            "Vendor must provide a schedule."
        ),
        filename="capped.pdf",
        proposal_id=1,
        max_items=2,
    )

    assert len(result.items) == 1
    assert result.coverage_state == "partial"
    assert result.output_capped is True
    assert result.malformed_items_skipped == 1
    assert result.source_chunks_completed == 1
    assert set(result.incomplete_reasons) == {
        "output_capped",
        "malformed_items_skipped",
    }


def test_source_character_truncation_is_partial(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {"items": []}, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())
    monkeypatch.setattr(compliance_matrix, "_MAX_INPUT_CHARS", 40)

    result = compliance_matrix.extract_compliance_items(
        document_text="--- Page 1 ---\n" + ("source " * 20),
        filename="oversize.pdf",
        proposal_id=1,
    )

    assert result.coverage_state == "partial"
    assert result.source_truncated is True
    assert result.source_chunks_completed == 1
    assert result.failed_chunk_count == 0


def test_unrecoverable_malformed_chunk_is_failed(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {"items": {"not": "an array"}}, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text="--- Page 1 ---\nVendor shall provide a plan.",
        filename="malformed.pdf",
        proposal_id=1,
    )

    assert result.items == []
    assert result.coverage_state == "failed"
    assert result.source_chunks_total == 1
    assert result.source_chunks_completed == 0
    assert result.failed_chunk_labels == ["document"]
    assert "malformed_tool_payload_unrecovered" in result.incomplete_reasons


def test_successful_split_recovery_restores_complete_coverage(monkeypatch):
    from app.agents import compliance_matrix

    calls = 0

    class _Client:
        def call_tool(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"items": {"not": "an array"}}, _usage()
            return {"items": []}, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())
    document = "".join(
        f"--- Page {page} ---\n" + (letter * 10_000) + "\n"
        for page, letter in ((1, "a"), (2, "b"), (3, "c"))
    )

    result = compliance_matrix.extract_compliance_items(
        document_text=document,
        filename="recoverable.pdf",
        proposal_id=1,
    )

    assert calls == 3
    assert result.coverage_state == "complete"
    assert result.source_chunks_total == 1
    assert result.source_chunks_completed == 1
    assert result.failed_chunk_labels == []


def test_large_document_chunk_exception_is_reported_partial(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **kwargs):
            prompt = kwargs["messages"][0]["content"]
            if "(chunk 2/2)" in prompt:
                raise RuntimeError("simulated provider failure")
            return {
                "items": [_item(text="Vendor shall provide a plan.")],
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    document = (
        "--- Page 1 ---\nVendor shall provide a plan.\n" + ("a" * 39_950) + "\n"
        "--- Page 2 ---\n" + ("b" * 40_000) + "\n"
        "--- Page 3 ---\n" + ("c" * 40_000) + "\n"
    )
    result = compliance_matrix.extract_compliance_items(
        document_text=document,
        filename="large.pdf",
        proposal_id=1,
    )

    assert len(result.items) == 1
    assert result.coverage_state == "partial"
    assert result.source_chunks_total == 2
    assert result.source_chunks_completed == 1
    assert result.failed_chunk_labels == ["chunk 2/2"]
    assert "chunk_call_failed" in result.incomplete_reasons


def test_delta_missing_required_arrays_is_failed(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {}, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text=(
            "--- Page 1 ---\nVendor shall provide a plan.\n"
            "Vendor must provide a schedule."
        ),
        filename="amendment.pdf",
        proposal_id=1,
        delta_mode=True,
    )

    assert result.coverage_state == "failed"
    assert result.source_chunks_completed == 0
    assert result.failed_chunk_labels == ["document"]
    assert "malformed_tool_payload" in result.incomplete_reasons


def test_delta_output_cap_is_partial(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "new_items": [
                    _item("REQ-NEW-1", "Vendor shall provide a plan."),
                    _item("REQ-NEW-2", "Vendor must provide a schedule."),
                ],
                "modified_items": [],
                "removed_items": [],
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text=(
            "--- Page 1 ---\nVendor shall provide a plan.\n"
            "Vendor must provide a schedule."
        ),
        filename="amendment.pdf",
        proposal_id=1,
        delta_mode=True,
        max_items=1,
    )

    assert len(result.new_items) == 1
    assert result.coverage_state == "partial"
    assert result.output_capped is True
    assert result.source_chunks_completed == 1


def test_delta_total_cap_includes_modified_and_removed_items(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "new_items": [],
                "modified_items": [
                    {
                        "existing_id": "REQ-001",
                        "new_text": "Vendor shall provide the revised plan.",
                        "change_summary": "The plan requirement was revised.",
                    },
                    {
                        "existing_id": "REQ-002",
                        "new_text": "Vendor shall provide the revised schedule.",
                        "change_summary": "The schedule requirement was revised.",
                    },
                ],
                "removed_items": [
                    {"existing_id": "REQ-003", "reason": "Deleted by amendment."},
                ],
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text=(
            "--- Page 1 ---\nVendor shall provide the revised plan.\n"
            "Vendor shall provide the revised schedule."
        ),
        filename="amendment.pdf",
        proposal_id=1,
        existing_items=[
            {"requirement_id": "REQ-001"},
            {"requirement_id": "REQ-002"},
            {"requirement_id": "REQ-003"},
        ],
        delta_mode=True,
        max_items=2,
    )

    assert len(result.modified_items) == 2
    assert result.removed_items == []
    assert result.coverage_state == "partial"
    assert result.extraction_complete is False
    assert result.output_capped is True


def test_invalid_ordinary_item_values_are_partial_not_coerced(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "items": [
                    {
                        "requirement_id": "REQ-001",
                        "requirement_text": None,
                        "requirement_type": None,
                        "category": None,
                        "source_page": "bad",
                        "weight": "bad",
                    }
                ]
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text="--- Page 1 ---\nVendor shall submit a plan.",
        filename="invalid.pdf",
        proposal_id=1,
    )

    assert result.items == []
    assert result.coverage_state == "partial"
    assert result.extraction_complete is False
    assert result.malformed_items_skipped == 1


def test_invalid_delta_values_and_unknown_ids_are_partial(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "new_items": [
                    {
                        "requirement_id": "REQ-NEW",
                        "requirement_text": "Vendor shall submit a plan.",
                        "requirement_type": "not-a-type",
                        "category": "technical",
                        "source_page": "bad",
                        "weight": float("nan"),
                    }
                ],
                "modified_items": [
                    {
                        "existing_id": "REQ-UNKNOWN",
                        "new_text": "Vendor shall submit a revised plan.",
                        "change_summary": "Revised.",
                    }
                ],
                "removed_items": [
                    {"existing_id": "REQ-001", "reason": None},
                ],
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text="--- Page 1 ---\nAmendment text.",
        filename="amendment.pdf",
        proposal_id=1,
        existing_items=[{"requirement_id": "REQ-001"}],
        delta_mode=True,
    )

    assert result.new_items == []
    assert result.modified_items == []
    assert result.removed_items == []
    assert result.coverage_state == "partial"
    assert result.extraction_complete is False
    assert result.malformed_items_skipped == 3


def test_chunk_worker_override_bounds_large_document_pool(monkeypatch):
    from app.agents import compliance_matrix

    observed_worker_counts: list[int] = []
    real_executor = compliance_matrix.ThreadPoolExecutor

    class _RecordingExecutor:
        def __init__(self, *, max_workers, **kwargs):
            observed_worker_counts.append(max_workers)
            self._delegate = real_executor(max_workers=max_workers, **kwargs)

        def __enter__(self):
            self._delegate.__enter__()
            return self._delegate

        def __exit__(self, *args):
            return self._delegate.__exit__(*args)

    class _Client:
        def call_tool(self, **_kwargs):
            return {"items": []}, _usage()

    monkeypatch.setattr(compliance_matrix, "ThreadPoolExecutor", _RecordingExecutor)
    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())
    document = "".join(
        f"--- Page {page} ---\n" + (letter * 40_000) + "\n"
        for page, letter in ((1, "a"), (2, "b"), (3, "c"))
    )

    result = compliance_matrix.extract_compliance_items(
        document_text=document,
        filename="large.pdf",
        proposal_id=1,
        max_workers=1,
    )

    assert result.extraction_complete is True
    assert observed_worker_counts == [1]


def test_page_split_preserves_text_before_first_marker():
    from app.agents.compliance_matrix import _split_text_by_pages

    text = (
        "IMPORTANT PREAMBLE\n"
        "--- Page 1 ---\n" + ("a" * 30) + "\n"
        "--- Page 2 ---\n" + ("b" * 30) + "\n"
        "--- Page 3 ---\n" + ("c" * 30)
    )

    chunks = _split_text_by_pages(text, target_chars=40)

    assert len(chunks) >= 2
    assert chunks[0].startswith("IMPORTANT PREAMBLE")
    assert "".join(chunks) == text


@pytest.mark.parametrize(
    ("requirement_text", "source_page"),
    [
        ("Vendor shall construct a lunar base.", 1),
        ("Vendor shall encrypt customer data.", 999),
    ],
)
def test_ordinary_items_must_be_grounded_on_a_real_cited_page(
    monkeypatch,
    requirement_text,
    source_page,
):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "items": [
                    {
                        **_item(text=requirement_text),
                        "source_page": source_page,
                    }
                ]
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text="--- Page 1 ---\nVendor shall encrypt customer data.",
        filename="grounding.pdf",
        proposal_id=1,
    )

    assert result.items == []
    assert result.coverage_state == "partial"
    assert result.malformed_items_skipped == 1


def test_requirement_may_span_a_page_break_when_cited_page_participates(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "items": [
                    _item(
                        text=(
                            "The contractor shall encrypt all customer data at rest."
                        )
                    )
                ]
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text=(
            "--- Page 1 ---\nThe contractor shall encrypt all customer\n"
            "--- Page 2 ---\ndata at rest."
        ),
        filename="page-break.pdf",
        proposal_id=1,
    )

    assert len(result.items) == 1
    assert result.extraction_complete is True


@pytest.mark.parametrize(
    "fabricated_section",
    [
        "Mars Annex",
        "Vendor shall encrypt customer data.",
        "encrypt customer",
    ],
)
def test_unsupported_source_section_is_not_presented_as_verified(
    monkeypatch,
    fabricated_section,
):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            item = _item(text="Vendor shall encrypt customer data.")
            item["source_section"] = fabricated_section
            return {"items": [item]}, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text="--- Page 1 ---\nVendor shall encrypt customer data.",
        filename="sections.pdf",
        proposal_id=1,
    )

    assert result.extraction_complete is True
    assert len(result.items) == 1
    assert result.items[0].source_section is None


def test_structural_source_section_is_retained(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            item = _item(text="Vendor shall encrypt customer data.")
            item["source_section"] = "Section 3.2"
            return {"items": [item]}, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())
    result = compliance_matrix.extract_compliance_items(
        document_text=(
            "--- Page 1 ---\n3.2 Technical Requirements\n"
            "Vendor shall encrypt customer data."
        ),
        filename="sections.pdf",
        proposal_id=1,
    )

    assert result.extraction_complete is True
    assert result.items[0].source_section == "Section 3.2"


@pytest.mark.parametrize(
    ("requirement_type", "weight", "text", "accepted"),
    [
        ("shall", 5, "Vendor shall submit five reports.", False),
        (
            "evaluation_criterion",
            60,
            "Technical approach will be evaluated at 60.0 points.",
            True,
        ),
        (
            "evaluation_criterion",
            0.6,
            "Technical approach carries 60.0 percent of the score.",
            True,
        ),
    ],
)
def test_extracted_weight_requires_grounded_evaluation_language(
    monkeypatch,
    requirement_type,
    weight,
    text,
    accepted,
):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            item = _item(text=text)
            item["requirement_type"] = requirement_type
            item["weight"] = weight
            return {"items": [item]}, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text=f"--- Page 1 ---\n{text}",
        filename="weights.pdf",
        proposal_id=1,
    )

    assert bool(result.items) is accepted
    assert result.extraction_complete is accepted


def test_delta_new_and_modified_text_must_be_grounded(monkeypatch):
    from app.agents import compliance_matrix

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "new_items": [_item("REQ-NEW", "Vendor shall build a lunar base.")],
                "modified_items": [
                    {
                        "existing_id": "REQ-001",
                        "new_text": "Vendor shall migrate operations to Mars.",
                        "change_summary": "Location changed.",
                    }
                ],
                "removed_items": [],
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())

    result = compliance_matrix.extract_compliance_items(
        document_text="--- Page 1 ---\nVendor shall encrypt customer data.",
        filename="amendment.pdf",
        proposal_id=1,
        existing_items=[{"requirement_id": "REQ-001"}],
        delta_mode=True,
    )

    assert result.new_items == []
    assert result.modified_items == []
    assert result.coverage_state == "partial"
    assert result.malformed_items_skipped == 2


def test_delta_rejects_existing_text_as_new_and_no_op_modification(monkeypatch):
    from app.agents import compliance_matrix

    existing_text = "Vendor shall encrypt customer data."

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "new_items": [_item("REQ-NEW", existing_text)],
                "modified_items": [
                    {
                        "existing_id": "REQ-001",
                        "new_text": existing_text,
                        "change_summary": "Restated without change.",
                    }
                ],
                "removed_items": [],
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())
    result = compliance_matrix.extract_compliance_items(
        document_text=f"--- Page 1 ---\n{existing_text}",
        filename="restatement.pdf",
        proposal_id=1,
        existing_items=[
            {"requirement_id": "REQ-001", "requirement_text": existing_text}
        ],
        delta_mode=True,
    )

    assert result.new_items == []
    assert result.modified_items == []
    assert result.coverage_state == "partial"
    assert "duplicate_delta_requirement" in result.incomplete_reasons
    assert "no_op_delta_modification" in result.incomplete_reasons


def test_delta_modified_text_may_span_a_source_page_break(monkeypatch):
    from app.agents import compliance_matrix

    revised = "Vendor shall encrypt all customer data at rest."

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "new_items": [],
                "modified_items": [
                    {
                        "existing_id": "REQ-001",
                        "new_text": revised,
                        "change_summary": "Encryption scope expanded.",
                    }
                ],
                "removed_items": [],
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())
    result = compliance_matrix.extract_compliance_items(
        document_text=(
            "--- Page 1 ---\nVendor shall encrypt all customer\n"
            "--- Page 2 ---\ndata at rest."
        ),
        filename="page-break-amendment.pdf",
        proposal_id=1,
        existing_items=[
            {
                "requirement_id": "REQ-001",
                "requirement_text": "Vendor shall encrypt customer data.",
            }
        ],
        delta_mode=True,
    )

    assert result.extraction_complete is True
    assert [item["new_text"] for item in result.modified_items] == [revised]


@pytest.mark.parametrize("conflict_kind", ["duplicate_modify", "modify_remove"])
def test_conflicting_delta_operations_are_partial_and_fail_closed(
    monkeypatch,
    conflict_kind,
):
    from app.agents import compliance_matrix
    from app.jobs.amendment import _require_complete_amendment_extraction

    modified = [
        {
            "existing_id": "REQ-001",
            "new_text": "Vendor shall submit the revised plan.",
            "change_summary": "Plan revised.",
        }
    ]
    removed = []
    if conflict_kind == "duplicate_modify":
        modified.append(
            {
                "existing_id": "REQ-001",
                "new_text": "Vendor shall submit the final plan.",
                "change_summary": "Plan revised again.",
            }
        )
    else:
        removed.append({"existing_id": "REQ-001", "reason": "Plan removed."})

    class _Client:
        def call_tool(self, **_kwargs):
            return {
                "new_items": [],
                "modified_items": modified,
                "removed_items": removed,
            }, _usage()

    monkeypatch.setattr(compliance_matrix, "get_anthropic", lambda: _Client())
    result = compliance_matrix.extract_compliance_items(
        document_text=(
            "--- Page 1 ---\nVendor shall submit the revised plan.\n"
            "Vendor shall submit the final plan."
        ),
        filename="amendment.pdf",
        proposal_id=1,
        existing_items=[{"requirement_id": "REQ-001"}],
        delta_mode=True,
    )

    assert result.coverage_state == "partial"
    assert "conflicting_delta_operations" in result.incomplete_reasons
    with pytest.raises(RuntimeError):
        _require_complete_amendment_extraction(result)
