"""Knowledge Base ingestion + management.

Per design doc §7. Saves uploaded files under data/kb_documents/<id>/, extracts
text, persists a KnowledgeBaseDocument row with class metadata. Fact extraction
+ profile suggestions run as a follow-up pass (see app.jobs.kb_ingest).
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config import KB_DIR
from app.core.enums import KbDocumentClass, KbDocumentStatus
from app.models import KnowledgeBaseDocument, ProfileSuggestion
from app.services.deletion_quarantine import stage_path_for_transaction
from app.services.storage_safety import (
    UnsafeManagedPath,
    require_contained_file,
    require_owned_direct_child_directory,
)

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



def _register_kb_rollback_cleanup(db: Session, document_dir: Path) -> None:
    state = {"committed": False}

    def _after_commit(_session: Session) -> None:
        state["committed"] = True

    def _after_rollback(_session: Session) -> None:
        if state["committed"]:
            return
        try:
            safe_dir = require_owned_direct_child_directory(
                document_dir,
                root=KB_DIR,
                expected_name=document_dir.name,
                description="rolled-back KB document directory",
            )
            if safe_dir.exists():
                shutil.rmtree(safe_dir)
        except UnsafeManagedPath as exc:
            log.error("refusing KB rollback cleanup for %s: %s", document_dir, exc)
        except Exception:
            log.exception("failed KB rollback cleanup for %s", document_dir)

    event.listen(db, "after_commit", _after_commit, once=True)
    event.listen(db, "after_rollback", _after_rollback, once=True)


def _purge_quarantined_kb_file(path: Path, *, managed_root: Path) -> None:
    """Purge one staged KB file and remove its now-empty owned directory."""
    path.unlink()
    parent = path.parent
    resolved_root = managed_root.expanduser().resolve(strict=False)
    if (
        parent != resolved_root
        and parent.exists()
        and parent.is_dir()
        and not any(parent.iterdir())
    ):
        parent.rmdir()


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
        _register_kb_rollback_cleanup(db, doc_dir)
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

    # Validate the database-sourced path before deleting suggestions or the
    # document row. Missing files are safe to recover from, but paths outside
    # the active KB workspace (or the workspace root itself) fail closed.
    try:
        storage_path = require_contained_file(
            doc.storage_path,
            root=KB_DIR,
            description="KB storage path",
            expected_parent_name=str(doc.id),
        )
    except UnsafeManagedPath as exc:
        return {"deleted": False, "reason": str(exc)}

    result = {"deleted": True}
    quarantine_path = storage_path.with_name(
        f".delete-kb-{doc.id}-{uuid4().hex}"
    )
    managed_root = KB_DIR
    document_dir_name = str(doc.id)
    stage_error = stage_path_for_transaction(
        db,
        original_path=storage_path,
        quarantine_path=quarantine_path,
        validate_quarantine=lambda path: require_contained_file(
            path,
            root=managed_root,
            description="quarantined KB storage path",
            expected_parent_name=document_dir_name,
        ),
        purge_quarantine=lambda path: _purge_quarantined_kb_file(
            path,
            managed_root=managed_root,
        ),
        result=result,
        description="KB document storage",
        logger=log,
    )
    if stage_error is not None:
        return {"deleted": False, "reason": stage_error}

    # ProfileSuggestion has FK CASCADE on the doc; explicit cleanup belt-and-suspenders.
    suggestions = db.query(ProfileSuggestion).filter(ProfileSuggestion.kb_document_id == document_id).count()
    db.query(ProfileSuggestion).filter(ProfileSuggestion.kb_document_id == document_id).delete(
        synchronize_session=False
    )

    db.delete(doc)
    db.flush()
    result["suggestions_removed"] = suggestions
    return result
