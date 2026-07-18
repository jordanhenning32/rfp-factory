"""Amendment + Q&A ingestion service.

Four public exports:
  - AmendmentApplyReport (dataclass): counters + lists describing what
    changed when a delta was applied to a proposal's compliance matrix.
  - attach_amendment_to_proposal(...): writes uploaded files under the
    proposal's RFP package directory and persists RfpPackageDocument rows
    tagged with `document_role` + `sequence_number`. Mirrors the storage
    layout of `create_proposal_with_files` from app/services/proposals.py.
  - list_amendments(...): timeline of every non-original document on the
    proposal, with the latest AmendmentRun status attached.
  - apply_amendment_delta(...): mutates ComplianceMatrixItem rows + flags
    ProposalSection rows with `compliance_drift_pending=True` based on the
    delta the compliance-matrix agent returned. Returns a populated
    AmendmentApplyReport for persistence on AmendmentRun.report_json.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.agents.compliance_matrix import (
    ComplianceExtractionResult,
)
from app.config import RFP_PACKAGES_DIR
from app.core.enums import ComplianceStatus, RequirementCategory, RequirementType
from app.models import (
    AmendmentRun,
    ComplianceMatrixItem,
    Proposal,
    ProposalSection,
    RfpPackageDocument,
)
from app.services.proposals import (
    UploadedFile,
    _content_hash,
    _safe_filename,
    _validate_upload_extensions,
    _validate_upload_sizes,
)

log = logging.getLogger(__name__)


_REQ_ID_NUM_RE = re.compile(r"REQ-(\d+)")


@dataclass
class AmendmentApplyReport:
    """Counts + summary of one amendment delta application.

    Persisted to `amendment_runs.report_json` via `as_dict()` so the UI
    timeline can render the totals without re-reading every row.
    """

    n_new: int = 0
    n_modified: int = 0
    n_removed: int = 0
    sections_marked_stale: list[str] = field(default_factory=list)
    due_date_changed: bool = False
    page_limit_changes: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        """JSON-serializable dict for AmendmentRun.report_json persistence."""
        return {
            "n_new": self.n_new,
            "n_modified": self.n_modified,
            "n_removed": self.n_removed,
            "sections_marked_stale": list(self.sections_marked_stale),
            "due_date_changed": bool(self.due_date_changed),
            "page_limit_changes": list(self.page_limit_changes),
        }


def attach_amendment_to_proposal(
    *,
    proposal_id: int,
    files: Iterable[UploadedFile],
    document_role: str,
    sequence_number: int | None,
    db: Session,
) -> list[RfpPackageDocument]:
    """Persist amendment / Q&A files under the proposal's RFP package dir.

    Validates `document_role` ∈ {'amendment', 'qa_response'} — anything else
    raises ValueError. When `document_role == 'qa_response'`, forces
    `sequence_number = None` silently (Q&A has no buyer-assigned number).

    Returns the newly created RfpPackageDocument rows (the caller spawns
    `spawn_amendment_ingestion(proposal_id, doc.id)` for each).
    """
    if document_role not in {"amendment", "qa_response"}:
        raise ValueError(f"document_role must be 'amendment' or 'qa_response', got {document_role!r}")
    if document_role == "qa_response":
        sequence_number = None

    file_list = list(files)
    if not file_list:
        return []
    _validate_upload_extensions(file_list)
    _validate_upload_sizes(file_list)

    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        raise ValueError(f"proposal {proposal_id} not found")
    pkg_id = proposal.rfp_package_id
    if pkg_id is None:
        raise ValueError(f"proposal {proposal_id} has no rfp_package_id")

    pkg_dir = RFP_PACKAGES_DIR / str(pkg_id)
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # Collision-safe naming. Protect against BOTH in-batch duplicates
    # (multiple files with the same name in one upload) AND on-disk
    # collisions (a prior original-RFP intake or earlier amendment
    # already wrote the same name). Without the on-disk check, a
    # second amendment with the same filename as the original silently
    # overwrites the original's bytes via path.write_bytes.
    seen: dict[str, int] = {}
    new_docs: list[RfpPackageDocument] = []
    for f in file_list:
        base_safe = _safe_filename(f.filename)
        safe = base_safe
        bump = 0
        while safe in seen or (pkg_dir / safe).exists():
            bump += 1
            stem = Path(base_safe).stem
            suffix = Path(base_safe).suffix
            safe = f"{stem}_{bump}{suffix}"
        seen[safe] = 0

        path = pkg_dir / safe
        path.write_bytes(f.content)

        hash_hex = _content_hash(f.content)
        doc = RfpPackageDocument(
            rfp_package_id=pkg_id,
            filename=f.filename,
            storage_path=str(path),
            structure_json={"content_sha256": hash_hex},
            document_role=document_role,
            sequence_number=sequence_number,
        )
        db.add(doc)
        new_docs.append(doc)

    db.flush()  # populate doc.id for callers spawning ingestion threads
    return new_docs


def list_amendments(proposal_id: int, db: Session) -> list[dict]:
    """Return the timeline of non-original documents on a proposal.

    Order: document_role ASC (amendment before qa_response), then
    sequence_number ASC NULLS LAST, then created_at ASC. For each doc,
    attach `latest_run_status` from the most recent AmendmentRun row
    (None when no run has ever been queued).
    """
    proposal = db.get(Proposal, proposal_id)
    if proposal is None or proposal.rfp_package_id is None:
        return []
    pkg_id = proposal.rfp_package_id

    docs = (
        db.query(RfpPackageDocument)
        .filter(
            RfpPackageDocument.rfp_package_id == pkg_id,
            RfpPackageDocument.document_role.is_not(None),
            RfpPackageDocument.document_role != "original",
        )
        .all()
    )

    def _sort_key(d: RfpPackageDocument):
        # document_role ASC; nulls-last on sequence_number; created_at ASC
        seq = d.sequence_number if d.sequence_number is not None else 1_000_000
        return (d.document_role or "", seq, d.created_at or datetime.min)

    docs_sorted = sorted(docs, key=_sort_key)

    out: list[dict] = []
    for d in docs_sorted:
        latest_run = (
            db.query(AmendmentRun)
            .filter(AmendmentRun.document_id == d.id)
            .order_by(AmendmentRun.started_at.desc().nullslast())
            .first()
        )
        latest_status = latest_run.status if latest_run is not None else None
        out.append(
            {
                "id": d.id,
                "filename": d.filename,
                "document_role": d.document_role,
                "sequence_number": d.sequence_number,
                "uploaded_at": d.created_at,
                "latest_run_status": latest_status,
            }
        )
    return out


def _next_req_id(existing_ids: Iterable[str]) -> int:
    """Compute the next sequential REQ-NNN integer based on existing IDs.

    Walks every requirement_id that matches the REQ-NNN pattern, takes
    max() + 1. Returns 1 when the input is empty or no IDs match.
    """
    nums: list[int] = []
    for rid in existing_ids:
        m = _REQ_ID_NUM_RE.match(rid or "")
        if m:
            try:
                nums.append(int(m.group(1)))
            except ValueError:
                pass
    return (max(nums) + 1) if nums else 1


def _coerce_requirement_type(value: str) -> RequirementType:
    """Map a raw type string to a RequirementType enum, falling back via
    the same _REQUIREMENT_TYPE_FALLBACKS map intake.py uses so amendment
    items match the original pipeline's behavior."""
    try:
        return RequirementType(value)
    except ValueError:
        # Import locally to avoid a hard cycle if intake.py ever gains
        # an import from this module.
        from app.jobs.intake import _REQUIREMENT_TYPE_FALLBACKS

        key = (value or "").lower().strip()
        return _REQUIREMENT_TYPE_FALLBACKS.get(key, RequirementType.SHOULD)


def _coerce_requirement_category(value: str) -> RequirementCategory:
    """Map a raw category string to a RequirementCategory enum, falling
    back via the intake module's table."""
    try:
        return RequirementCategory(value)
    except ValueError:
        from app.jobs.intake import _REQUIREMENT_CATEGORY_FALLBACKS

        key = (value or "").lower().strip()
        return _REQUIREMENT_CATEGORY_FALLBACKS.get(
            key,
            RequirementCategory.ADMINISTRATIVE,
        )


def apply_amendment_delta(
    *,
    proposal_id: int,
    amendment_document_id: int,
    delta: ComplianceExtractionResult,
    db: Session,
) -> AmendmentApplyReport:
    """Apply a delta to the proposal's compliance matrix.

    Inserts new_items (with fresh sequential REQ-NNN ids), supersedes
    modified_items (insert new row with same REQ-NNN + flip old row to
    'superseded' + set superseded_by_id), marks removed_items as
    'removed'. Flips `compliance_drift_pending=True` on every
    ProposalSection whose `compliance_items_addressed_json` overlaps the
    modified-or-removed set, and returns an AmendmentApplyReport.

    Returns a populated AmendmentApplyReport. Failures to locate an
    existing_id for a modified/removed entry are logged + skipped (the
    counts in the report reflect only successful mutations).
    """
    # Look up the amendment doc's filename — used as the amendment_origin
    # marker on every row we touch / insert.
    amendment_doc = db.get(RfpPackageDocument, amendment_document_id)
    amendment_filename = amendment_doc.filename if amendment_doc is not None else "(unknown)"

    # Snapshot active items by REQ-ID so we can target modifications / removals.
    active_items = (
        db.query(ComplianceMatrixItem)
        .filter(
            ComplianceMatrixItem.proposal_id == proposal_id,
            ComplianceMatrixItem.status == "active",
        )
        .all()
    )
    existing_by_req_id: dict[str, ComplianceMatrixItem] = {i.requirement_id: i for i in active_items}

    # Compute next REQ-NNN — must account for ALL rows on the proposal
    # (active + superseded + removed) so we don't reuse a retired ID.
    all_req_ids = (
        db.query(ComplianceMatrixItem.requirement_id)
        .filter(ComplianceMatrixItem.proposal_id == proposal_id)
        .all()
    )
    next_seq = _next_req_id([r[0] for r in all_req_ids])

    n_new = 0
    n_modified = 0
    n_removed = 0
    changed_req_ids: set[str] = set()

    # --- new_items ---
    for item in delta.new_items:
        new_req_id = f"REQ-{next_seq:03d}"
        next_seq += 1
        rtype = _coerce_requirement_type(item.requirement_type)
        cat = _coerce_requirement_category(item.category)
        db.add(
            ComplianceMatrixItem(
                proposal_id=proposal_id,
                requirement_id=new_req_id,
                requirement_text=item.requirement_text,
                source_doc=amendment_filename,
                source_section=item.source_section,
                source_page=item.source_page,
                requirement_type=rtype,
                category=cat,
                weight=item.weight,
                compliance_status=ComplianceStatus.TO_BE_DRAFTED,
                amendment_origin=amendment_filename,
                status="active",
            )
        )
        n_new += 1

    # --- modified_items ---
    for mod in delta.modified_items:
        existing_id = mod.get("existing_id")
        new_text = mod.get("new_text")
        if not existing_id or not new_text:
            log.warning(
                "amendment apply: skipping modified_item missing keys: %r",
                mod,
            )
            continue
        existing = existing_by_req_id.get(existing_id)
        if existing is None:
            log.warning(
                "amendment apply: modified_item refs unknown existing_id %r "
                "(not in active items) — skipping.",
                existing_id,
            )
            continue
        # Insert new row carrying SAME requirement_id; copy classification
        # + source citation from the existing row.
        new_row = ComplianceMatrixItem(
            proposal_id=proposal_id,
            requirement_id=existing_id,
            requirement_text=new_text,
            source_doc=amendment_filename,
            source_section=existing.source_section,
            source_page=existing.source_page,
            requirement_type=existing.requirement_type,
            category=existing.category,
            weight=existing.weight,
            compliance_status=ComplianceStatus.TO_BE_DRAFTED,
            amendment_origin=amendment_filename,
            status="active",
        )
        db.add(new_row)
        db.flush()  # populate new_row.id
        existing.status = "superseded"
        existing.superseded_by_id = new_row.id
        n_modified += 1
        changed_req_ids.add(existing_id)

    # --- removed_items ---
    for rem in delta.removed_items:
        existing_id = rem.get("existing_id")
        if not existing_id:
            log.warning(
                "amendment apply: skipping removed_item missing existing_id: %r",
                rem,
            )
            continue
        existing = existing_by_req_id.get(existing_id)
        if existing is None:
            log.warning(
                "amendment apply: removed_item refs unknown existing_id %r (not in active items) — skipping.",
                existing_id,
            )
            continue
        existing.status = "removed"
        existing.amendment_origin = amendment_filename
        n_removed += 1
        changed_req_ids.add(existing_id)

    # --- ProposalSection drift flag ---
    sections_marked_stale: list[str] = []
    if changed_req_ids:
        sections = db.query(ProposalSection).filter(ProposalSection.proposal_id == proposal_id).all()
        for sec in sections:
            sec_ids = set(sec.compliance_items_addressed_json or [])
            if sec_ids & changed_req_ids:
                sec.compliance_drift_pending = True
                sections_marked_stale.append(sec.section_id)

    db.flush()

    log.info(
        "amendment apply: proposal=%d doc=%s -> %d new / %d modified / %d removed; "
        "%d section(s) flagged stale",
        proposal_id,
        amendment_filename,
        n_new,
        n_modified,
        n_removed,
        len(sections_marked_stale),
    )

    return AmendmentApplyReport(
        n_new=n_new,
        n_modified=n_modified,
        n_removed=n_removed,
        sections_marked_stale=sections_marked_stale,
        due_date_changed=False,
        page_limit_changes=[],
    )
