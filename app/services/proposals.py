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
from uuid import uuid4

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.config import RFP_PACKAGES_DIR
from app.core.enums import ProposalRole, ProposalStatus, RfpDocumentType
from app.models import (
    AgentRun,
    ComplianceMatrixItem,
    GapAnalysis,
    MarketScan,
    PolishEdit,
    PricingPackage,
    Proposal,
    ProposalSection,
    ReviewerFinding,
    RfpPackage,
    RfpPackageDocument,
    SubmissionCommitment,
)
from app.services.deletion_quarantine import stage_path_for_transaction
from app.services.proposal_access import (
    ArchivedProposalError,
    ensure_proposal_mutable,
)
from app.services.storage_safety import (
    UnsafeManagedPath,
    require_owned_direct_child_directory,
)

log = logging.getLogger(__name__)

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
ALLOWED_PROPOSAL_EXTENSIONS = {".pdf", ".docx", ".xlsx"}
MAX_PROPOSAL_FILE_BYTES = 50 * 1024 * 1024
MAX_PROPOSAL_PACKAGE_BYTES = 200 * 1024 * 1024

# The intake dispatcher can extract these formats end to end. Keep this
# allowlist next to proposal creation so callers cannot persist a package that
# the background pipeline is guaranteed to reject later.
SUPPORTED_RFP_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx"})


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
            raise ValueError(
                "Unsupported RFP file type: "
                f"{f.filename} has unsupported file type "
                f"{suffix or '(none)'}; allowed: {allowed}."
            )


def _register_package_rollback_cleanup(db: Session, package_dir: Path) -> None:
    """Remove a newly-created package directory if its DB transaction rolls back.

    Proposal creation writes files before the caller commits. Session events
    close that transaction boundary without forcing this service to own the
    caller's commit policy. A successful commit permanently disarms cleanup.
    """
    state = {"committed": False}

    def _after_commit(_session: Session) -> None:
        state["committed"] = True

    def _after_rollback(_session: Session) -> None:
        if state["committed"]:
            return
        try:
            safe_dir = require_owned_direct_child_directory(
                package_dir,
                root=RFP_PACKAGES_DIR,
                expected_name=package_dir.name,
                description="rolled-back RFP package storage path",
            )
            if safe_dir.exists():
                shutil.rmtree(safe_dir)
        except UnsafeManagedPath as exc:
            log.error("refusing rollback cleanup for %s: %s", package_dir, exc)
        except Exception:
            log.exception("failed rollback cleanup for package dir %s", package_dir)

    event.listen(db, "after_commit", _after_commit, once=True)
    event.listen(db, "after_rollback", _after_rollback, once=True)


def _purge_quarantined_package(path: Path) -> None:
    """Permanently remove a package only after its DB deletion commits."""
    shutil.rmtree(path)


def find_duplicate_rfp_documents(
    db: Session, files: Iterable[UploadedFile]
) -> dict[str, RfpPackageDocument]:
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

    unsupported = sorted({
        Path(f.filename).suffix.lower() or "(no extension)"
        for f in file_list
        if Path(f.filename).suffix.lower() not in SUPPORTED_RFP_SUFFIXES
    })
    if unsupported:
        supported = ", ".join(sorted(SUPPORTED_RFP_SUFFIXES))
        raise ValueError(
            "Unsupported RFP file type(s): "
            f"{', '.join(unsupported)}. Supported types: {supported}."
        )

    # 1. RFP package row first so we have an id for the storage path.
    pkg = RfpPackage(
        uploaded_by=uploaded_by,
        uploaded_at=datetime.now(UTC),
        storage_dir="",  # backfilled in step 2
        notes=notes,
    )
    db.add(pkg)
    db.flush()  # populates pkg.id

    pkg_dir = RFP_PACKAGES_DIR / str(pkg.id)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    _register_package_rollback_cleanup(db, pkg_dir)
    pkg.storage_dir = str(pkg_dir)

    # 2. Save files; collide-safely.
    seen: dict[str, int] = {}
    stored_documents: list[tuple[RfpPackageDocument, bytes]] = []
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
        document = RfpPackageDocument(
            rfp_package_id=pkg.id,
            filename=f.filename,
            storage_path=str(path),
            document_type=RfpDocumentType.UNKNOWN,
            structure_json={"content_sha256": hash_hex},
        )
        db.add(document)
        stored_documents.append((document, f.content))

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

    # Cost matrices are discovered synchronously from their structure so the
    # proposal knows about them before intake starts. Discovery only registers
    # the immutable template; it never fills any workbook cells here.
    from app.services.cost_matrix import register_original_cost_matrices

    register_original_cost_matrices(
        db,
        proposal=proposal,
        documents_and_content=stored_documents,
    )
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
    try:
        ensure_proposal_mutable(
            db,
            proposal_id,
            operation="delete proposal",
        )
    except ArchivedProposalError:
        return {
            "deleted": False,
            "reason": "archived proposals are read-only and cannot be deleted",
        }

    pkg_id = p.rfp_package_id
    pkg = db.get(RfpPackage, pkg_id) if pkg_id else None
    if pkg is None:
        return {
            "deleted": False,
            "reason": "RFP package record is missing; deletion was blocked",
        }

    # The path came from the database, so prove it is exactly the directory
    # this package owns before deleting any rows. A stale or tampered path must
    # never turn proposal deletion into an arbitrary recursive filesystem delete.
    try:
        storage_dir = require_owned_direct_child_directory(
            pkg.storage_dir,
            root=RFP_PACKAGES_DIR,
            expected_name=str(pkg.id),
            description="RFP package storage path",
        )
    except UnsafeManagedPath as exc:
        return {"deleted": False, "reason": str(exc)}

    result = {"deleted": True}
    quarantine_path = storage_dir.with_name(
        f".delete-proposal-{pkg.id}-{uuid4().hex}"
    )
    managed_root = RFP_PACKAGES_DIR
    stage_error = stage_path_for_transaction(
        db,
        original_path=storage_dir,
        quarantine_path=quarantine_path,
        validate_quarantine=lambda path: require_owned_direct_child_directory(
            path,
            root=managed_root,
            expected_name=quarantine_path.name,
            description="quarantined RFP package storage path",
        ),
        purge_quarantine=_purge_quarantined_package,
        result=result,
        description="RFP package storage",
        logger=log,
    )
    if stage_error is not None:
        return {"deleted": False, "reason": stage_error}

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
    result.update(counts)

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

    return result


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

    # Belt-and-suspenders: clean only confirmed orphan directories. A proposal
    # whose stored path failed validation still has its package row, so its
    # numeric directory must not be swept up by this recovery pass.
    live_package_ids = {row[0] for row in db.query(RfpPackage.id).all()}
    if RFP_PACKAGES_DIR.exists():
        for child in RFP_PACKAGES_DIR.iterdir():
            if (
                child.is_dir()
                and child.name.isdigit()
                and int(child.name) not in live_package_ids
            ):
                try:
                    safe_child = require_owned_direct_child_directory(
                        child,
                        root=RFP_PACKAGES_DIR,
                        expected_name=child.name,
                        description="orphan RFP package storage path",
                    )
                    shutil.rmtree(safe_child)
                    counts["files_deleted_dirs"] += 1
                except UnsafeManagedPath as exc:
                    log.warning("refusing to remove orphan directory %s: %s", child, exc)
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
    from sqlalchemy import select

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
            # liveness signal. A terminal failed/cancelled row is definitive
            # even when recent; otherwise no stages means stale immediately.
            latest_stage = db.execute(
                select(AgentRun)
                .where(
                    AgentRun.proposal_id == p.id,
                    AgentRun.agent_name == "_stage",
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()

            age = _age_seconds(latest_stage.created_at if latest_stage else None)
            latest_status = (
                latest_stage.status.value
                if latest_stage is not None and hasattr(latest_stage.status, "value")
                else (str(latest_stage.status) if latest_stage is not None else None)
            )
            terminal_stage = latest_status in {"failed", "cancelled"}
            if not terminal_stage and age is not None and age < threshold:
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


def reset_for_intake_retry(proposal_id: int) -> dict:
    """Transactionally reset an incomplete intake before retrying it.

    The RFP package, extracted document text/files, proposal metadata, audit
    history, user-curated team/timeline/standalone checklist commitments, and
    outcome are kept.
    Derived scope/draft/review/cost rows are removed so a partial prior run
    cannot leak duplicate requirements, obsolete gaps, drafts, findings, or
    pricing into the replacement run. Per-requirement checklist flags are
    necessarily discarded with that invalid compliance extraction; standalone
    commitments remain intact and are unlinked from deleted sections.

    A fresh non-terminal stage is treated as a live pipeline and blocks the
    reset. This makes double-clicks and visits to Progress during an active
    intake non-destructive. A failed/cancelled terminal stage may retry
    immediately; otherwise activity must be at least five minutes old.

    A proposal that already reached scope sign-off may also retry, but only
    when the durable per-document requirements-review state is one that the
    scope gate itself blocks (for example pending, failed, partial, unknown,
    extracting, or reviewing). Healthy advanced proposals remain immutable through this
    recovery path.
    """
    from app.db.session import session_scope  # local import; test/startup safe

    now = datetime.now(UTC)

    def _age_seconds(when: datetime | None) -> float | None:
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return (now - when).total_seconds()

    with session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        if proposal is None:
            return {"ok": False, "reason": "not_found"}

        status_value = (
            proposal.status.value
            if hasattr(proposal.status, "value")
            else str(proposal.status)
        )
        requirements_review_retry = False
        if status_value == ProposalStatus.AWAITING_SCOPE_SIGNOFF.value:
            # Keep recovery authorization aligned with the authoritative
            # scope gate rather than maintaining a second status allowlist.
            from app.services.workflow import blocking_requirements_reviews

            blocked_reviews = blocking_requirements_reviews(
                db,
                rfp_package_id=proposal.rfp_package_id,
            )
            requirements_review_retry = bool(blocked_reviews)
            if not requirements_review_retry:
                return {
                    "ok": False,
                    "reason": "invalid_status",
                    "status": status_value,
                }
        elif status_value != ProposalStatus.INTAKING.value:
            return {
                "ok": False,
                "reason": "invalid_status",
                "status": status_value,
            }

        # An awaiting-scope-signoff proposal has no intake worker in flight;
        # its incomplete durable review is the retry authorization. Intaking
        # proposals still require the existing stale/terminal-stage proof.
        if not requirements_review_retry:
            latest_stage = db.execute(
                select(AgentRun)
                .where(
                    AgentRun.proposal_id == proposal_id,
                    AgentRun.agent_name == "_stage",
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()

            if latest_stage is not None:
                from app.services.stages import TERMINAL_STAGE_MARKER

                latest_status = (
                    latest_stage.status.value
                    if hasattr(latest_stage.status, "value")
                    else str(latest_stage.status)
                )
                age = _age_seconds(latest_stage.created_at)
                # FAILED is also used for recoverable intake sub-steps (for
                # example Section M extraction) while the pipeline continues.
                # Only an explicitly terminal failure/cancellation can authorize
                # an immediate destructive reset.  Keep the exact legacy final
                # message as a compatibility bridge for rows written before the
                # marker existed.
                terminal = latest_status == "cancelled" or (
                    latest_status == "failed"
                    and (
                        latest_stage.prompt_version == TERMINAL_STAGE_MARKER
                        or latest_stage.error_text == "Pipeline failed — check logs."
                    )
                )
            else:
                latest_status = None
                # Close the tiny proposal-create → first-stage race: a brand-new
                # intake with no stage yet is presumed live, while an old row with
                # no stage is recoverable.
                age = _age_seconds(proposal.updated_at or proposal.created_at)
                terminal = False

            if not terminal and age is not None and age < _STALE_BUSY_SECONDS:
                return {
                    "ok": False,
                    "reason": "pipeline_active",
                    "age_seconds": age,
                    "stage_status": latest_status,
                }

        section_ids = [
            row[0]
            for row in db.query(ProposalSection.id)
            .filter(ProposalSection.proposal_id == proposal_id)
            .all()
        ]

        counts = {
            "compliance_items": db.query(ComplianceMatrixItem)
            .filter(ComplianceMatrixItem.proposal_id == proposal_id)
            .count(),
            "gap_analyses": db.query(GapAnalysis)
            .filter(GapAnalysis.proposal_id == proposal_id)
            .count(),
            "sections": len(section_ids),
            "reviewer_findings": (
                db.query(ReviewerFinding)
                .filter(ReviewerFinding.proposal_section_id.in_(section_ids))
                .count()
                if section_ids
                else 0
            ),
            "polish_edits": db.query(PolishEdit)
            .filter(PolishEdit.proposal_id == proposal_id)
            .count(),
            "pricing_packages": db.query(PricingPackage)
            .filter(PricingPackage.proposal_id == proposal_id)
            .count(),
            "market_scans": db.query(MarketScan)
            .filter(MarketScan.proposal_id == proposal_id)
            .count(),
        }

        # Bulk deletes do not invoke ORM relationship cascades, so remove the
        # section-owned tables explicitly and unlink user checklist rows first.
        if section_ids:
            db.query(SubmissionCommitment).filter(
                SubmissionCommitment.source_section_id.in_(section_ids)
            ).update(
                {SubmissionCommitment.source_section_id: None},
                synchronize_session=False,
            )
            db.query(ReviewerFinding).filter(
                ReviewerFinding.proposal_section_id.in_(section_ids)
            ).delete(synchronize_session=False)
        db.query(PolishEdit).filter(
            PolishEdit.proposal_id == proposal_id
        ).delete(synchronize_session=False)
        db.query(ProposalSection).filter(
            ProposalSection.proposal_id == proposal_id
        ).delete(synchronize_session=False)
        db.query(GapAnalysis).filter(
            GapAnalysis.proposal_id == proposal_id
        ).delete(synchronize_session=False)
        db.query(ComplianceMatrixItem).filter(
            ComplianceMatrixItem.proposal_id == proposal_id
        ).delete(synchronize_session=False)
        # Pricing children and relational cost-review findings cascade from
        # PricingPackage; awards/competitors cascade from MarketScan.
        db.query(PricingPackage).filter(
            PricingPackage.proposal_id == proposal_id
        ).delete(synchronize_session=False)
        db.query(MarketScan).filter(
            MarketScan.proposal_id == proposal_id
        ).delete(synchronize_session=False)

        # Reset proposal-level agent products rooted in the discarded scope or
        # drafts. User choices (framing, service line, pricing selection,
        # timeline, roster) deliberately remain intact.
        proposal.cots_orientation = False
        proposal.evaluation_criteria_json = None
        proposal.evaluator_scorecard_json = None
        proposal.win_themes_json = None
        proposal.past_performance_matches_json = None
        proposal.price_to_win_json = None
        proposal.red_team_findings_json = None
        proposal.graphics_tables_json = None
        proposal.cost_review_strategy_markdown = None
        proposal.cost_review_strategy_generated_at = None
        proposal.cost_review_strategy_findings_count = None
        proposal.payment_market_scan_json = None
        proposal.payment_cost_review_findings_json = None
        proposal.team_approved_at = None
        proposal.status = ProposalStatus.INTAKING

        # The replacement run rebuilds a package-wide matrix. Reset every
        # document, not only the original blocker, so progress never mixes
        # stale terminal results with the new run. Preserve source text and
        # unrelated parsed structure metadata.
        documents = (
            db.query(RfpPackageDocument)
            .filter(RfpPackageDocument.rfp_package_id == proposal.rfp_package_id)
            .order_by(RfpPackageDocument.id)
            .all()
        )
        pending_at = now.isoformat()
        for document in documents:
            structure = dict(document.structure_json or {})
            structure["requirements_review"] = {
                "schema_version": 1,
                "status": "pending",
                "source_document_id": document.id,
                "requires_manual_review": False,
                "reason": "Queued for a fresh requirements extraction and review.",
                "updated_at": pending_at,
            }
            document.structure_json = structure

        return {"ok": True, **counts}
