"""Re-classify and re-tag every existing KB document.

Use this when the classifier prompt changes, when new classes are added,
or after a batch upload that mis-tagged files (e.g., the pre-fix bug
where every doc in a batch shared the first file's tags).

Per doc:
1. Read extracted_text_md (already on the row from prior ingestion).
2. Call the classifier against that text.
3. Update document_class and tags_json on the row.
4. Delete pending profile suggestions for this doc and re-run fact
   extraction. Approved/rejected suggestions stay — they reflect prior
   user decisions and shouldn't be churned.
"""

from __future__ import annotations

import logging
import threading

from app.agents.kb_classify import classify_text
from app.agents.kb_facts import extract_profile_suggestions
from app.db.session import session_scope
from app.models import KnowledgeBaseDocument, ProfileSuggestion

log = logging.getLogger(__name__)

_PROGRESS: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "skipped": 0,
    "updated": 0,
    "started_at": None,
    "completed_at": None,
}


def get_progress() -> dict:
    """Read-only snapshot for the UI's polling timer."""
    return dict(_PROGRESS)


def _reset_progress() -> None:
    _PROGRESS.update(
        running=False,
        total=0,
        done=0,
        skipped=0,
        updated=0,
        started_at=None,
        completed_at=None,
    )


def reclassify_all_documents() -> dict:
    """Sync — runs in the spawn helper's thread."""
    from datetime import datetime

    _PROGRESS.update(
        running=True,
        total=0,
        done=0,
        skipped=0,
        updated=0,
        started_at=datetime.utcnow(),
        completed_at=None,
    )

    with session_scope() as db:
        doc_ids = [d.id for d in db.query(KnowledgeBaseDocument).all()]
    _PROGRESS["total"] = len(doc_ids)

    for doc_id in doc_ids:
        try:
            _reclassify_one(doc_id)
            _PROGRESS["updated"] += 1
        except Exception:
            log.exception("reclassify failed for doc %d", doc_id)
            _PROGRESS["skipped"] += 1
        _PROGRESS["done"] += 1

    _PROGRESS["running"] = False
    _PROGRESS["completed_at"] = datetime.utcnow()
    log.info(
        "reclassify_all_documents done: %d total, %d updated, %d skipped",
        _PROGRESS["total"],
        _PROGRESS["updated"],
        _PROGRESS["skipped"],
    )
    return dict(_PROGRESS)


def _reclassify_one(document_id: int) -> None:
    # Step 1: read text + filename inside one session, release before LLM call.
    with session_scope() as db:
        doc = db.get(KnowledgeBaseDocument, document_id)
        if doc is None:
            return
        text = doc.extracted_text_md or ""
        filename = doc.filename
    if not text.strip():
        log.info("reclassify: doc %d has no extracted text — skipping", document_id)
        raise ValueError("no_text")

    # Step 2: classify (LLM call).
    result = classify_text(filename=filename, text=text)
    if result is None:
        log.info("reclassify: doc %d classifier returned None — skipping", document_id)
        raise ValueError("classifier_failed")

    # Step 3: update class + tags. Drop existing pending suggestions so the
    # next fact-extraction pass can write a fresh set under the (possibly new)
    # class. Approved/rejected stay.
    with session_scope() as db:
        doc = db.get(KnowledgeBaseDocument, document_id)
        if doc is None:
            return
        doc.document_class = result.document_class
        doc.tags_json = result.tags
        db.query(ProfileSuggestion).filter(
            ProfileSuggestion.kb_document_id == document_id,
            ProfileSuggestion.status == "pending",
        ).delete(synchronize_session=False)

    # Step 4: re-run fact extraction. extract_profile_suggestions writes its
    # own session_scopes per ProfileSuggestion, no need to wrap.
    try:
        extract_profile_suggestions(
            document_id=document_id,
            document_class=result.document_class,
            text=text,
        )
    except Exception:
        log.exception("reclassify: fact extraction failed for doc %d", document_id)


def spawn_reclassify_all() -> threading.Thread | None:
    """Fire-and-forget. Returns None if a job is already in flight."""
    if _PROGRESS.get("running"):
        return None
    t = threading.Thread(
        target=reclassify_all_documents,
        name="kb-reclassify-all",
        daemon=True,
    )
    t.start()
    return t
