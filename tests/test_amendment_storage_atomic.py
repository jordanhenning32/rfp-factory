from __future__ import annotations

from datetime import UTC, datetime

import pytest


def _seed_proposal(db_session, storage_root) -> tuple[int, int]:
    from app.models import Proposal, RfpPackage

    with db_session.SessionLocal() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="",
        )
        db.add(package)
        db.flush()
        package_dir = storage_root / str(package.id)
        package_dir.mkdir(parents=True)
        package.storage_dir = str(package_dir)
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Amendment storage test",
            status="awaiting_scope_signoff",
        )
        db.add(proposal)
        db.commit()
        return proposal.id, package.id


def test_amendment_file_is_removed_on_transaction_rollback(
    inmemory_db, monkeypatch, tmp_path,
) -> None:
    import app.db.session as db_session
    import app.services.amendments as amendments
    from app.services.proposals import UploadedFile

    storage_root = tmp_path / "rfp_packages"
    storage_root.mkdir()
    monkeypatch.setattr(amendments, "RFP_PACKAGES_DIR", storage_root)
    proposal_id, package_id = _seed_proposal(db_session, storage_root)

    with db_session.SessionLocal() as db:
        docs = amendments.attach_amendment_to_proposal(
            proposal_id=proposal_id,
            files=[UploadedFile(filename="amendment.pdf", content=b"%PDF-amendment")],
            document_role="amendment",
            sequence_number=1,
            db=db,
        )
        stored = storage_root / str(package_id) / "amendment.pdf"
        assert docs and stored.is_file()
        db.rollback()

    assert not stored.exists()


def test_amendment_rejects_unsupported_file_before_writing(
    inmemory_db, monkeypatch, tmp_path,
) -> None:
    import app.db.session as db_session
    import app.services.amendments as amendments
    from app.services.proposals import UploadedFile

    storage_root = tmp_path / "rfp_packages"
    storage_root.mkdir()
    monkeypatch.setattr(amendments, "RFP_PACKAGES_DIR", storage_root)
    proposal_id, package_id = _seed_proposal(db_session, storage_root)

    with db_session.SessionLocal() as db:
        with pytest.raises(ValueError, match="Unsupported amendment file type"):
            amendments.attach_amendment_to_proposal(
                proposal_id=proposal_id,
                files=[UploadedFile(filename="amendment.zip", content=b"zip")],
                document_role="amendment",
                sequence_number=1,
                db=db,
            )

    assert list((storage_root / str(package_id)).iterdir()) == []
