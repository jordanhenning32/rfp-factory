from __future__ import annotations

import pytest


@pytest.mark.parametrize("filename", ["bundle.zip", "scope.txt", "readme.md", "rates.csv"])
def test_proposal_creation_rejects_files_intake_cannot_parse(
    inmemory_db, monkeypatch, tmp_path, filename,
) -> None:
    import app.db.session as db_session
    import app.services.proposals as proposals
    from app.models import Proposal, RfpPackage

    storage_root = tmp_path / "rfp_packages"
    monkeypatch.setattr(proposals, "RFP_PACKAGES_DIR", storage_root)

    with db_session.SessionLocal() as db:
        with pytest.raises(ValueError, match="Unsupported RFP file type"):
            proposals.create_proposal_with_files(
                db,
                title="Unsupported input",
                files=[proposals.UploadedFile(filename=filename, content=b"test")],
            )
        assert db.query(Proposal).count() == 0
        assert db.query(RfpPackage).count() == 0
    assert not storage_root.exists()


def test_proposal_files_are_removed_when_creation_transaction_rolls_back(
    inmemory_db, monkeypatch, tmp_path,
) -> None:
    import app.db.session as db_session
    import app.services.proposals as proposals
    from app.models import Proposal, RfpPackage

    storage_root = tmp_path / "rfp_packages"
    monkeypatch.setattr(proposals, "RFP_PACKAGES_DIR", storage_root)

    with db_session.SessionLocal() as db:
        proposal = proposals.create_proposal_with_files(
            db,
            title="Rollback contract",
            files=[proposals.UploadedFile(filename="rfp.pdf", content=b"%PDF-test")],
        )
        package_dir = storage_root / str(proposal.rfp_package_id)
        assert package_dir.is_dir()
        assert (package_dir / "rfp.pdf").is_file()
        db.rollback()
        assert db.query(Proposal).count() == 0
        assert db.query(RfpPackage).count() == 0

    assert not package_dir.exists()


def test_proposal_files_survive_successful_creation_commit(
    inmemory_db, monkeypatch, tmp_path,
) -> None:
    import app.db.session as db_session
    import app.services.proposals as proposals

    storage_root = tmp_path / "rfp_packages"
    monkeypatch.setattr(proposals, "RFP_PACKAGES_DIR", storage_root)

    with db_session.SessionLocal() as db:
        proposal = proposals.create_proposal_with_files(
            db,
            title="Commit contract",
            files=[proposals.UploadedFile(filename="rfp.pdf", content=b"%PDF-test")],
        )
        package_dir = storage_root / str(proposal.rfp_package_id)
        db.commit()
        db.rollback()  # a later transaction cannot remove committed files

    assert (package_dir / "rfp.pdf").read_bytes() == b"%PDF-test"
