from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace


def _seed_two_document_proposal(db_session) -> tuple[int, list[int]]:
    from app.core.enums import ProposalStatus
    from app.models import Proposal, RfpPackage, RfpPackageDocument

    with db_session.session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://requirements-review",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Multi-file requirements review",
            status=ProposalStatus.INTAKING,
        )
        db.add(proposal)
        db.flush()
        docs = [
            RfpPackageDocument(
                rfp_package_id=package.id,
                filename="first.pdf",
                storage_path="memory://first.pdf",
                extracted_text_md="--- Page 1 ---\nThe contractor shall provide A.",
                structure_json={"content_sha256": "first-hash"},
            ),
            RfpPackageDocument(
                rfp_package_id=package.id,
                filename="second.xlsx",
                storage_path="memory://second.xlsx",
                extracted_text_md="--- Page 1 ---\nThe offeror must provide B.",
                structure_json={"content_sha256": "second-hash"},
            ),
        ]
        db.add_all(docs)
        db.flush()
        return proposal.id, [doc.id for doc in docs]


def test_document_review_state_merges_without_erasing_existing_metadata(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.jobs.intake as intake
    from app.models import RfpPackageDocument

    monkeypatch.setattr(intake, "session_scope", db_session.session_scope)
    _proposal_id, document_ids = _seed_two_document_proposal(db_session)

    intake._update_document_requirements_review(
        document_ids[0],
        {"schema_version": 1, "status": "reviewing"},
    )

    with db_session.session_scope() as db:
        document = db.get(RfpPackageDocument, document_ids[0])
        assert document.structure_json["content_sha256"] == "first-hash"
        review = document.structure_json["requirements_review"]
        assert review["status"] == "reviewing"
        assert review["updated_at"]


def test_review_state_write_failure_is_not_silently_treated_as_legacy(
    monkeypatch,
) -> None:
    import pytest

    import app.jobs.intake as intake

    @contextmanager
    def broken_session_scope():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(intake, "session_scope", broken_session_scope)

    with pytest.raises(RuntimeError, match="database unavailable"):
        intake._update_document_requirements_review(7, {"status": "failed"})


def test_document_review_details_are_remapped_to_proposal_global_ids(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.jobs.intake as intake
    from app.models import RfpPackageDocument

    monkeypatch.setattr(intake, "session_scope", db_session.session_scope)
    _proposal_id, document_ids = _seed_two_document_proposal(db_session)
    temporary_id = f"DOC-{document_ids[0]}-REQ-001"
    intake._update_document_requirements_review(
        document_ids[0],
        {
            "status": "partial",
            "classification": {
                "unresolved_requirement_ids": [temporary_id],
                "manual_review": [
                    {
                        "requirement_id": temporary_id,
                        "issue": "truncated_text",
                        "reason": "Check the source sentence.",
                    }
                ],
                "auto_applied": [
                    {
                        "requirement_id": temporary_id,
                        "field": "category",
                        "from": "technical",
                        "to": "pricing",
                    }
                ],
            },
        },
    )

    intake._finalize_document_requirements_review_ids(
        document_ids[0],
        {temporary_id: "REQ-001"},
        ["REQ-001"],
    )

    with db_session.session_scope() as db:
        document = db.get(RfpPackageDocument, document_ids[0])
        review = document.structure_json["requirements_review"]
        assert review["classification"]["unresolved_requirement_ids"] == ["REQ-001"]
        assert review["classification"]["manual_review"][0][
            "requirement_id"
        ] == "REQ-001"
        assert review["classification"]["auto_applied"][0][
            "requirement_id"
        ] == "REQ-001"
        assert review["recovered_requirement_ids"] == ["REQ-001"]


def test_parallel_documents_receive_proposal_global_ids_in_source_order(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.jobs.intake as intake
    from app.agents.compliance_matrix import ExtractedComplianceItem
    from app.models import ComplianceMatrixItem, RfpPackageDocument

    monkeypatch.setattr(intake, "session_scope", db_session.session_scope)
    proposal_id, document_ids = _seed_two_document_proposal(db_session)
    extraction_worker_budgets: list[int] = []

    def fake_worker(_proposal_id: int, doc: dict):
        extraction_worker_budgets.append(int(doc["extraction_workers"]))
        # Finish the first source last to prove persistence/order does not
        # depend on ThreadPool completion order.
        if doc["filename"] == "first.pdf":
            time.sleep(0.03)
        item = ExtractedComplianceItem(
            requirement_id="REQ-001",
            requirement_text=f"Requirement from {doc['filename']}",
            requirement_type="shall",
            category="technical",
            source_page=1,
            extraction_origin=(
                "completeness" if doc["filename"] == "second.xlsx" else "primary"
            ),
        )
        return int(doc["id"]), str(doc["filename"]), [item]

    monkeypatch.setattr(intake, "_extract_one_doc_for_matrix", fake_worker)
    assert intake._run_compliance_matrix(proposal_id) == 2
    provider_budget = max(1, int(intake.get_settings().shortfall_workers or 1))
    outer_workers = min(2, provider_budget)
    assert extraction_worker_budgets == [
        max(1, provider_budget // outer_workers),
        max(1, provider_budget // outer_workers),
    ]

    with db_session.session_scope() as db:
        rows = (
            db.query(ComplianceMatrixItem)
            .filter(ComplianceMatrixItem.proposal_id == proposal_id)
            .order_by(ComplianceMatrixItem.id)
            .all()
        )
        assert [row.requirement_id for row in rows] == ["REQ-001", "REQ-002"]
        assert [row.source_document_id for row in rows] == document_ids
        assert [row.source_doc for row in rows] == ["first.pdf", "second.xlsx"]
        recovered_doc = db.get(RfpPackageDocument, document_ids[1])
        assert recovered_doc.structure_json["requirements_review"][
            "recovered_requirement_ids"
        ] == ["REQ-002"]


def test_review_outcome_never_says_no_issues_for_partial_coverage(monkeypatch) -> None:
    import app.jobs.intake as intake

    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        intake,
        "_set_stage",
        lambda _pid, message, **kwargs: messages.append(
            (message, kwargs.get("status", "completed"))
        ),
    )
    state = {
        "status": "partial",
        "extraction": {"recovered_item_count": 0},
        "classification": {
            "reviewed_count": 1,
            "total_count": 2,
            "primary_model": "gemini-2.5-pro",
            "fallback_model": "claude-haiku-4-5-20251001",
            "retry_used": True,
            "fallback_used": False,
            "flagged_for_review_count": 0,
            "blocked_correction_count": 0,
            "unresolved_requirement_ids": ["REQ-002"],
            "findings_count": 0,
        },
        "completeness": {
            "reviewed_units": 1,
            "source_units_total": 1,
            "retry_used": False,
            "fallback_used": False,
            "manual_review_candidate_count": 0,
            "uncertain_passage_count": 0,
            "unresolved_unit_labels": [],
            "candidate_count": 0,
        },
    }

    intake._record_requirements_review_outcome(7, "rfp.pdf", state)

    assert messages[0][1] == "failed"
    assert "1/2 items" in messages[0][0]
    assert "unresolved" in messages[0][0]
    assert "no issues" not in messages[0][0]


def test_actionable_fallback_finding_uses_human_review_status() -> None:
    import app.jobs.intake as intake
    from app.agents.compliance_completeness import ComplianceCompletenessReport
    from app.agents.compliance_validator import (
        ComplianceValidationReport,
        ValidationAttempt,
    )

    validation = ComplianceValidationReport(
        total_count=1,
        primary_model="gemini-2.5-pro",
        fallback_model="claude-haiku-4-5-20251001",
        reviewed_requirement_ids=["REQ-001"],
        attempts=[
            ValidationAttempt(
                model="claude-haiku-4-5-20251001",
                role="fallback",
                requirement_ids=("REQ-001",),
                depth=1,
                success=True,
            )
        ],
        flagged_for_review_count=1,
    )
    completeness = ComplianceCompletenessReport(
        source_units_total=0,
        primary_model="gemini-2.5-pro",
        fallback_model="claude-haiku-4-5-20251001",
        source_sha256="source",
        matrix_sha256="matrix",
    )

    state = intake._combined_review_state(
        document_id=7,
        extraction_model="claude-sonnet-4-6",
        initially_extracted=1,
        recovered_count=0,
        final_count=1,
        validation=validation,
        completeness=completeness,
    )

    assert state["status"] == "review_required"
    assert state["requires_manual_review"] is True


def test_incomplete_source_extraction_forces_partial_review_state() -> None:
    import app.jobs.intake as intake
    from app.agents.compliance_completeness import ComplianceCompletenessReport
    from app.agents.compliance_validator import ComplianceValidationReport

    validation = ComplianceValidationReport(
        total_count=1,
        primary_model="gemini-2.5-pro",
        fallback_model="claude-haiku-4-5-20251001",
        reviewed_requirement_ids=["REQ-001"],
    )
    completeness = ComplianceCompletenessReport(
        source_units_total=0,
        primary_model="gemini-2.5-pro",
        fallback_model="claude-haiku-4-5-20251001",
        source_sha256="source",
        matrix_sha256="matrix",
    )
    coverage = {
        "state": "partial",
        "complete": False,
        "source_chunks_total": 2,
        "source_chunks_completed": 1,
        "failed_chunk_count": 1,
        "failed_chunk_labels": ["chunk 2/2"],
        "incomplete_reasons": ["chunk_failed"],
    }

    state = intake._combined_review_state(
        document_id=7,
        extraction_model="claude-sonnet-4-6",
        initially_extracted=1,
        recovered_count=0,
        final_count=1,
        validation=validation,
        completeness=completeness,
        extraction_coverage=coverage,
    )

    assert state["status"] == "partial"
    assert state["requires_manual_review"] is True
    assert state["extraction"]["coverage"] == coverage


def test_cost_estimate_uses_configured_extractor_and_includes_review(
    monkeypatch,
) -> None:
    import app.services.cost_estimate as cost_estimate

    settings = SimpleNamespace(
        model_light_extraction="claude-haiku-4-5-20251001",
        model_compliance_matrix="claude-sonnet-4-6",
        model_compliance_validator="gemini-2.5-pro",
        model_compliance_validator_fallback="claude-haiku-4-5-20251001",
        model_drafter="claude-sonnet-4-6",
        model_writer_team_initial="claude-sonnet-4-6",
        model_reviewer_a="gpt-5.5",
        model_reviewer_b="gemini-2.5-pro",
        model_writer_team_pass_1_2="claude-sonnet-4-6",
    )
    monkeypatch.setattr(cost_estimate, "get_settings", lambda: settings)

    estimate = cost_estimate.estimate_pipeline_cost({"rfp.pdf": b"x" * 100_000})

    assert estimate.compliance_review > 0
    assert estimate.compliance_fallback_contingency > 0
    assert estimate.compliance_extraction_model == "claude-sonnet-4-6"
    assert estimate.compliance_review_model == "gemini-2.5-pro"
    assert estimate.intake_total > (
        estimate.intake_metadata + estimate.compliance_matrix + estimate.shortfall
    )


def test_progress_helpers_render_exact_provider_and_warning_state() -> None:
    from app.ui.pages import (
        _model_provider_label,
        _requirements_review_from_structure,
        _requirements_review_visual,
    )

    assert _model_provider_label("gemini-2.5-pro") == "Google"
    assert _model_provider_label("claude-haiku-4-5-20251001") == "Anthropic"
    assert _requirements_review_visual("degraded")[2] == (
        "Fallback used — human review needed"
    )
    assert _requirements_review_visual("partial")[2] == "Partial — review incomplete"
    assert _requirements_review_visual("pending")[2] == "Queued for review"
    assert _requirements_review_visual("not_applicable")[2] == (
        "Not a requirements source"
    )
    pending = _requirements_review_from_structure(
        {"requirements_review": {"status": "pending"}}
    )
    assert pending["status"] == "pending"
    assert _requirements_review_from_structure({}) == {}
    malformed = _requirements_review_from_structure(
        {"requirements_review": "invalid"}
    )
    assert malformed["status"] == "unknown"
    assert malformed["requires_manual_review"] is True

    malformed_nested = _requirements_review_from_structure(
        {
            "requirements_review": {
                "status": "complete",
                "extraction": {"coverage": "invalid"},
                "classification": {
                    "auto_applied_count": "not-a-number",
                    "manual_review": "invalid",
                },
                "completeness": "invalid",
                "known_review_cost_usd": "not-a-number",
            }
        }
    )
    assert malformed_nested["status"] == "unknown"
    assert malformed_nested["requires_manual_review"] is True
    assert malformed_nested["extraction"]["coverage"] == {}
    assert malformed_nested["classification"]["auto_applied_count"] == 0
    assert malformed_nested["classification"]["manual_review"] == []
    assert malformed_nested["completeness"] == {}
    assert malformed_nested["known_review_cost_usd"] == 0.0


def test_all_requirements_review_attempts_roll_up_to_compliance_cost() -> None:
    from app.services.cost_dashboard import stage_for_agent

    for agent_name in (
        "compliance_validator_retry",
        "compliance_validator_fallback",
        "compliance_completeness",
        "compliance_completeness_retry",
        "compliance_completeness_fallback",
    ):
        assert stage_for_agent(agent_name) == "Compliance Matrix"
