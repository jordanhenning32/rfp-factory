from __future__ import annotations

from contextlib import contextmanager

import pytest


def _seed_kb_document(db_session) -> int:
    from app.models import KnowledgeBaseDocument

    with db_session.session_scope() as db:
        doc = KnowledgeBaseDocument(
            filename="capability.pdf",
            storage_path="memory://capability.pdf",
            document_class="corporate",
            status="pending",
        )
        db.add(doc)
        db.flush()
        return doc.id


@pytest.mark.parametrize(
    "extractor",
    [
        lambda _path: (_ for _ in ()).throw(ValueError("corrupt pdf")),
        lambda _path: ("   \n", 1),
    ],
)
def test_kb_extraction_failure_never_marks_document_active(
    inmemory_db, monkeypatch, extractor,
) -> None:
    import app.db.session as db_session
    import app.jobs.kb_ingest as ingest
    from app.core.enums import KbDocumentStatus
    from app.models import KnowledgeBaseDocument

    monkeypatch.setattr(ingest, "session_scope", db_session.session_scope)
    monkeypatch.setattr(ingest, "extract_text_for_path", extractor)
    monkeypatch.setattr(
        ingest,
        "extract_profile_suggestions",
        lambda **_kwargs: pytest.fail("fact extraction must not run without text"),
    )
    document_id = _seed_kb_document(db_session)

    ingest.ingest_kb_document(document_id)

    with db_session.session_scope() as db:
        doc = db.get(KnowledgeBaseDocument, document_id)
        assert doc.status == KbDocumentStatus.DEACTIVATED
        assert doc.extracted_text_md is None


def test_kb_success_marks_active_after_text_is_persisted(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.jobs.kb_ingest as ingest
    from app.core.enums import KbDocumentStatus
    from app.models import KnowledgeBaseDocument

    monkeypatch.setattr(ingest, "session_scope", db_session.session_scope)
    monkeypatch.setattr(
        ingest,
        "extract_text_for_path",
        lambda _path: ("Verified capability statement", 1),
    )
    fact_calls: list[dict] = []
    monkeypatch.setattr(
        ingest,
        "extract_profile_suggestions",
        lambda **kwargs: fact_calls.append(kwargs) or 0,
    )
    document_id = _seed_kb_document(db_session)

    ingest.ingest_kb_document(document_id)

    with db_session.session_scope() as db:
        doc = db.get(KnowledgeBaseDocument, document_id)
        assert doc.status == KbDocumentStatus.ACTIVE
        assert doc.extracted_text_md == "Verified capability statement"
    assert fact_calls and fact_calls[0]["document_id"] == document_id


def test_reclassify_progress_clears_running_after_top_level_failure(
    inmemory_db, monkeypatch,
) -> None:
    import app.jobs.kb_reclassify as reclassify

    @contextmanager
    def broken_session_scope():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    reclassify._reset_progress()
    monkeypatch.setattr(reclassify, "session_scope", broken_session_scope)

    with pytest.raises(RuntimeError, match="database unavailable"):
        reclassify.reclassify_all_documents()

    progress = reclassify.get_progress()
    assert progress["running"] is False
    assert progress["completed_at"] is not None
