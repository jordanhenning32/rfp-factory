from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.core.enums import KbDocumentClass
from app.models import KnowledgeBaseDocument, RfpPackage, RfpPackageDocument
from app.services import kb as kb_service
from app.services import proposals as proposal_service
from app.services.kb import KbUploadedFile, create_kb_documents, find_duplicate_documents
from app.services.proposals import (
    UploadedFile,
    create_proposal_with_files,
    delete_proposal,
    find_duplicate_rfp_documents,
)


def test_delete_proposal_refuses_rfp_package_root(tmp_path, monkeypatch, inmemory_db):
    root = tmp_path / "rfp_packages"
    root.mkdir()
    monkeypatch.setattr(proposal_service, "RFP_PACKAGES_DIR", root)

    from app.db.session import SessionLocal

    db = SessionLocal()
    proposal = create_proposal_with_files(
        db,
        title="Valid RFP",
        files=[UploadedFile(filename="rfp.pdf", content=b"%PDF-1.4")],
    )
    db.commit()

    package = db.get(RfpPackage, proposal.rfp_package_id)
    keep = root / "unrelated" / "keep.txt"
    keep.parent.mkdir()
    keep.write_text("keep")
    package.storage_dir = str(root)
    db.commit()

    delete_proposal(db, proposal.id)
    db.commit()

    assert keep.exists()


def test_create_proposal_rejects_unsupported_extension_before_rows(inmemory_db):
    from app.db.session import SessionLocal

    db = SessionLocal()

    try:
        create_proposal_with_files(
            db,
            title="Bad RFP",
            files=[UploadedFile(filename="payload.exe", content=b"MZ")],
        )
    except ValueError as exc:
        assert "unsupported file type" in str(exc)
    else:
        raise AssertionError("expected unsupported proposal extension to be rejected")

    assert db.query(RfpPackage).count() == 0


def test_create_kb_documents_rejects_oversized_file_before_rows(monkeypatch, inmemory_db):
    monkeypatch.setattr(kb_service, "MAX_KB_FILE_BYTES", 5)
    monkeypatch.setattr(kb_service, "MAX_KB_BATCH_BYTES", 8)
    from app.db.session import SessionLocal

    db = SessionLocal()

    try:
        create_kb_documents(
            db,
            files=[
                KbUploadedFile(
                    filename="large.pdf",
                    content=b"123456",
                    document_class=KbDocumentClass.CORPORATE,
                )
            ],
        )
    except ValueError as exc:
        assert "too large" in str(exc)
    else:
        raise AssertionError("expected oversized KB upload to be rejected")

    assert db.query(KnowledgeBaseDocument).count() == 0


def test_kb_duplicate_check_rejects_oversized_batch_before_hashing(monkeypatch, inmemory_db):
    monkeypatch.setattr(kb_service, "MAX_KB_FILE_BYTES", 10)
    monkeypatch.setattr(kb_service, "MAX_KB_BATCH_BYTES", 8)
    from app.db.session import SessionLocal

    db = SessionLocal()

    try:
        find_duplicate_documents(
            db,
            [
                KbUploadedFile(filename="a.pdf", content=b"1234"),
                KbUploadedFile(filename="b.pdf", content=b"56789"),
            ],
        )
    except ValueError as exc:
        assert "batch is too large" in str(exc)
    else:
        raise AssertionError("expected oversized KB batch to be rejected")


def test_delete_kb_document_refuses_other_document_file(tmp_path, monkeypatch, inmemory_db):
    root = tmp_path / "kb_documents"
    root.mkdir()
    monkeypatch.setattr(kb_service, "KB_DIR", root)
    from app.db.session import SessionLocal

    db = SessionLocal()
    doc1, doc2 = create_kb_documents(
        db,
        files=[
            KbUploadedFile(
                filename="one.pdf",
                content=b"one",
                document_class=KbDocumentClass.CORPORATE,
            ),
            KbUploadedFile(
                filename="two.pdf",
                content=b"two",
                document_class=KbDocumentClass.CORPORATE,
            ),
        ],
    )
    db.commit()

    doc2_path = doc2.storage_path
    doc1.storage_path = doc2_path
    db.commit()

    kb_service.delete_kb_document(db, doc1.id)
    db.commit()

    assert Path(doc2_path).exists()
    assert db.get(KnowledgeBaseDocument, doc2.id) is not None


def test_find_duplicate_rfp_documents_rejects_oversized_file_before_hashing(monkeypatch, inmemory_db):
    monkeypatch.setattr(proposal_service, "MAX_PROPOSAL_FILE_BYTES", 5)
    monkeypatch.setattr(proposal_service, "MAX_PROPOSAL_PACKAGE_BYTES", 8)
    from app.db.session import SessionLocal

    db = SessionLocal()

    try:
        find_duplicate_rfp_documents(
            db,
            [UploadedFile(filename="large.pdf", content=b"123456")],
        )
    except ValueError as exc:
        assert "too large" in str(exc)
    else:
        raise AssertionError("expected oversized proposal duplicate-check upload to be rejected")


def test_find_duplicate_rfp_documents_returns_all_staged_filenames_for_same_hash(tmp_path, monkeypatch, inmemory_db):
    monkeypatch.setattr(proposal_service, "RFP_PACKAGES_DIR", tmp_path / "rfp_packages")
    from app.db.session import SessionLocal

    db = SessionLocal()
    existing = create_proposal_with_files(
        db,
        title="Existing RFP",
        files=[UploadedFile(filename="existing.pdf", content=b"same bytes")],
    )
    db.commit()

    existing_doc = db.query(RfpPackageDocument).filter_by(rfp_package_id=existing.rfp_package_id).one()
    matches = find_duplicate_rfp_documents(
        db,
        [
            UploadedFile(filename="a.pdf", content=b"same bytes"),
            UploadedFile(filename="b.pdf", content=b"same bytes"),
        ],
    )

    assert set(matches) == {"a.pdf", "b.pdf"}
    assert {doc.id for doc in matches.values()} == {existing_doc.id}


def test_create_kb_documents_rejects_unsupported_extension_before_rows(inmemory_db):
    from app.db.session import SessionLocal

    db = SessionLocal()

    try:
        create_kb_documents(
            db,
            files=[
                KbUploadedFile(
                    filename="payload.exe",
                    content=b"MZ",
                    document_class=KbDocumentClass.CORPORATE,
                )
            ],
        )
    except ValueError as exc:
        assert "unsupported file type" in str(exc)
    else:
        raise AssertionError("expected unsupported KB extension to be rejected")

    assert db.query(KnowledgeBaseDocument).count() == 0


def test_find_duplicate_documents_returns_all_staged_filenames_for_same_hash(tmp_path, monkeypatch, inmemory_db):
    monkeypatch.setattr(kb_service, "KB_DIR", tmp_path / "kb_documents")
    from app.db.session import SessionLocal

    db = SessionLocal()
    (existing_doc,) = create_kb_documents(
        db,
        files=[
            KbUploadedFile(
                filename="existing.pdf",
                content=b"same bytes",
                document_class=KbDocumentClass.CORPORATE,
            )
        ],
    )
    db.commit()

    matches = find_duplicate_documents(
        db,
        [
            KbUploadedFile(filename="a.pdf", content=b"same bytes"),
            KbUploadedFile(filename="b.pdf", content=b"same bytes"),
        ],
    )

    assert set(matches) == {"a.pdf", "b.pdf"}
    assert {doc.id for doc in matches.values()} == {existing_doc.id}


def test_attach_amendment_rejects_unsupported_extension_before_rows_or_files(tmp_path, monkeypatch, inmemory_db):
    monkeypatch.setattr("app.services.amendments.RFP_PACKAGES_DIR", tmp_path / "rfp_packages")
    from app.core.enums import ProposalRole, ProposalStatus
    from app.db.session import SessionLocal
    from app.models import Proposal
    from app.services.amendments import attach_amendment_to_proposal

    db = SessionLocal()
    pkg = RfpPackage(uploaded_by="pytest", uploaded_at=datetime.now(UTC), storage_dir="")
    db.add(pkg)
    db.flush()
    proposal = Proposal(
        rfp_package_id=pkg.id,
        title="Amendment Safety",
        role=ProposalRole.PRIME,
        status=ProposalStatus.INTAKING,
    )
    db.add(proposal)
    db.flush()

    try:
        attach_amendment_to_proposal(
            proposal_id=proposal.id,
            files=[UploadedFile(filename="payload.exe", content=b"MZ")],
            document_role="amendment",
            sequence_number=1,
            db=db,
        )
    except ValueError as exc:
        assert "unsupported file type" in str(exc)
    else:
        raise AssertionError("expected unsupported amendment extension to be rejected")

    assert db.query(RfpPackageDocument).count() == 0
    assert not (tmp_path / "rfp_packages" / str(pkg.id)).exists()


def test_attach_amendment_rejects_oversized_file_before_rows_or_files(
    tmp_path, monkeypatch, inmemory_db
):
    monkeypatch.setattr("app.services.amendments.RFP_PACKAGES_DIR", tmp_path / "rfp_packages")
    monkeypatch.setattr(proposal_service, "MAX_PROPOSAL_FILE_BYTES", 5)
    monkeypatch.setattr(proposal_service, "MAX_PROPOSAL_PACKAGE_BYTES", 8)
    from app.core.enums import ProposalRole, ProposalStatus
    from app.db.session import SessionLocal
    from app.models import Proposal
    from app.services.amendments import attach_amendment_to_proposal

    db = SessionLocal()
    pkg = RfpPackage(uploaded_by="pytest", uploaded_at=datetime.now(UTC), storage_dir="")
    db.add(pkg)
    db.flush()
    proposal = Proposal(
        rfp_package_id=pkg.id,
        title="Amendment Safety",
        role=ProposalRole.PRIME,
        status=ProposalStatus.INTAKING,
    )
    db.add(proposal)
    db.flush()

    try:
        attach_amendment_to_proposal(
            proposal_id=proposal.id,
            files=[UploadedFile(filename="large.pdf", content=b"123456")],
            document_role="qa_response",
            sequence_number=None,
            db=db,
        )
    except ValueError as exc:
        assert "too large" in str(exc)
    else:
        raise AssertionError("expected oversized amendment upload to be rejected")

    assert db.query(RfpPackageDocument).count() == 0
    assert not (tmp_path / "rfp_packages" / str(pkg.id)).exists()


def test_find_duplicate_rfp_documents_rejects_unsupported_extension_before_hashing(monkeypatch, inmemory_db):
    def fail_hash(_content: bytes) -> str:
        raise AssertionError("unsupported duplicate-check upload should not be hashed")

    monkeypatch.setattr(proposal_service, "_content_hash", fail_hash)
    from app.db.session import SessionLocal

    db = SessionLocal()

    try:
        find_duplicate_rfp_documents(
            db,
            [UploadedFile(filename="payload.exe", content=b"MZ")],
        )
    except ValueError as exc:
        assert "unsupported file type" in str(exc)
    else:
        raise AssertionError("expected unsupported proposal duplicate-check upload to be rejected")
