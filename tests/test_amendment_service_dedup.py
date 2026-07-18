"""Test for the F1 SHA-256 dedup contract via the amendment service.

Mirrors the F1 dedup precedent (find_duplicate_rfp_documents) — when we
upload an amendment file once via attach_amendment_to_proposal, the same
file's content should be detected as a duplicate on the next dedup check.
"""

from __future__ import annotations

from datetime import UTC, datetime


def test_attach_amendment_then_find_duplicate_detects_the_doc(tmp_path, monkeypatch, inmemory_db):
    """attach_amendment_to_proposal writes the file + persists the row;
    a subsequent find_duplicate_rfp_documents() finds it via SHA-256."""
    from app.core.enums import ProposalRole, ProposalStatus
    from app.db.session import session_scope
    from app.models import Proposal, RfpPackage
    from app.services.amendments import attach_amendment_to_proposal
    from app.services.proposals import (
        UploadedFile,
        find_duplicate_rfp_documents,
    )

    # Redirect the on-disk RFP_PACKAGES_DIR to a tmp path so we don't
    # write into the real data/rfp_packages/ tree during the test.
    monkeypatch.setattr(
        "app.services.amendments.RFP_PACKAGES_DIR",
        tmp_path / "rfp_packages",
    )

    with session_scope() as db:
        pkg = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir=str(tmp_path / "rfp_packages"),
        )
        db.add(pkg)
        db.flush()

        proposal = Proposal(
            rfp_package_id=pkg.id,
            title="Dedup Test",
            role=ProposalRole.PRIME,
            status=ProposalStatus.INTAKING,
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id

    uf = UploadedFile(
        filename="amendment.pdf",
        content=b"%PDF-1.4 amendment content here",
    )

    with session_scope() as db:
        new_docs = attach_amendment_to_proposal(
            proposal_id=proposal_id,
            files=[uf],
            document_role="amendment",
            sequence_number=1,
            db=db,
        )
        n_created = len(new_docs)

    assert n_created == 1

    # Now run the F1 dedup check against the same UploadedFile bytes —
    # it should detect the existing doc by SHA-256.
    with session_scope() as db:
        matches = find_duplicate_rfp_documents(db, [uf])
        assert "amendment.pdf" in matches
        matched_doc = matches["amendment.pdf"]
        # Snapshot ORM attrs inside the session — accessing them after
        # session_scope exits raises DetachedInstanceError.
        assert matched_doc.filename == "amendment.pdf"
        assert matched_doc.document_role == "amendment"
        assert matched_doc.sequence_number == 1
        # structure_json carries the SHA-256 — that's the contract
        assert matched_doc.structure_json is not None
        assert "content_sha256" in matched_doc.structure_json


def test_attach_qa_response_forces_sequence_number_none(tmp_path, monkeypatch, inmemory_db):
    """document_role='qa_response' silently nulls sequence_number even if
    the caller passed one."""
    from app.core.enums import ProposalRole, ProposalStatus
    from app.db.session import session_scope
    from app.models import Proposal, RfpPackage
    from app.services.amendments import attach_amendment_to_proposal
    from app.services.proposals import UploadedFile

    monkeypatch.setattr(
        "app.services.amendments.RFP_PACKAGES_DIR",
        tmp_path / "rfp_packages",
    )

    with session_scope() as db:
        pkg = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir=str(tmp_path / "rfp_packages"),
        )
        db.add(pkg)
        db.flush()

        proposal = Proposal(
            rfp_package_id=pkg.id,
            title="QA Test",
            role=ProposalRole.PRIME,
            status=ProposalStatus.INTAKING,
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id

    uf = UploadedFile(filename="qa_answers.pdf", content=b"qa pdf content")

    with session_scope() as db:
        new_docs = attach_amendment_to_proposal(
            proposal_id=proposal_id,
            files=[uf],
            document_role="qa_response",
            sequence_number=42,  # caller passed a number, but Q&A should null it
            db=db,
        )
        # Snapshot ORM attrs while we're still in the session.
        n_created = len(new_docs)
        role = new_docs[0].document_role
        seq = new_docs[0].sequence_number

    assert n_created == 1
    assert role == "qa_response"
    assert seq is None
