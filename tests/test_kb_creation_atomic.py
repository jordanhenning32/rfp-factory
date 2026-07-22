from __future__ import annotations


def test_kb_files_are_removed_when_creation_transaction_rolls_back(
    inmemory_db, monkeypatch, tmp_path,
) -> None:
    import app.db.session as db_session
    import app.services.kb as kb
    from app.models import KnowledgeBaseDocument

    storage_root = tmp_path / "kb_documents"
    monkeypatch.setattr(kb, "KB_DIR", storage_root)

    with db_session.SessionLocal() as db:
        docs = kb.create_kb_documents(
            db,
            files=[kb.KbUploadedFile(filename="capability.pdf", content=b"%PDF-test")],
            document_class="corporate",
        )
        doc_dir = storage_root / str(docs[0].id)
        assert (doc_dir / "capability.pdf").is_file()
        db.rollback()
        assert db.query(KnowledgeBaseDocument).count() == 0

    assert not doc_dir.exists()


def test_kb_files_survive_successful_creation_commit(
    inmemory_db, monkeypatch, tmp_path,
) -> None:
    import app.db.session as db_session
    import app.services.kb as kb

    storage_root = tmp_path / "kb_documents"
    monkeypatch.setattr(kb, "KB_DIR", storage_root)

    with db_session.SessionLocal() as db:
        docs = kb.create_kb_documents(
            db,
            files=[kb.KbUploadedFile(filename="capability.pdf", content=b"%PDF-test")],
            document_class="corporate",
        )
        stored = storage_root / str(docs[0].id) / "capability.pdf"
        db.commit()
        db.rollback()

    assert stored.read_bytes() == b"%PDF-test"
