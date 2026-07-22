from __future__ import annotations

from types import SimpleNamespace

import pytest

_SOURCE = """--- Page 1 ---
3.1 Technical Requirements
The contractor shall encrypt all data at rest.
The offeror must submit a signed security certification with its proposal.
"""


def _existing() -> list[dict]:
    return [
        {
            "requirement_id": "REQ-001",
            "requirement_text": "The contractor shall encrypt all data at rest.",
            "requirement_type": "shall",
            "category": "technical",
            "source_page": 1,
        }
    ]


@pytest.fixture()
def completeness(monkeypatch):
    import app.agents.compliance_completeness as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            model_compliance_validator="gemini-2.5-pro",
            model_compliance_validator_fallback="claude-haiku-4-5-20251001",
        ),
    )
    return module


def _missing_payload(
    text: str | None = None,
    *,
    confidence: str = "HIGH",
    source_section: str | None = "3.1",
    requirement_type: str = "mandatory_form",
    category: str = "certification",
    weight: float | None = None,
) -> dict:
    candidates = []
    if text is not None:
        candidates.append(
            {
                "requirement_text": text,
                "source_page": 1,
                "source_section": source_section,
                "requirement_type": requirement_type,
                "category": category,
                "weight": weight,
                "confidence": confidence,
                "reason": "This mandatory submission is absent from the matrix.",
            }
        )
    return {"missing_candidates": candidates, "uncertain_passages": []}


def test_source_audit_verifies_and_recovers_high_confidence_omission(
    completeness, monkeypatch,
) -> None:
    missing = "The offeror must submit a signed security certification with its proposal."
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (_missing_payload(missing), {}),
    )

    report = completeness.audit_compliance_completeness(
        source_text=_SOURCE,
        source_filename="security-rfp.pdf",
        items=_existing(),
    )

    assert report.state == "complete"
    assert report.reviewed_units == report.source_units_total == 1
    assert len(report.auto_add_candidates) == 1
    assert report.auto_add_candidates[0].requirement_text == missing


def test_source_audit_preserves_text_before_first_page_marker(
    completeness, monkeypatch,
) -> None:
    missing = (
        "The offeror must submit a signed security certification with its proposal."
    )
    source = (
        missing
        + "\n--- Page 1 ---\n"
        + "Agency background and administrative information."
    )
    seen_source_units: list[str] = []

    def fake_call(**kwargs):
        seen_source_units.append(kwargs["messages"][0]["content"])
        return _missing_payload(missing, source_section=None), {}

    monkeypatch.setattr(completeness, "call_tool_for_model", fake_call)

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="preamble.pdf",
        items=[],
    )

    assert report.reviewed_units == report.source_units_total == 1
    assert len(report.auto_add_candidates) == 1
    assert report.auto_add_candidates[0].requirement_text == missing
    assert missing in seen_source_units[0]
    assert completeness._blank_source_pages(source) == []


def test_source_audit_never_auto_adds_a_literal_requirement_fragment(
    completeness, monkeypatch,
) -> None:
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(
                "shall",
                requirement_type="shall",
                category="technical",
            ),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=_SOURCE,
        source_filename="security-rfp.pdf",
        items=_existing(),
    )

    assert report.auto_add_candidates == []
    assert len(report.manual_review_candidates) == 1
    candidate = report.manual_review_candidates[0]
    assert candidate.requirement_text == "shall"
    assert "not a complete requirement" in candidate.reason


@pytest.mark.parametrize(
    "fragment,requirement_type",
    [
        ("contractor shall provide", "shall"),
        ("offeror must submit", "must"),
        ("Submit the signed certification", "mandatory_form"),
        ("Technical approach will be evaluated", "evaluation_criterion"),
        (
            "The offeror must submit a signed security certification with",
            "mandatory_form",
        ),
        (
            "The contractor shall provide all required cybersecurity documentation and",
            "shall",
        ),
        (
            "and the contractor shall provide all required documentation",
            "shall",
        ),
        (
            "because the offeror must submit a signed security certification",
            "must",
        ),
    ],
)
def test_source_audit_keeps_short_clipped_clauses_out_of_auto_add(
    completeness, monkeypatch, fragment, requirement_type,
) -> None:
    source = f"--- Page 1 ---\n3.1 Requirements\n{fragment}\n"
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(
                fragment,
                source_section="3.1",
                requirement_type=requirement_type,
            ),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="fragment.pdf",
        items=[],
    )

    assert report.auto_add_candidates == []
    assert len(report.manual_review_candidates) == 1
    assert "not a complete requirement" in report.manual_review_candidates[0].reason


def test_unverified_section_and_weight_are_removed_and_require_manual_review(
    completeness, monkeypatch,
) -> None:
    missing = "The offeror must submit a signed security certification with its proposal."
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(
                missing,
                source_section="Invented Section 99.9",
                weight=99.9,
            ),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=_SOURCE,
        source_filename="security-rfp.pdf",
        items=_existing(),
    )

    assert report.auto_add_candidates == []
    assert len(report.manual_review_candidates) == 1
    candidate = report.manual_review_candidates[0]
    assert candidate.source_section is None
    assert candidate.weight is None
    assert "source section is not supported" in candidate.reason
    assert "weight is not supported" in candidate.reason


def test_requirement_prose_cannot_be_accepted_as_its_own_source_section(
    completeness, monkeypatch,
) -> None:
    missing = "The offeror must submit a signed security certification with its proposal."
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(missing, source_section=missing),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=_SOURCE,
        source_filename="security-rfp.pdf",
        items=_existing(),
    )

    assert report.auto_add_candidates == []
    assert len(report.manual_review_candidates) == 1
    candidate = report.manual_review_candidates[0]
    assert candidate.source_section is None
    assert "source section is not supported" in candidate.reason


@pytest.mark.parametrize("field", ["candidate", "uncertain"])
def test_boolean_source_page_is_rejected_by_strict_parser(
    completeness, field,
) -> None:
    missing = "The offeror must submit a signed security certification with its proposal."
    payload = _missing_payload(missing)
    if field == "candidate":
        payload["missing_candidates"][0]["source_page"] = True
    else:
        payload = {
            "missing_candidates": [],
            "uncertain_passages": [
                {"source_page": True, "reason": "Unreadable source passage."}
            ],
        }
    unit = completeness._source_units(_SOURCE)[0]

    with pytest.raises(completeness.CompletenessProtocolError):
        completeness._parse_payload_strict(payload, unit, _existing())


def test_verified_evaluation_weight_and_section_remain_auto_add_eligible(
    completeness, monkeypatch,
) -> None:
    source = """--- Page 1 ---
M.2 Evaluation Factors
Technical approach will be evaluated at 60 points.
"""
    quote = "Technical approach will be evaluated at 60 points."
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(
                quote,
                source_section="M.2",
                requirement_type="evaluation_criterion",
                category="technical",
                weight=60,
            ),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="evaluation.pdf",
        items=[],
    )

    assert len(report.auto_add_candidates) == 1
    candidate = report.auto_add_candidates[0]
    assert candidate.source_section == "M.2"
    assert candidate.weight == 60


def test_clipped_prefix_of_a_longer_source_sentence_is_manual_only(
    completeness, monkeypatch,
) -> None:
    full = (
        "The contractor shall provide all required support documentation "
        "and monthly security reports."
    )
    clipped = "The contractor shall provide all required support documentation"
    source = f"--- Page 1 ---\n3.1 Requirements\n{full}\n"
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (_missing_payload(clipped), {}),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="clipped.pdf",
        items=[],
    )

    assert report.auto_add_candidates == []
    assert len(report.manual_review_candidates) == 1
    assert "complete source sentence or line" in report.manual_review_candidates[0].reason


def test_suffix_after_a_comma_loses_context_and_is_manual_only(
    completeness, monkeypatch,
) -> None:
    full = (
        "During option periods, the contractor shall provide all required "
        "support services."
    )
    suffix = "the contractor shall provide all required support services."
    source = f"--- Page 1 ---\n3.1 Requirements\n{full}\n"
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (_missing_payload(suffix), {}),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="suffix.pdf",
        items=[],
    )

    assert report.auto_add_candidates == []
    assert len(report.manual_review_candidates) == 1
    assert "complete source sentence or line" in report.manual_review_candidates[0].reason


@pytest.mark.parametrize("separator", [":", ";"])
def test_suffix_after_clause_separator_loses_context_and_is_manual_only(
    completeness, monkeypatch, separator,
) -> None:
    suffix = "the contractor shall provide all required support services."
    source = (
        f"--- Page 1 ---\nDuring option periods{separator} {suffix}\n"
    )
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(suffix, source_section=None),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="suffix.pdf",
        items=[],
    )

    assert report.auto_add_candidates == []
    assert len(report.manual_review_candidates) == 1


def test_numeric_quantity_cannot_be_invented_as_evaluation_weight(
    completeness, monkeypatch,
) -> None:
    quote = "The contractor shall deliver 99.9 reports each month."
    source = f"--- Page 1 ---\n3.1 Requirements\n{quote}\n"
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(
                quote,
                requirement_type="shall",
                category="technical",
                weight=99.9,
            ),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="quantity.pdf",
        items=[],
    )

    candidate = report.manual_review_candidates[0]
    assert candidate.weight is None
    assert "weight is not supported" in candidate.reason


def test_visible_mandatory_language_cannot_be_auto_added_as_should(
    completeness, monkeypatch,
) -> None:
    quote = "The offeror must submit a signed security certification."
    source = f"--- Page 1 ---\n3.1 Requirements\n{quote}\n"
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(
                quote,
                requirement_type="should",
                category="pricing",
            ),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="wrong-type.pdf",
        items=[],
    )

    assert report.auto_add_candidates == []
    candidate = report.manual_review_candidates[0]
    assert "requirement type is not supported" in candidate.reason


@pytest.mark.parametrize(
    "section,source",
    [
        (
            "contractor",
            "--- Page 1 ---\nThe contractor shall provide all required reports.\n",
        ),
        (
            "3.1",
            "--- Page 1 ---\nThe contractor shall comply with contract clause 3.1.\n",
        ),
    ],
)
def test_source_section_must_be_a_structural_header(
    completeness, monkeypatch, section, source,
) -> None:
    quote = source.splitlines()[-1]
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(
                quote,
                source_section=section,
                requirement_type="shall",
                category="technical",
            ),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="section.pdf",
        items=[],
    )

    candidate = report.manual_review_candidates[0]
    assert candidate.source_section is None
    assert "source section is not supported" in candidate.reason


@pytest.mark.parametrize(
    "quote",
    [
        "--- Page 1 ---\nThe contractor shall provide all required support services.",
        "1.2 Scope\nThe contractor shall provide all required support services.",
    ],
)
def test_multiline_or_page_marker_quotes_are_never_auto_added(
    completeness, monkeypatch, quote,
) -> None:
    source = "--- Page 1 ---\n1.2 Scope\nThe contractor shall provide all required support services.\n"
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (
            _missing_payload(
                quote,
                source_section="1.2",
                requirement_type="shall",
                category="technical",
            ),
            {},
        ),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="header.pdf",
        items=[],
    )

    assert report.auto_add_candidates == []
    assert len(report.manual_review_candidates) == 1


def test_truncated_existing_prefix_does_not_suppress_fuller_candidate(
    completeness,
) -> None:
    candidate = "The contractor shall provide monthly reports and remediation plans."
    existing = [
        {
            "requirement_text": "The contractor shall provide monthly reports",
        }
    ]

    assert not completeness._is_represented(candidate, existing)


def test_source_audit_rejects_a_real_quote_attributed_to_the_wrong_page(
    completeness, monkeypatch,
) -> None:
    source = """--- Page 1 ---
Administrative information only.
--- Page 2 ---
The offeror must submit a signed security certification with its proposal.
"""
    quote = "The offeror must submit a signed security certification with its proposal."

    def fake_call(**kwargs):
        if kwargs["model"].startswith("gemini-"):
            return _missing_payload(quote, source_section=None), {}
        return _missing_payload(), {}

    monkeypatch.setattr(completeness, "call_tool_for_model", fake_call)

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="security-rfp.pdf",
        items=[],
    )

    assert report.auto_add_candidates == []
    assert report.candidates == []
    assert report.state == "degraded"
    assert report.fallback_used


def test_blank_marked_page_is_persisted_as_an_uncertain_passage(
    completeness, monkeypatch,
) -> None:
    source = """--- Page 1 ---
The contractor shall provide monthly reports.
--- Page 2 ---
"""
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (_missing_payload(), {}),
    )

    report = completeness.audit_compliance_completeness(
        source_text=source,
        source_filename="partially-scanned.pdf",
        items=[],
    )

    assert [(item.source_page, item.reason) for item in report.uncertain_passages] == [
        (
            2,
            "No extractable text was available on this page; verify whether OCR "
            "or a text-searchable source is needed.",
        )
    ]
    assert report.as_public_dict()["uncertain_passages"][0]["source_page"] == 2


def test_existing_requirement_is_not_returned_as_an_omission(
    completeness, monkeypatch,
) -> None:
    already_present = _existing()[0]["requirement_text"]
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (_missing_payload(already_present), {}),
    )
    report = completeness.audit_compliance_completeness(
        source_text=_SOURCE,
        source_filename="security-rfp.pdf",
        items=_existing(),
    )

    assert report.candidates == []
    assert report.duplicate_candidates_ignored == 1


def test_medium_candidate_requires_human_review(completeness, monkeypatch) -> None:
    missing = "The offeror must submit a signed security certification with its proposal."
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (_missing_payload(missing, confidence="MEDIUM"), {}),
    )
    report = completeness.audit_compliance_completeness(
        source_text=_SOURCE,
        source_filename="security-rfp.pdf",
        items=_existing(),
    )

    assert report.auto_add_candidates == []
    assert len(report.manual_review_candidates) == 1
    assert report.as_public_dict()["manual_review"][0]["source_page"] == 1


def test_hallucinated_quote_invalidates_primary_and_fallback_is_degraded(
    completeness, monkeypatch,
) -> None:
    def fake_call(**kwargs):
        if kwargs["model"].startswith("gemini-"):
            return _missing_payload("The contractor shall operate a lunar base."), {}
        return _missing_payload(), {}

    monkeypatch.setattr(completeness, "call_tool_for_model", fake_call)
    report = completeness.audit_compliance_completeness(
        source_text=_SOURCE,
        source_filename="security-rfp.pdf",
        items=_existing(),
    )

    assert report.state == "degraded"
    assert report.fallback_used
    assert report.candidates == []


def test_fallback_omission_is_never_auto_added(completeness, monkeypatch) -> None:
    missing = "The offeror must submit a signed security certification with its proposal."

    def fake_call(**kwargs):
        if kwargs["model"].startswith("gemini-"):
            raise RuntimeError("Gemini returned no function_call")
        return _missing_payload(missing), {}

    monkeypatch.setattr(completeness, "call_tool_for_model", fake_call)
    report = completeness.audit_compliance_completeness(
        source_text=_SOURCE,
        source_filename="security-rfp.pdf",
        items=_existing(),
    )

    assert report.state == "degraded"
    assert report.auto_add_candidates == []
    assert report.manual_review_candidates[0].review_role == "fallback"


def test_both_source_review_providers_fail_explicitly(
    completeness, monkeypatch,
) -> None:
    monkeypatch.setattr(
        completeness,
        "call_tool_for_model",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    report = completeness.audit_compliance_completeness(
        source_text=_SOURCE,
        source_filename="security-rfp.pdf",
        items=_existing(),
    )

    assert report.state == "failed"
    assert report.reviewed_units == 0
    assert report.unresolved_unit_labels == ["page 1"]


@pytest.mark.parametrize(
    "message",
    ["status code: 429", "rate limit exceeded", "service unavailable", "timed out"],
)
def test_provider_wide_failures_do_not_trigger_recursive_source_splitting(
    completeness, message,
) -> None:
    assert completeness._is_provider_wide_failure(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    [
        "invalid candidate quote mentions gateway timeout",
        "invalid candidate reason mentions forbidden credentials",
        "invalid candidate text mentions rate limit status code: 503",
    ],
)
def test_completeness_protocol_errors_are_never_provider_wide_failures(
    completeness, message,
) -> None:
    error = completeness.CompletenessProtocolError(message)

    assert not completeness._is_provider_wide_failure(error)


def test_intake_appends_only_auto_add_eligible_candidates() -> None:
    from app.agents.compliance_matrix import ExtractedComplianceItem
    from app.jobs.intake import _append_verified_missing_requirements

    items = [
        ExtractedComplianceItem(
            requirement_id="REQ-001",
            requirement_text="The contractor shall encrypt all data at rest.",
            requirement_type="shall",
            category="technical",
            source_page=1,
        )
    ]
    high = SimpleNamespace(
        auto_add_eligible=True,
        requirement_text="The offeror must submit a signed security certification.",
        requirement_type="mandatory_form",
        category="certification",
        source_section="3.1",
        source_page=1,
        weight=None,
    )
    medium = SimpleNamespace(
        auto_add_eligible=False,
        requirement_text="The offeror should describe optional training.",
        requirement_type="should",
        category="technical",
        source_section="3.2",
        source_page=1,
        weight=None,
    )

    assert _append_verified_missing_requirements(items, [high, medium]) == 1
    assert len(items) == 2
    assert items[-1].requirement_text == high.requirement_text
