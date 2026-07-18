"""Knowledge Base ingestion + management.

Per design doc §7. Saves uploaded files under data/kb_documents/<id>/, extracts
text, persists a KnowledgeBaseDocument row with class metadata. Fact extraction
+ profile suggestions run as a follow-up pass (see app.jobs.kb_ingest).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import KB_DIR
from app.core.enums import KbDocumentClass, KbDocumentStatus
from app.models import KnowledgeBaseDocument, ProfileSuggestion

log = logging.getLogger(__name__)

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
ALLOWED_KB_EXTENSIONS = {".csv", ".docx", ".markdown", ".md", ".pdf", ".txt", ".xlsx"}
MAX_KB_FILE_BYTES = 50 * 1024 * 1024
MAX_KB_BATCH_BYTES = 200 * 1024 * 1024


@dataclass
class KbUploadedFile:
    filename: str
    content: bytes
    # Optional per-file overrides set by the upload UI's auto-classifier.
    document_class: KbDocumentClass | None = None
    tags: list[str] | None = None


def _safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = _SAFE_FILENAME.sub("_", base).strip("._")
    return cleaned or "file"


def _normalize_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = [t.strip() for t in raw.split(",")]
    return [t for t in parts if t]


def _content_hash(data: bytes) -> str:
    """SHA-256 of the raw file bytes, hex digest. Used to detect duplicate
    KB uploads."""
    return hashlib.sha256(data).hexdigest()


def _validate_upload_sizes(files: list[KbUploadedFile]) -> None:
    total = 0
    for f in files:
        size = len(f.content)
        if size > MAX_KB_FILE_BYTES:
            raise ValueError(
                f"{f.filename} is too large "
                f"({size / 1024 / 1024:.1f} MB; max {MAX_KB_FILE_BYTES / 1024 / 1024:.0f} MB)."
            )
        total += size
    if total > MAX_KB_BATCH_BYTES:
        raise ValueError(
            "KB upload batch is too large "
            f"({total / 1024 / 1024:.1f} MB; max {MAX_KB_BATCH_BYTES / 1024 / 1024:.0f} MB)."
        )


def _validate_upload_extensions(files: list[KbUploadedFile]) -> None:
    allowed = ", ".join(sorted(ALLOWED_KB_EXTENSIONS))
    for f in files:
        suffix = Path(f.filename).suffix.lower()
        if suffix not in ALLOWED_KB_EXTENSIONS:
            raise ValueError(f"{f.filename} has unsupported file type {suffix or '(none)'}; allowed: {allowed}.")


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def find_duplicate_documents(
    db: Session, files: Iterable[KbUploadedFile]
) -> dict[str, KnowledgeBaseDocument]:
    """For each file, return the existing KnowledgeBaseDocument that has the
    same content hash, if any. Keyed by uploaded filename. Files with no match
    don't appear in the dict.

    Reads metadata_json.content_sha256 — populated for every doc saved after
    the duplicate-detection feature shipped. Older docs without a hash are
    silently ignored (they get hashed on next reclassify pass).
    """
    file_list = list(files)
    if not file_list:
        return {}
    _validate_upload_extensions(file_list)
    _validate_upload_sizes(file_list)
    hashes_to_files: dict[str, list[str]] = {}
    for f in file_list:
        hashes_to_files.setdefault(_content_hash(f.content), []).append(f.filename)
    existing = db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.metadata_json.is_not(None)).all()
    matches: dict[str, KnowledgeBaseDocument] = {}
    for doc in existing:
        h = (doc.metadata_json or {}).get("content_sha256")
        if h and h in hashes_to_files:
            for filename in hashes_to_files[h]:
                matches[filename] = doc
    return matches


def create_kb_documents(
    db: Session,
    *,
    files: Iterable[KbUploadedFile],
    document_class: KbDocumentClass | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    status: KbDocumentStatus = KbDocumentStatus.PENDING,
) -> list[KnowledgeBaseDocument]:
    """Save uploaded files to disk and create one row per file.

    Each file can carry its own document_class and tags via the KbUploadedFile
    fields — set by the per-file auto-classifier in the upload UI. The
    function-level `document_class` and `tags` are applied as fallbacks when
    a file has no per-file value, and `tags` is also merged on top of any
    per-file tags (de-duplicated, case-insensitive).

    Caller is responsible for committing. Text extraction happens in a
    separate pipeline step so this returns fast.
    """
    file_list = list(files)
    if not file_list:
        raise ValueError("At least one file is required.")
    _validate_upload_extensions(file_list)
    _validate_upload_sizes(file_list)

    documents: list[KnowledgeBaseDocument] = []
    extra_tags = tags or []
    for f in file_list:
        # Per-file class wins; otherwise function-level fallback.
        cls = f.document_class or document_class
        if cls is None:
            raise ValueError(
                f"No document_class for {f.filename}: classifier failed and no fallback class provided."
            )

        # Merge per-file tags + function-level extras, de-duped (case-insensitive).
        merged: list[str] = []
        seen: set[str] = set()
        for t in (f.tags or []) + list(extra_tags):
            key = t.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(t.strip())

        # Stash the content hash so future uploads can detect duplicates.
        doc_metadata = dict(metadata or {})
        doc_metadata["content_sha256"] = _content_hash(f.content)

        doc = KnowledgeBaseDocument(
            filename=f.filename,
            storage_path="",  # backfilled below
            document_class=cls,
            tags_json=merged,
            status=status,
            metadata_json=doc_metadata,
        )
        db.add(doc)
        db.flush()

        doc_dir = KB_DIR / str(doc.id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        path = doc_dir / _safe_filename(f.filename)
        path.write_bytes(f.content)
        doc.storage_path = str(path)

        documents.append(doc)
    return documents


def delete_kb_document(db: Session, document_id: int) -> dict:
    """Delete a KB document, its chunks, its profile suggestions, and the on-disk file."""
    doc = db.get(KnowledgeBaseDocument, document_id)
    if doc is None:
        return {"deleted": False, "reason": "not_found"}

    # ProfileSuggestion has FK CASCADE on the doc; explicit cleanup belt-and-suspenders.
    suggestions = db.query(ProfileSuggestion).filter(ProfileSuggestion.kb_document_id == document_id).count()
    db.query(ProfileSuggestion).filter(ProfileSuggestion.kb_document_id == document_id).delete(
        synchronize_session=False
    )

    storage_path = Path(doc.storage_path) if doc.storage_path else None

    db.delete(doc)
    db.flush()

    # Filesystem
    if storage_path:
        try:
            expected_dir = (KB_DIR / str(document_id)).resolve()
            resolved_storage_path = storage_path.resolve()
            if not _path_is_within(storage_path, KB_DIR) or not _path_is_within(storage_path, expected_dir):
                log.error(
                    "refusing to remove unexpected KB storage path for document %s: %s (expected under %s)",
                    document_id,
                    storage_path,
                    expected_dir,
                )
                return {"deleted": True, "suggestions_removed": suggestions}
            if resolved_storage_path.exists() and resolved_storage_path.is_file():
                resolved_storage_path.unlink()
            if expected_dir.exists() and expected_dir.is_dir() and not any(expected_dir.iterdir()):
                expected_dir.rmdir()
        except Exception:
            log.exception("failed to clean up KB file %s", storage_path)

    return {"deleted": True, "suggestions_removed": suggestions}
