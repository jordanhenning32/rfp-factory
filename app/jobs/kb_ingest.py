"""KB ingestion pipeline.

Per uploaded KB document:
  1. Extract text (pdfplumber for PDFs; .txt/.md read as-is; others fall through).
  2. Persist extracted text to KnowledgeBaseDocument.extracted_text_md.
  3. Status flips PENDING -> ACTIVE on success.
  4. Run fact extraction (Phase B) — emits ProfileSuggestion rows.

Runs in a daemon thread spawned from the upload UI handler.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from app.agents.kb_facts import extract_profile_suggestions
from app.core.enums import KbDocumentStatus
from app.db.session import session_scope
from app.models import KnowledgeBaseDocument
from app.services.pdf_extract import extract_text_for_path

log = logging.getLogger(__name__)


def ingest_kb_document(document_id: int) -> None:
    """Full ingestion pipeline for one KB document. Sync; runs in a thread."""
    log.info("kb ingestion starting for document %d", document_id)

    # Stage 1: extract text + persist; capture class for fact-extraction.
    document_class = None
    with session_scope() as db:
        doc = db.get(KnowledgeBaseDocument, document_id)
        if doc is None:
            log.warning("kb ingestion: document %d not found", document_id)
            return
        path = Path(doc.storage_path)
        document_class = doc.document_class
    try:
        text, _ = extract_text_for_path(path)
    except Exception:
        log.exception("kb ingestion: text extraction failed for doc %d", document_id)
        text = ""

    with session_scope() as db:
        doc = db.get(KnowledgeBaseDocument, document_id)
        if doc is None:
            return
        doc.extracted_text_md = text or None
        # Even with empty text, mark active — user can add tags/notes manually.
        doc.status = KbDocumentStatus.ACTIVE

    log.info("kb ingestion: document %d text=%d chars", document_id, len(text or ""))

    # Stage 2: fact extraction → profile suggestions.
    if text and document_class is not None:
        try:
            n = extract_profile_suggestions(
                document_id=document_id,
                document_class=document_class,
                text=text,
            )
            log.info("kb ingestion: document %d generated %d profile suggestion(s)", document_id, n)
        except Exception:
            log.exception("kb ingestion: fact extraction failed for doc %d", document_id)

    log.info("kb ingestion complete for document %d", document_id)


def spawn_kb_ingest(document_id: int) -> threading.Thread:
    t = threading.Thread(
        target=ingest_kb_document,
        args=(document_id,),
        name=f"kb-ingest-{document_id}",
        daemon=True,
    )
    t.start()
    return t
