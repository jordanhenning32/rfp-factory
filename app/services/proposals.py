"""Proposal creation + deletion service.

Wraps the multi-step "create proposal + RFP package + save files" flow so
the UI and any future API endpoints share one implementation.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import RFP_PACKAGES_DIR
from app.core.enums import ProposalRole, ProposalStatus, RfpDocumentType
from app.models import (
    AgentRun,
    ComplianceMatrixItem,
    GapAnalysis,
    PricingPackage,
    Proposal,
    ProposalSection,
    RfpPackage,
    RfpPackageDocument,
)

log = logging.getLogger(__name__)

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
ALLOWED_PROPOSAL_EXTENSIONS = {".pdf", ".docx", ".xlsx"}
MAX_PROPOSAL_FILE_BYTES = 50 * 1024 * 1024
MAX_PROPOSAL_PACKAGE_BYTES = 200 * 1024 * 1024


@dataclass
class UploadedFile:
    filename: str
    content: bytes


def _safe_filename(name: str) -> str:
    """Strip path components and reduce to safe characters.

    Prevents directory traversal (../../etc/passwd) and quirky chars on
    Windows. Falls back to "file" if the name reduces to empty.
    """
    base = Path(name).name  # drops any path components
    cleaned = _SAFE_FILENAME.sub("_", base).strip("._")
    return cleaned or "file"


def _parse_due_date(text: str | None) -> date | None:
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None  # caller can validate separately if it cares


def _content_hash(data: bytes) -> str:
    """SHA-256 of the raw file bytes, hex digest. Used to detect duplicate RFP uploads."""
    return hashlib.sha256(data).hexdigest()


def _validate_upload_sizes(files: list[UploadedFile]) -> None:
    total = 0
    for f in files:
        size = len(f.content)
        if size > MAX_PROPOSAL_FILE_BYTES:
            raise ValueError(
                f"{f.filename} is too large "
                f"({size / 1024 / 1024:.1f} MB; max {MAX_PROPOSAL_FILE_BYTES / 1024 / 1024:.0f} MB)."
            )
        total += size
    if total > MAX_PROPOSAL_PACKAGE_BYTES:
        raise ValueError(
            "RFP package is too large "
            f"({total / 1024 / 1024:.1f} MB; max {MAX_PROPOSAL_PACKAGE_BYTES / 1024 / 1024:.0f} MB)."
        )


def _validate_upload_extensions(files: list[UploadedFile]) -> None:
    allowed = ", ".join(sorted(ALLOWED_PROPOSAL_EXTENSIONS))
    for f in files:
        suffix = Path(f.filename).suffix.lower()
        if suffix not in ALLOWED_PROPOSAL_EXTENSIONS:
            raise ValueError(f"{f.filename} has unsupported file type {suffix or '(none)'}; allowed: {allowed}.")


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def find_duplicate_rfp_documents(db: Session, files: Iterable[UploadedFile]) -> dict[str, RfpPackageDocument]:
    """For each file, return the existing RfpPackageDocument with matching content.

    Reads structure_json.content_sha256. Older RFP package documents without a
    hash are silently ignored.
    """
    file_list = list(files)
    if not file_list:
        return {}
    _validate_upload_extensions(file_list)
    _validate_upload_sizes(file_list)
    hashes_to_files: dict[str, list[str]] = {}
    for f in file_list:
        hashes_to_files.setdefault(_content_hash(f.content), []).append(f.filename)
    existing = db.query(RfpPackageDocument).filter(RfpPackageDocument.structure_json.is_not(None)).all()
    matches: dict[str, RfpPackageDocument] = {}
    for doc in existing:
        h = (doc.structure_json or {}).get("content_sha256")
        if h and h in hashes_to_files:
            for filename in hashes_to_files[h]:
                matches[filename] = doc
    return matches


def create_proposal_with_files(
    db: Session,
    *,
    title: str,
    files: Iterable[UploadedFile],
    agency: str | None = None,
    naics: str | None = None,
    due_date_str: str | None = None,
    role: str = "prime",
    notes: str | None = None,
    uploaded_by: str | None = "local",
) -> Proposal:
    """Create RfpPackage + Proposal + RfpPackageDocument rows, persist files.

    Returns the persisted Proposal. Caller is responsible for committing the
    session — if anything raises, the partial filesystem writes are left in
    place under data/rfp_packages/<pkg.id>/ and can be cleaned up by id.
    structure_json["content_sha256"] is populated for every row so callers can
    detect duplicate-content uploads via find_duplicate_rfp_documents.
    """
    file_list = list(files)
    if not file_list:
        raise ValueError("At least one file is required to create an RFP package.")
    if not title or not title.strip():
        raise ValueError("Proposal title is required.")
    _validate_upload_extensions(file_list)
    _validate_upload_sizes(file_list)

    # 1. RFP package row first so we have an id for the storage path.
    pkg = RfpPackage(
        uploaded_by=uploaded_by,
        uploaded_at=datetime.utcnow(),
        storage_dir="",  # backfilled in step 2
        notes=notes,
    )
    db.add(pkg)
    db.flush()  # populates pkg.id

    pkg_dir = RFP_PACKAGES_DIR / str(pkg.id)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    pkg.storage_dir = str(pkg_dir)

    # 2. Save files; collide-safely.
    seen: dict[str, int] = {}
    for f in file_list:
        safe = _safe_filename(f.filename)
        if safe in seen:
            seen[safe] += 1
            stem = Path(safe).stem
            suffix = Path(safe).suffix
            safe = f"{stem}_{seen[safe]}{suffix}"
        else:
            seen[safe] = 0

        path = pkg_dir / safe
        path.write_bytes(f.content)

        hash_hex = _content_hash(f.content)
        db.add(
            RfpPackageDocument(
                rfp_package_id=pkg.id,
                filename=f.filename,
                storage_path=str(path),
                document_type=RfpDocumentType.UNKNOWN,
                structure_json={"content_sha256": hash_hex},
            )
        )

    # 3. Proposal row.
    try:
        role_enum = ProposalRole(role)
    except ValueError:
        role_enum = ProposalRole.PRIME

    proposal = Proposal(
        rfp_package_id=pkg.id,
        title=title.strip(),
        agency=(agency or "").strip() or None,
        naics=(naics or "").strip() or None,
        due_date=_parse_due_date(due_date_str),
        role=role_enum,
        status=ProposalStatus.INTAKING,
        notes=(notes or "").strip() or None,
    )
    db.add(proposal)
    db.flush()
    return proposal


def delete_proposal(db: Session, proposal_id: int) -> dict:
    """Delete a proposal and everything that hangs off it: compliance items,
    gap analyses, sections, pricing, agent runs, RFP package + documents,
    and the on-disk files. Returns counts of what was removed.

    Cascades on the FKs cover most of this, but we want explicit counts and
    we need to remove the on-disk RFP package directory ourselves.
    """
    p = db.get(Proposal, proposal_id)
    if p is None:
        return {"deleted": False, "reason": "not_found"}

    pkg_id = p.rfp_package_id
    pkg = db.get(RfpPackage, pkg_id) if pkg_id else None
    storage_dir = pkg.storage_dir if pkg else None

    counts = {
        "compliance_items": db.query(ComplianceMatrixItem)
        .filter(ComplianceMatrixItem.proposal_id == proposal_id)
        .count(),
        "gap_analyses": db.query(GapAnalysis).filter(GapAnalysis.proposal_id == proposal_id).count(),
        "sections": db.query(ProposalSection).filter(ProposalSection.proposal_id == proposal_id).count(),
        "pricing_packages": db.query(PricingPackage)
        .filter(PricingPackage.proposal_id == proposal_id)
        .count(),
        "agent_runs": db.query(AgentRun).filter(AgentRun.proposal_id == proposal_id).count(),
        "documents": (
            db.query(RfpPackageDocument).filter(RfpPackageDocument.rfp_package_id == pkg_id).count()
            if pkg_id
            else 0
        ),
    }

    # Explicit cleanup of agent_runs — not in the ORM relationship cascade,
    # and we don't trust SQLite's FK CASCADE alone (only fires when PRAGMA
    # foreign_keys=ON, which session.py enables but belt-and-suspenders).
    db.query(AgentRun).filter(AgentRun.proposal_id == proposal_id).delete(synchronize_session=False)

    # Delete the proposal — its ORM cascade clears compliance_items, gaps,
    # sections, pricing. Then delete the rfp_package.
    db.delete(p)
    db.flush()
    if pkg is not None:
        db.delete(pkg)
        db.flush()

    # Now the filesystem.
    if storage_dir:
        try:
            sd = Path(storage_dir)
            expected_dir = (RFP_PACKAGES_DIR / str(pkg_id)).resolve() if pkg_id else None
            resolved_sd = sd.resolve()
            if expected_dir is None or resolved_sd != expected_dir or not _path_is_within(sd, RFP_PACKAGES_DIR):
                log.error(
                    "refusing to remove unexpected proposal storage dir for package %s: %s (expected %s)",
                    pkg_id,
                    storage_dir,
                    expected_dir,
                )
                return {"deleted": True, **counts}
            if sd.exists() and sd.is_dir():
                shutil.rmtree(sd)
        except Exception:
            log.exception("failed to remove storage dir %s", storage_dir)

    return {"deleted": True, **counts}


def wipe_all_test_data(db: Session) -> dict:
    """Delete every proposal and its dependents. Use only when you mean it.

    Does NOT touch the knowledge base or the company profile — only
    proposal-side data and the data/rfp_packages/ tree.
    """
    counts = {"proposals": 0, "rfp_packages": 0, "files_deleted_dirs": 0}
    proposal_ids = [p.id for p in db.query(Proposal).all()]
    for pid in proposal_ids:
        result = delete_proposal(db, pid)
        if result.get("deleted"):
            counts["proposals"] += 1
            counts["rfp_packages"] += 1

    # Belt-and-suspenders: clean any orphan dirs that lost their pkg row.
    if RFP_PACKAGES_DIR.exists():
        for child in RFP_PACKAGES_DIR.iterdir():
            if child.is_dir() and child.name.isdigit():
                try:
                    shutil.rmtree(child)
                    counts["files_deleted_dirs"] += 1
                except Exception:
                    log.exception("failed to remove orphan dir %s", child)
    return counts


# ---- Stale-status recovery (app startup hook) ---------------------------
# Background pipelines (intake, outline, writer team, reviewer auto-loop)
# run in daemon threads. If the app process is killed mid-flight (Ctrl+C,
# crash, host reboot), those threads die without ever flipping the
# proposal status back to a quiescent state. The next process startup
# sees the orphan busy status and renders a stuck "X is in progress"
# banner forever. This pass detects + repairs that on app boot.
#
# Repair mappings (busy → quiescent revert target):
#   draft_in_progress → awaiting_outline_approval
#       Rationale: re-clicking "Approve & Begin Drafting" is now
#       resume-aware (skips already-drafted sections) — exactly what
#       the user wants after an interrupted Writer Team run.
#   reviewing         → draft_ready
#       Rationale: reviewer was running before; user can re-trigger.
#   pricing           → draft_ready
#       Cost Analysis Agent isn't built yet (Weeks 12-13); placeholder.
#   intaking          → NO REVERT
#       Intake is the first phase; there's no earlier state to revert
#       to. Compliance matrix may be partial. User should click "Retry
#       pipeline" on the Run Progress page (which wipes + re-runs).
#       We log a warning so the operator sees the stuck case but don't
#       mutate the row — Retry is the safe path.

# How long the latest "_stage" agent_run can be without a fresh write
# before we conclude the underlying thread is dead. 5 minutes is well
# beyond the longest single LLM call we observe in practice (~2 min for
# Sonnet on a 200-item compliance matrix); under 5min we err on the side
# of "still running, leave alone."
_STALE_BUSY_SECONDS = 5 * 60

_BUSY_STATUS_REVERT = {
    # Phase 2B reorder: a crashed Writer Team reverts to
    # AWAITING_DRAFT (the gate after Cost Analyst) so the user
    # can retry without rewinding past the team / cost work.
    ProposalStatus.DRAFT_IN_PROGRESS: ProposalStatus.AWAITING_DRAFT,
    ProposalStatus.REVIEWING: ProposalStatus.DRAFT_READY,
    ProposalStatus.PRICING: ProposalStatus.DRAFT_READY,
}


def recover_stale_busy_proposals() -> dict:
    """Scan for proposals stuck in a busy status because their pipeline
    thread was killed. Revert each to its appropriate quiescent state.

    Returns a dict with `reverted` (list of (proposal_id, old_status,
    new_status) tuples) and `intaking_stuck` (list of proposal_ids that
    need a manual Retry — we don't mutate those).

    Safe to call before any background threads have been spawned (i.e.,
    at app startup) since no in-flight work could be interrupted by a
    status flip.
    """
    from sqlalchemy import func, select

    from app.db.session import session_scope  # local import; avoids cycles

    now = datetime.now(UTC)
    threshold = _STALE_BUSY_SECONDS
    reverted: list[tuple[int, str, str]] = []
    intaking_stuck: list[int] = []

    busy_statuses = list(_BUSY_STATUS_REVERT.keys()) + [ProposalStatus.INTAKING]

    def _age_seconds(when: datetime | None) -> float | None:
        """Normalize a possibly-naive DB timestamp to UTC and return its
        age in seconds. None when there's no stage record."""
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return (now - when).total_seconds()

    with session_scope() as db:
        rows = db.execute(select(Proposal).where(Proposal.status.in_(busy_statuses))).scalars().all()

        for p in rows:
            # Find the most recent _stage entry for this proposal as the
            # liveness signal. No stages → treat as stale immediately.
            latest_stage_at = db.execute(
                select(func.max(AgentRun.created_at)).where(
                    AgentRun.proposal_id == p.id,
                    AgentRun.agent_name == "_stage",
                )
            ).scalar()

            age = _age_seconds(latest_stage_at)
            if age is not None and age < threshold:
                # Still potentially active — leave alone.
                continue

            age_str = f"{age:.0f}s" if age is not None else "no stages"
            old_status = p.status
            if old_status == ProposalStatus.INTAKING:
                intaking_stuck.append(p.id)
                log.warning(
                    "stale-status recovery: proposal %d stuck in 'intaking' "
                    "(latest stage age=%s) — leaving status alone; user "
                    "should click 'Retry pipeline' on the Run Progress page.",
                    p.id,
                    age_str,
                )
                continue

            new_status = _BUSY_STATUS_REVERT[old_status]
            old_value = old_status.value if hasattr(old_status, "value") else str(old_status)
            new_value = new_status.value if hasattr(new_status, "value") else str(new_status)
            p.status = new_status
            reverted.append((p.id, old_value, new_value))
            log.warning(
                "stale-status recovery: proposal %d %r -> %r (latest stage "
                "age=%s; pipeline thread presumed dead).",
                p.id,
                old_value,
                new_value,
                age_str,
            )

    return {"reverted": reverted, "intaking_stuck": intaking_stuck}
