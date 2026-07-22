"""Destructive services must only remove files owned by the active workspace."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.enums import KbDocumentClass
from app.models import (
    KnowledgeBaseDocument,
    ProfileSuggestion,
    Proposal,
    RfpPackage,
    RfpPackageDocument,
)
from app.services import kb as kb_service
from app.services import proposals as proposal_service


def _create_proposal(db: Session, monkeypatch, root):
    monkeypatch.setattr(proposal_service, "RFP_PACKAGES_DIR", root)
    proposal = proposal_service.create_proposal_with_files(
        db,
        title="Deletion safety test",
        files=[proposal_service.UploadedFile("rfp.pdf", b"requirements")],
    )
    db.flush()
    package = db.get(RfpPackage, proposal.rfp_package_id)
    assert package is not None
    return proposal, package, root / str(package.id)


def _create_kb_document(db: Session, monkeypatch, root):
    monkeypatch.setattr(kb_service, "KB_DIR", root)
    [document] = kb_service.create_kb_documents(
        db,
        files=[kb_service.KbUploadedFile("evidence.txt", b"past performance")],
        document_class=KbDocumentClass.PAST_PERFORMANCE_WON,
    )
    db.flush()
    suggestion = ProfileSuggestion(
        kb_document_id=document.id,
        operation="set",
        section="test_section",
        proposed_value_json={"value": "test"},
        summary="Test suggestion",
    )
    db.add(suggestion)
    db.flush()
    return document, suggestion


def test_valid_proposal_deletion_removes_owned_directory(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "rfp_packages"
    with Session(inmemory_db) as db:
        proposal, package, package_dir = _create_proposal(db, monkeypatch, root)
        proposal_id, package_id = proposal.id, package.id
        assert package_dir.is_dir()

        result = proposal_service.delete_proposal(db, proposal_id)

        assert result["deleted"] is True
        assert db.get(Proposal, proposal_id) is None
        assert db.get(RfpPackage, package_id) is None
        assert not package_dir.exists()
        assert root.is_dir()


def test_proposal_deletion_rejects_outside_path_without_db_mutation(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "rfp_packages"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with Session(inmemory_db) as db:
        proposal, package, original_dir = _create_proposal(db, monkeypatch, root)
        proposal_id, package_id = proposal.id, package.id
        document_id = db.query(RfpPackageDocument.id).scalar()
        package.storage_dir = str(outside)
        db.flush()

        result = proposal_service.delete_proposal(db, proposal_id)

        assert result["deleted"] is False
        assert "unsafe RFP package storage path" in result["reason"]
        assert db.get(Proposal, proposal_id) is not None
        assert db.get(RfpPackage, package_id) is not None
        assert db.get(RfpPackageDocument, document_id) is not None
        assert original_dir.is_dir()
        assert marker.read_text(encoding="utf-8") == "keep"


def test_proposal_deletion_rejects_managed_root_without_db_mutation(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "rfp_packages"
    with Session(inmemory_db) as db:
        proposal, package, original_dir = _create_proposal(db, monkeypatch, root)
        proposal_id, package_id = proposal.id, package.id
        package.storage_dir = str(root)
        db.flush()

        result = proposal_service.delete_proposal(db, proposal_id)

        assert result["deleted"] is False
        assert "unsafe RFP package storage path" in result["reason"]
        assert db.get(Proposal, proposal_id) is not None
        assert db.get(RfpPackage, package_id) is not None
        assert original_dir.is_dir()
        assert root.is_dir()


def test_proposal_deletion_allows_missing_owned_directory(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "rfp_packages"
    with Session(inmemory_db) as db:
        proposal, package, package_dir = _create_proposal(db, monkeypatch, root)
        proposal_id, package_id = proposal.id, package.id
        shutil.rmtree(package_dir)

        result = proposal_service.delete_proposal(db, proposal_id)

        assert result["deleted"] is True
        assert db.get(Proposal, proposal_id) is None
        assert db.get(RfpPackage, package_id) is None


def test_valid_kb_deletion_removes_owned_file_and_rows(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "kb_documents"
    with Session(inmemory_db) as db:
        document, suggestion = _create_kb_document(db, monkeypatch, root)
        document_id, suggestion_id = document.id, suggestion.id
        storage_path = root / str(document.id) / "evidence.txt"
        assert storage_path.is_file()

        result = kb_service.delete_kb_document(db, document_id)

        assert result == {"deleted": True, "suggestions_removed": 1}
        assert db.get(KnowledgeBaseDocument, document_id) is None
        assert db.query(ProfileSuggestion).filter_by(id=suggestion_id).count() == 0
        assert not storage_path.exists()
        assert root.is_dir()


def test_kb_deletion_rejects_outside_path_without_db_mutation(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "kb_documents"
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    with Session(inmemory_db) as db:
        document, suggestion = _create_kb_document(db, monkeypatch, root)
        document_id, suggestion_id = document.id, suggestion.id
        original_path = root / str(document.id) / "evidence.txt"
        document.storage_path = str(outside)
        db.flush()

        result = kb_service.delete_kb_document(db, document_id)

        assert result["deleted"] is False
        assert "unsafe KB storage path" in result["reason"]
        assert db.get(KnowledgeBaseDocument, document_id) is not None
        assert db.get(ProfileSuggestion, suggestion_id) is not None
        assert original_path.is_file()
        assert outside.read_text(encoding="utf-8") == "keep"


def test_kb_deletion_rejects_managed_root_without_db_mutation(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "kb_documents"
    with Session(inmemory_db) as db:
        document, suggestion = _create_kb_document(db, monkeypatch, root)
        document_id, suggestion_id = document.id, suggestion.id
        document.storage_path = str(root)
        db.flush()

        result = kb_service.delete_kb_document(db, document_id)

        assert result["deleted"] is False
        assert "managed directory itself" in result["reason"]
        assert db.get(KnowledgeBaseDocument, document_id) is not None
        assert db.get(ProfileSuggestion, suggestion_id) is not None
        assert root.is_dir()


def test_kb_deletion_rejects_another_documents_owned_file(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "kb_documents"
    with Session(inmemory_db) as db:
        first, first_suggestion = _create_kb_document(db, monkeypatch, root)
        second, second_suggestion = _create_kb_document(db, monkeypatch, root)
        first_id, first_suggestion_id = first.id, first_suggestion.id
        second_id, second_suggestion_id = second.id, second_suggestion.id
        second_path = root / str(second.id) / "evidence.txt"
        first.storage_path = str(second_path)
        db.flush()

        result = kb_service.delete_kb_document(db, first_id)

        assert result["deleted"] is False
        assert "not inside the owned directory" in result["reason"]
        assert db.get(KnowledgeBaseDocument, first_id) is not None
        assert db.get(KnowledgeBaseDocument, second_id) is not None
        assert db.get(ProfileSuggestion, first_suggestion_id) is not None
        assert db.get(ProfileSuggestion, second_suggestion_id) is not None
        assert second_path.read_bytes() == b"past performance"


def test_kb_deletion_allows_missing_safe_file(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "kb_documents"
    with Session(inmemory_db) as db:
        document, suggestion = _create_kb_document(db, monkeypatch, root)
        document_id, suggestion_id = document.id, suggestion.id
        storage_path = root / str(document.id) / "evidence.txt"
        storage_path.unlink()
        storage_path.parent.rmdir()

        result = kb_service.delete_kb_document(db, document_id)

        assert result == {"deleted": True, "suggestions_removed": 1}
        assert db.get(KnowledgeBaseDocument, document_id) is None
        assert db.query(ProfileSuggestion).filter_by(id=suggestion_id).count() == 0
        assert root.is_dir()


def test_deletion_not_found_results_are_preserved(
    inmemory_db, monkeypatch, tmp_path
):
    monkeypatch.setattr(proposal_service, "RFP_PACKAGES_DIR", tmp_path / "rfp")
    monkeypatch.setattr(kb_service, "KB_DIR", tmp_path / "kb")
    with Session(inmemory_db) as db:
        assert proposal_service.delete_proposal(db, 99999) == {
            "deleted": False,
            "reason": "not_found",
        }
        assert kb_service.delete_kb_document(db, 99999) == {
            "deleted": False,
            "reason": "not_found",
        }


def test_wipe_only_removes_confirmed_orphan_directories_after_rejection(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "rfp_packages"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_marker = outside / "keep.txt"
    outside_marker.write_text("keep", encoding="utf-8")

    with Session(inmemory_db) as db:
        proposal, package, owned_dir = _create_proposal(db, monkeypatch, root)
        proposal_id, package_id = proposal.id, package.id
        package.storage_dir = str(outside)
        orphan_dir = root / "99999"
        orphan_dir.mkdir()
        (orphan_dir / "old.txt").write_text("old", encoding="utf-8")
        db.flush()

        result = proposal_service.wipe_all_test_data(db)

        assert result["proposals"] == 0
        assert result["files_deleted_dirs"] == 1
        assert db.get(Proposal, proposal_id) is not None
        assert db.get(RfpPackage, package_id) is not None
        assert owned_dir.is_dir()
        assert outside_marker.read_text(encoding="utf-8") == "keep"
        assert not orphan_dir.exists()


def test_proposal_deletion_purges_quarantine_only_after_commit(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "rfp_packages"
    with Session(inmemory_db) as db:
        proposal, package, package_dir = _create_proposal(db, monkeypatch, root)
        proposal_id, package_id = proposal.id, package.id
        db.commit()

        result = proposal_service.delete_proposal(db, proposal_id)

        quarantines = list(root.glob(f".delete-proposal-{package_id}-*"))
        assert result["deleted"] is True
        assert not package_dir.exists()
        assert len(quarantines) == 1
        assert (quarantines[0] / "rfp.pdf").read_bytes() == b"requirements"

        db.commit()

        assert db.get(Proposal, proposal_id) is None
        assert db.get(RfpPackage, package_id) is None
        assert not quarantines[0].exists()
        assert "cleanup_warning" not in result


def test_proposal_deletion_rollback_restores_original_directory(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "rfp_packages"
    with Session(inmemory_db) as db:
        proposal, package, package_dir = _create_proposal(db, monkeypatch, root)
        proposal_id, package_id = proposal.id, package.id
        db.commit()

        result = proposal_service.delete_proposal(db, proposal_id)
        assert not package_dir.exists()
        assert len(list(root.glob(f".delete-proposal-{package_id}-*"))) == 1

        db.rollback()

        assert result["deleted"] is False
        assert result["reason"] == "transaction_rolled_back"
        assert db.get(Proposal, proposal_id) is not None
        assert db.get(RfpPackage, package_id) is not None
        assert (package_dir / "rfp.pdf").read_bytes() == b"requirements"
        assert list(root.glob(f".delete-proposal-{package_id}-*")) == []


def test_proposal_deletion_staging_failure_leaves_db_and_live_files_unchanged(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "rfp_packages"
    with Session(inmemory_db) as db:
        proposal, package, package_dir = _create_proposal(db, monkeypatch, root)
        proposal_id, package_id = proposal.id, package.id
        document_id = db.query(RfpPackageDocument.id).scalar()
        db.commit()
        real_rename = Path.rename

        def fail_live_path_rename(self: Path, target: Path):
            if self == package_dir:
                raise PermissionError("simulated staging failure")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", fail_live_path_rename)

        result = proposal_service.delete_proposal(db, proposal_id)

        assert result["deleted"] is False
        assert "failed to stage RFP package storage" in result["reason"]
        assert db.get(Proposal, proposal_id) is not None
        assert db.get(RfpPackage, package_id) is not None
        assert db.get(RfpPackageDocument, document_id) is not None
        assert (package_dir / "rfp.pdf").read_bytes() == b"requirements"
        assert list(root.glob(f".delete-proposal-{package_id}-*")) == []


def test_proposal_post_commit_cleanup_failure_is_truthfully_quarantined(
    inmemory_db, monkeypatch, tmp_path, caplog
):
    root = tmp_path / "rfp_packages"
    with Session(inmemory_db) as db:
        proposal, package, package_dir = _create_proposal(db, monkeypatch, root)
        proposal_id, package_id = proposal.id, package.id
        db.commit()

        def fail_purge(_path: Path) -> None:
            raise PermissionError("simulated post-commit purge failure")

        monkeypatch.setattr(
            proposal_service,
            "_purge_quarantined_package",
            fail_purge,
        )
        with caplog.at_level(logging.ERROR):
            result = proposal_service.delete_proposal(db, proposal_id)
            quarantine = next(root.glob(f".delete-proposal-{package_id}-*"))
            db.commit()

        assert db.get(Proposal, proposal_id) is None
        assert db.get(RfpPackage, package_id) is None
        assert not package_dir.exists()
        assert quarantine.is_dir()
        assert (quarantine / "rfp.pdf").read_bytes() == b"requirements"
        assert result["deleted"] is True
        assert result["filesystem_cleanup"] == "quarantined"
        assert result["quarantine_path"] == str(quarantine)
        assert "remains safely quarantined" in result["cleanup_warning"]
        assert "database deletion committed" in caplog.text


def test_kb_deletion_purges_quarantine_only_after_commit(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "kb_documents"
    with Session(inmemory_db) as db:
        document, suggestion = _create_kb_document(db, monkeypatch, root)
        document_id, suggestion_id = document.id, suggestion.id
        storage_path = root / str(document_id) / "evidence.txt"
        db.commit()

        result = kb_service.delete_kb_document(db, document_id)

        quarantines = list(storage_path.parent.glob(f".delete-kb-{document_id}-*"))
        assert result == {"deleted": True, "suggestions_removed": 1}
        assert not storage_path.exists()
        assert len(quarantines) == 1
        assert quarantines[0].read_bytes() == b"past performance"

        db.commit()

        assert db.get(KnowledgeBaseDocument, document_id) is None
        assert db.get(ProfileSuggestion, suggestion_id) is None
        assert not quarantines[0].exists()
        assert not storage_path.parent.exists()


def test_kb_deletion_rollback_restores_original_file(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "kb_documents"
    with Session(inmemory_db) as db:
        document, suggestion = _create_kb_document(db, monkeypatch, root)
        document_id, suggestion_id = document.id, suggestion.id
        storage_path = root / str(document_id) / "evidence.txt"
        db.commit()

        result = kb_service.delete_kb_document(db, document_id)
        assert not storage_path.exists()
        assert len(list(storage_path.parent.glob(f".delete-kb-{document_id}-*"))) == 1

        db.rollback()

        assert result["deleted"] is False
        assert result["reason"] == "transaction_rolled_back"
        assert db.get(KnowledgeBaseDocument, document_id) is not None
        assert db.get(ProfileSuggestion, suggestion_id) is not None
        assert storage_path.read_bytes() == b"past performance"
        assert list(storage_path.parent.glob(f".delete-kb-{document_id}-*")) == []


def test_kb_deletion_staging_failure_leaves_db_and_live_file_unchanged(
    inmemory_db, monkeypatch, tmp_path
):
    root = tmp_path / "kb_documents"
    with Session(inmemory_db) as db:
        document, suggestion = _create_kb_document(db, monkeypatch, root)
        document_id, suggestion_id = document.id, suggestion.id
        storage_path = root / str(document_id) / "evidence.txt"
        db.commit()
        real_rename = Path.rename

        def fail_live_path_rename(self: Path, target: Path):
            if self == storage_path:
                raise PermissionError("simulated staging failure")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", fail_live_path_rename)

        result = kb_service.delete_kb_document(db, document_id)

        assert result["deleted"] is False
        assert "failed to stage KB document storage" in result["reason"]
        assert db.get(KnowledgeBaseDocument, document_id) is not None
        assert db.get(ProfileSuggestion, suggestion_id) is not None
        assert storage_path.read_bytes() == b"past performance"
        assert list(storage_path.parent.glob(f".delete-kb-{document_id}-*")) == []


def test_kb_post_commit_cleanup_failure_is_truthfully_quarantined(
    inmemory_db, monkeypatch, tmp_path, caplog
):
    root = tmp_path / "kb_documents"
    with Session(inmemory_db) as db:
        document, suggestion = _create_kb_document(db, monkeypatch, root)
        document_id, suggestion_id = document.id, suggestion.id
        storage_path = root / str(document_id) / "evidence.txt"
        db.commit()

        def fail_purge(_path: Path, **_kwargs) -> None:
            raise PermissionError("simulated post-commit purge failure")

        monkeypatch.setattr(kb_service, "_purge_quarantined_kb_file", fail_purge)
        with caplog.at_level(logging.ERROR):
            result = kb_service.delete_kb_document(db, document_id)
            quarantine = next(
                storage_path.parent.glob(f".delete-kb-{document_id}-*")
            )
            db.commit()

        assert db.get(KnowledgeBaseDocument, document_id) is None
        assert db.get(ProfileSuggestion, suggestion_id) is None
        assert not storage_path.exists()
        assert quarantine.read_bytes() == b"past performance"
        assert result["deleted"] is True
        assert result["filesystem_cleanup"] == "quarantined"
        assert result["quarantine_path"] == str(quarantine)
        assert "remains safely quarantined" in result["cleanup_warning"]
        assert "database deletion committed" in caplog.text
