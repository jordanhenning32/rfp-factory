from __future__ import annotations

from datetime import UTC, datetime

import pytest


def _seed_proposal(db_session, *, extracted_text: str | None = None) -> tuple[int, int]:
    from app.core.enums import ProposalStatus
    from app.models import Proposal, RfpPackage, RfpPackageDocument

    with db_session.session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://intake-package",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Fail-closed intake",
            status=ProposalStatus.INTAKING,
        )
        db.add(proposal)
        db.flush()
        document = RfpPackageDocument(
            rfp_package_id=package.id,
            filename="rfp.pdf",
            storage_path="memory://rfp.pdf",
            extracted_text_md=extracted_text,
        )
        db.add(document)
        db.flush()
        return proposal.id, document.id


def test_marker_only_extraction_is_not_meaningful(inmemory_db) -> None:
    from app.jobs.intake import _has_meaningful_extracted_text

    assert not _has_meaningful_extracted_text(None)
    assert not _has_meaningful_extracted_text("--- Page 1 ---\n\n--- Page 2 ---")
    assert not _has_meaningful_extracted_text("--- Page 1 ---\n[Sheet: Scope]")
    assert _has_meaningful_extracted_text("--- Page 1 ---\nThe contractor shall deliver.")


def test_parse_documents_fails_when_every_document_is_empty(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.jobs.intake as intake

    monkeypatch.setattr(intake, "session_scope", db_session.session_scope)
    proposal_id, _ = _seed_proposal(db_session)
    monkeypatch.setattr(
        intake,
        "_extract_text_for_intake",
        lambda *_args: ("--- Page 1 ---\n", 1),
    )

    with pytest.raises(RuntimeError, match="could not parse any RFP document"):
        intake._parse_documents(proposal_id)


def test_parse_documents_counts_valid_text_already_saved_on_retry(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.jobs.intake as intake

    monkeypatch.setattr(intake, "session_scope", db_session.session_scope)
    proposal_id, _ = _seed_proposal(
        db_session,
        extracted_text="--- Page 1 ---\nThe contractor shall provide support.",
    )
    monkeypatch.setattr(
        intake,
        "_extract_text_for_intake",
        lambda *_args: pytest.fail("retry should reuse persisted extraction"),
    )

    assert intake._parse_documents(proposal_id) == 1


def test_partial_parse_failure_is_persisted_as_a_failed_source_review(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.jobs.intake as intake
    from app.models import Proposal, RfpPackageDocument

    monkeypatch.setattr(intake, "session_scope", db_session.session_scope)
    proposal_id, _ = _seed_proposal(
        db_session,
        extracted_text="--- Page 1 ---\nThe contractor shall provide support.",
    )
    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        failed_doc = RfpPackageDocument(
            rfp_package_id=proposal.rfp_package_id,
            filename="scanned.pdf",
            storage_path="memory://scanned.pdf",
        )
        db.add(failed_doc)
        db.flush()
        failed_doc_id = failed_doc.id

    monkeypatch.setattr(
        intake,
        "_extract_text_for_intake",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("sensitive raw error")),
    )

    assert intake._parse_documents(proposal_id) == 1
    with db_session.session_scope() as db:
        failed_doc = db.get(RfpPackageDocument, failed_doc_id)
        review = failed_doc.structure_json["requirements_review"]
        assert review["status"] == "failed"
        assert review["requires_manual_review"] is True
        assert "text-searchable file" in review["reason"]
        assert "sensitive raw error" not in review["reason"]


def test_intake_does_not_advance_when_compliance_is_empty(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.jobs.intake as intake
    from app.core.enums import ProposalStatus
    from app.models import Proposal

    monkeypatch.setattr(intake, "session_scope", db_session.session_scope)
    proposal_id, _ = _seed_proposal(
        db_session,
        extracted_text="--- Page 1 ---\nThe contractor shall provide support.",
    )
    stages: list[str] = []
    monkeypatch.setattr(
        intake,
        "_set_stage",
        lambda _pid, msg, **_kwargs: stages.append(msg),
    )
    monkeypatch.setattr(intake, "_detect_cots_orientation", lambda _pid: None)
    monkeypatch.setattr(intake, "_run_compliance_matrix", lambda _pid: 0)
    monkeypatch.setattr(
        intake,
        "_run_section_m_extractor",
        lambda _pid: pytest.fail("Section M must not run after an empty matrix"),
    )

    intake.run_intake_pipeline(proposal_id)

    with db_session.session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        assert proposal.status == ProposalStatus.INTAKING
    assert stages[-1].startswith("Pipeline failed")
