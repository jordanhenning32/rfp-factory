from __future__ import annotations

from types import SimpleNamespace

import pytest


def _items(count: int) -> list[dict]:
    return [
        {
            "requirement_id": f"REQ-{index:03d}",
            "requirement_text": f"The contractor shall provide deliverable {index}.",
            "requirement_type": "shall",
            "category": "technical",
        }
        for index in range(1, count + 1)
    ]


@pytest.fixture()
def validator(monkeypatch):
    import app.agents.compliance_validator as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            model_compliance_validator="gemini-2.5-pro",
            model_compliance_validator_fallback="claude-haiku-4-5-20251001",
        ),
    )
    return module


def test_clean_response_is_complete_only_after_every_item_reviewed(
    validator, monkeypatch,
) -> None:
    calls: list[dict] = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return {"results": []}, {"input_tokens": 100, "output_tokens": 5}

    monkeypatch.setattr(validator, "call_tool_for_model", fake_call)
    report = validator.validate_compliance_items_report(_items(2))

    assert report.state == "complete"
    assert report.reviewed_count == report.total_count == 2
    assert report.findings == []
    assert calls[0]["model"] == "gemini-2.5-pro"
    assert calls[0]["agent_name"] == "compliance_validator"


def test_primary_failure_retries_exact_smaller_batches(
    validator, monkeypatch,
) -> None:
    batch_sizes: list[tuple[str, int]] = []

    def fake_call(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        size = prompt.count("REQ-ID:")
        batch_sizes.append((kwargs["agent_name"], size))
        if kwargs["agent_name"] == "compliance_validator":
            raise RuntimeError("Gemini returned no function_call")
        return {"results": []}, {}

    monkeypatch.setattr(validator, "call_tool_for_model", fake_call)
    report = validator.validate_compliance_items_report(_items(6))

    assert batch_sizes == [
        ("compliance_validator", 6),
        ("compliance_validator_retry", 3),
        ("compliance_validator_retry", 3),
    ]
    assert report.state == "complete"
    assert report.retry_used
    assert not report.fallback_used
    assert report.reviewed_count == 6


def test_rate_limit_uses_fallback_without_recursive_split(
    validator, monkeypatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def fake_call(**kwargs):
        size = kwargs["messages"][0]["content"].count("REQ-ID:")
        calls.append((kwargs["agent_name"], size))
        if kwargs["model"].startswith("gemini-"):
            raise RuntimeError("status code: 429 rate limit")
        return {"results": []}, {}

    monkeypatch.setattr(validator, "call_tool_for_model", fake_call)
    report = validator.validate_compliance_items_report(_items(6))

    assert calls == [
        ("compliance_validator", 6),
        ("compliance_validator_fallback", 6),
    ]
    assert report.state == "degraded"


@pytest.mark.parametrize(
    "message",
    [
        "invalid reason mentions gateway timeout",
        "invalid reason mentions forbidden credentials",
        "invalid reason mentions rate limit status code: 503",
    ],
)
def test_protocol_errors_are_never_provider_wide_failures(
    validator, message,
) -> None:
    error = validator.ValidationProtocolError(message)

    assert not validator._is_provider_wide_failure(error)


def test_haiku_leaf_fallback_is_degraded_not_clean(
    validator, monkeypatch,
) -> None:
    def fake_call(**kwargs):
        if kwargs["model"].startswith("gemini-"):
            raise RuntimeError("Gemini returned no function_call")
        return {"results": []}, {}

    monkeypatch.setattr(validator, "call_tool_for_model", fake_call)
    report = validator.validate_compliance_items_report(_items(2))

    assert report.state == "degraded"
    assert report.reviewed_count == 2
    assert report.fallback_used
    assert report.fallback_reviewed_count == 2
    assert report.findings == []


def test_legacy_findings_only_wrapper_rejects_fallback_clean_result(
    validator, monkeypatch,
) -> None:
    def fake_call(**kwargs):
        if kwargs["model"].startswith("gemini-"):
            raise RuntimeError("status code: 503 service unavailable")
        return {"results": []}, {}

    monkeypatch.setattr(validator, "call_tool_for_model", fake_call)

    with pytest.raises(validator.ComplianceValidationIncompleteError):
        validator.validate_compliance_items(_items(1))


def test_haiku_fallback_finding_is_marked_non_primary(
    validator, monkeypatch,
) -> None:
    finding = {
        "results": [
            {
                "requirement_id": "REQ-001",
                "issue": "category_misclassified",
                "suggested_category": "pricing",
                "confidence": "HIGH",
                "reason": "The row requests a labor rate.",
            }
        ]
    }

    def fake_call(**kwargs):
        if kwargs["model"].startswith("gemini-"):
            raise RuntimeError("Gemini returned no function_call")
        return finding, {}

    monkeypatch.setattr(validator, "call_tool_for_model", fake_call)
    report = validator.validate_compliance_items_report(_items(1))

    assert report.state == "degraded"
    assert report.findings[0].review_role == "fallback"


def test_both_providers_failing_never_becomes_empty_clean_result(
    validator, monkeypatch,
) -> None:
    monkeypatch.setattr(
        validator,
        "call_tool_for_model",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )
    report = validator.validate_compliance_items_report(_items(2))

    assert report.state == "failed"
    assert report.reviewed_count == 0
    assert report.unresolved_requirement_ids == ["REQ-001", "REQ-002"]
    with pytest.raises(validator.ComplianceValidationIncompleteError):
        validator.validate_compliance_items(_items(2))


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},
        {"results": "not-an-array"},
        {"results": [{"requirement_id": "UNKNOWN"}]},
        {
            "results": [
                {
                    "requirement_id": "REQ-001",
                    "issue": "type_misclassified",
                    "confidence": "HIGH",
                    "reason": "Wrong type.",
                }
            ]
        },
        {
            "results": [
                {
                    "requirement_id": "REQ-001",
                    "issue": "text_is_truncated_or_incomplete",
                    "suggested_type": "should",
                    "confidence": "HIGH",
                    "reason": "The visible row looks clipped.",
                }
            ]
        },
    ],
)
def test_malformed_primary_payload_uses_fallback_and_is_not_reported_clean(
    validator, monkeypatch, bad_payload,
) -> None:
    def fake_call(**kwargs):
        if kwargs["model"].startswith("gemini-"):
            return bad_payload, {}
        return {"results": []}, {}

    monkeypatch.setattr(validator, "call_tool_for_model", fake_call)
    report = validator.validate_compliance_items_report(_items(1))

    assert report.state == "degraded"
    assert report.primary_failed_attempts == 1
    assert report.fallback_used
    assert report.reviewed_count == 1


def test_valid_finding_survives_strict_contract(
    validator, monkeypatch,
) -> None:
    payload = {
        "results": [
            {
                "requirement_id": "REQ-001",
                "issue": "category_misclassified",
                "suggested_category": "pricing",
                "confidence": "HIGH",
                "reason": "The row requests labor rates.",
            }
        ]
    }
    monkeypatch.setattr(
        validator,
        "call_tool_for_model",
        lambda **_kwargs: (payload, {}),
    )
    report = validator.validate_compliance_items_report(_items(1))

    assert report.state == "complete"
    assert report.findings[0].suggested_category == "pricing"


def test_intake_never_auto_applies_high_fallback_finding(monkeypatch) -> None:
    import app.jobs.intake as intake
    from app.agents.compliance_matrix import ExtractedComplianceItem
    from app.agents.compliance_validator import (
        ComplianceValidationReport,
        ValidationResult,
    )

    item = ExtractedComplianceItem(
        requirement_id="REQ-001",
        requirement_text="The offeror shall provide labor rates.",
        requirement_type="shall",
        category="technical",
    )
    report = ComplianceValidationReport(
        total_count=1,
        primary_model="gemini-2.5-pro",
        fallback_model="claude-haiku-4-5-20251001",
        findings=[
            ValidationResult(
                requirement_id="REQ-001",
                issue="category_misclassified",
                suggested_type=None,
                suggested_category="pricing",
                confidence="HIGH",
                reason="The row requests labor rates.",
                review_role="fallback",
            )
        ],
        reviewed_requirement_ids=["REQ-001"],
    )
    monkeypatch.setattr(
        intake,
        "validate_compliance_items_report",
        lambda *_args, **_kwargs: report,
    )
    monkeypatch.setattr(intake, "_set_stage", lambda *_args, **_kwargs: None)

    applied = intake._validate_and_apply_corrections([item], proposal_id=-1)

    assert item.category == "technical"
    assert applied.auto_applied_count == 0
    assert applied.flagged_for_review_count == 1
    public = applied.as_public_dict()
    assert public["manual_review_count"] == 1
    assert public["manual_review"] == [
        {
            "requirement_id": "REQ-001",
            "issue": "category_misclassified",
            "confidence": "HIGH",
            "reason": "The row requests labor rates.",
            "suggested_type": None,
            "suggested_category": "pricing",
            "review_role": "fallback",
        }
    ]


def test_intake_blocks_mandatory_to_should_downgrade(monkeypatch) -> None:
    import app.jobs.intake as intake
    from app.agents.compliance_matrix import ExtractedComplianceItem
    from app.agents.compliance_validator import (
        ComplianceValidationReport,
        ValidationResult,
    )

    item = ExtractedComplianceItem(
        requirement_id="REQ-001",
        requirement_text="The contractor shall provide monthly reports.",
        requirement_type="shall",
        category="management",
    )
    report = ComplianceValidationReport(
        total_count=1,
        primary_model="gemini-2.5-pro",
        fallback_model="claude-haiku-4-5-20251001",
        findings=[
            ValidationResult(
                requirement_id="REQ-001",
                issue="type_misclassified",
                suggested_type="should",
                suggested_category=None,
                confidence="HIGH",
                reason="The reviewer claims this is advisory.",
            )
        ],
        reviewed_requirement_ids=["REQ-001"],
    )
    monkeypatch.setattr(
        intake,
        "validate_compliance_items_report",
        lambda *_args, **_kwargs: report,
    )
    monkeypatch.setattr(intake, "_set_stage", lambda *_args, **_kwargs: None)

    result = intake._validate_and_apply_corrections([item], proposal_id=-1)

    assert item.requirement_type == "shall"
    assert result.auto_applied_count == 0
    assert result.blocked_correction_count == 1
    assert result.manual_review_findings[0].requirement_id == "REQ-001"


def test_intake_persists_auditable_auto_applied_change(monkeypatch) -> None:
    import app.jobs.intake as intake
    from app.agents.compliance_matrix import ExtractedComplianceItem
    from app.agents.compliance_validator import (
        ComplianceValidationReport,
        ValidationResult,
    )

    item = ExtractedComplianceItem(
        requirement_id="REQ-001",
        requirement_text="The offeror shall provide labor rates.",
        requirement_type="shall",
        category="technical",
    )
    report = ComplianceValidationReport(
        total_count=1,
        primary_model="gemini-2.5-pro",
        fallback_model="claude-haiku-4-5-20251001",
        findings=[
            ValidationResult(
                requirement_id="REQ-001",
                issue="category_misclassified",
                suggested_type=None,
                suggested_category="pricing",
                confidence="HIGH",
                reason="This row requests labor rates.",
            )
        ],
        reviewed_requirement_ids=["REQ-001"],
    )
    monkeypatch.setattr(
        intake,
        "validate_compliance_items_report",
        lambda *_args, **_kwargs: report,
    )
    monkeypatch.setattr(intake, "_set_stage", lambda *_args, **_kwargs: None)

    result = intake._validate_and_apply_corrections([item], proposal_id=-1)

    assert item.category == "pricing"
    assert result.auto_applied_count == 1
    assert result.as_public_dict()["auto_applied"] == [
        {
            "requirement_id": "REQ-001",
            "field": "category",
            "from": "technical",
            "to": "pricing",
            "issue": "category_misclassified",
            "reason": "This row requests labor rates.",
            "review_role": "primary",
        }
    ]
