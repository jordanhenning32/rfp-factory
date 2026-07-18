"""Amendment ingestion daemon-thread orchestrator.

Two public entrypoints:
  - run_amendment_ingestion(proposal_id, document_id): sync end-to-end
    ingestion of one amendment / Q&A document. Inserts an AmendmentRun
    row with status='running' on entry, flips to 'completed' on success
    or 'failed' on exception, persists the AmendmentApplyReport JSON.
  - spawn_amendment_ingestion(proposal_id, document_id): daemon-thread
    wrapper. Used by the Amendments & Q&A tab upload handlers.

Mirrors the daemon-thread pattern of `spawn_intake` / `spawn_section_m_only`
in app/jobs/intake.py.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime

from app.agents.compliance_matrix import (
    ComplianceExtractionResult,
    extract_compliance_items,
)
from app.db.session import session_scope
from app.models import (
    AmendmentRun,
    ComplianceMatrixItem,
    RfpPackageDocument,
)
from app.services.amendments import (
    AmendmentApplyReport,
    apply_amendment_delta,
)
from app.services.stages import record_stage as _set_stage

log = logging.getLogger(__name__)


# Heuristic substrings — when ANY of these appears in the amendment text
# or a modified item's new_text / change_summary, we re-run the Section M
# extractor since the amendment may have changed evaluation factors.
_SECTION_M_TRIGGER_SUBSTRINGS = (
    "evaluation factor",
    "section m",
    "scoring",
)


def _delta_touches_evaluation_criteria(
    delta: ComplianceExtractionResult,
) -> bool:
    """Substring heuristic: did the amendment likely change Section M?

    Trigger conditions:
      - any new_item with category == 'evaluation_criterion'
      - any modified_item whose new_text or change_summary contains
        'evaluation factor' / 'section m' / 'scoring'
    Returns False otherwise.
    """
    for item in delta.new_items:
        if (item.category or "").lower() == "evaluation_criterion":
            return True
    for mod in delta.modified_items:
        blob = " ".join(
            [
                str(mod.get("new_text") or ""),
                str(mod.get("change_summary") or ""),
            ]
        ).lower()
        for substr in _SECTION_M_TRIGGER_SUBSTRINGS:
            if substr in blob:
                return True
    return False


def run_amendment_ingestion(
    *,
    proposal_id: int,
    document_id: int,
) -> AmendmentApplyReport:
    """Run amendment ingestion synchronously for one document.

    Stages:
      1. Insert AmendmentRun(status='running', started_at=now).
      2. Read the document's text (using the intake module's dispatcher).
      3. Snapshot ACTIVE compliance items.
      4. Call extract_compliance_items(..., delta_mode=True).
      5. Call apply_amendment_delta(...).
      6. Best-effort Section-M re-run when the delta touches evaluation
         criteria.
      7. Update AmendmentRun(status='completed', report_json=...).

    On exception: update the AmendmentRun row with status='failed' and
    error_text=str(exc)[:2000], then re-raise so the daemon wrapper
    surfaces the failure to logs.
    """
    log.info(
        "amendment ingestion starting for proposal=%d doc=%d",
        proposal_id,
        document_id,
    )

    # --- 1. Insert audit row ---
    amendment_run_id: int | None = None
    with session_scope() as db:
        doc = db.get(RfpPackageDocument, document_id)
        if doc is None:
            raise ValueError(f"document {document_id} not found")
        filename = doc.filename
        storage_path = doc.storage_path

        run = AmendmentRun(
            proposal_id=proposal_id,
            document_id=document_id,
            status="running",
            started_at=datetime.now(UTC),
        )
        db.add(run)
        db.flush()
        amendment_run_id = run.id

    _set_stage(proposal_id, f"Processing amendment {filename}...")

    try:
        # --- 2. Extract text ---
        # Local import keeps the intake module off this module's import
        # path until first use.
        from app.jobs.intake import _extract_text_for_intake

        text, _ = _extract_text_for_intake(storage_path, filename)

        # --- 3. Snapshot active items ---
        with session_scope() as db:
            active_items = (
                db.query(ComplianceMatrixItem)
                .filter(
                    ComplianceMatrixItem.proposal_id == proposal_id,
                    ComplianceMatrixItem.status == "active",
                )
                .order_by(ComplianceMatrixItem.id)
                .all()
            )
            existing_items = [
                {
                    "requirement_id": i.requirement_id,
                    "requirement_text": i.requirement_text,
                    "source_section": i.source_section,
                    "source_page": i.source_page,
                }
                for i in active_items
            ]

        # --- 4. Run delta extraction ---
        delta = extract_compliance_items(
            document_text=text,
            filename=filename,
            proposal_id=proposal_id,
            existing_items=existing_items,
            delta_mode=True,
        )

        # --- 5. Apply delta ---
        with session_scope() as db:
            report = apply_amendment_delta(
                proposal_id=proposal_id,
                amendment_document_id=document_id,
                delta=delta,
                db=db,
            )

        # --- 6. Best-effort Section-M re-run ---
        if _delta_touches_evaluation_criteria(delta):
            try:
                from app.services.evaluation_criteria import (
                    extract_and_persist_evaluation_criteria,
                )

                _set_stage(
                    proposal_id,
                    "Amendment touched evaluation criteria — re-running Section M…",
                )
                extract_and_persist_evaluation_criteria(proposal_id)
            except Exception:
                log.exception(
                    "amendment: best-effort Section-M re-run failed for proposal=%d doc=%d — continuing.",
                    proposal_id,
                    document_id,
                )

        # --- 7. Mark completed ---
        with session_scope() as db:
            run_row = db.get(AmendmentRun, amendment_run_id)
            if run_row is not None:
                run_row.status = "completed"
                run_row.completed_at = datetime.now(UTC)
                run_row.report_json = json.dumps(report.as_dict())

        _set_stage(
            proposal_id,
            f"Amendment {filename} applied: "
            f"{report.n_new} new, {report.n_modified} modified, "
            f"{report.n_removed} removed.",
        )
        return report

    except Exception as exc:
        log.exception(
            "amendment ingestion failed for proposal=%d doc=%d",
            proposal_id,
            document_id,
        )
        # Best-effort failure record. Suppress secondary exceptions so the
        # primary one (re-raised below) is what the caller sees.
        try:
            with session_scope() as db:
                run_row = db.get(AmendmentRun, amendment_run_id)
                if run_row is not None:
                    run_row.status = "failed"
                    run_row.completed_at = datetime.now(UTC)
                    run_row.error_text = str(exc)[:2000]
        except Exception:
            log.exception(
                "amendment: failed to persist 'failed' status on run %s",
                amendment_run_id,
            )
        _set_stage(
            proposal_id,
            f"Amendment {filename} failed — check logs.",
        )
        raise


def spawn_amendment_ingestion(
    proposal_id: int,
    document_id: int,
) -> threading.Thread:
    """Fire-and-forget daemon thread for one amendment / Q&A document.

    Daemon so it doesn't block app exit. Exceptions are caught + logged
    inside the wrapper so the daemon doesn't die silently — the
    AmendmentRun row will have status='failed' with the exception text.
    """

    def _target():
        try:
            run_amendment_ingestion(
                proposal_id=proposal_id,
                document_id=document_id,
            )
        except Exception:
            log.exception(
                "amendment thread died for proposal=%d doc=%d",
                proposal_id,
                document_id,
            )

    t = threading.Thread(
        target=_target,
        name=f"amendment-{proposal_id}-{document_id}",
        daemon=True,
    )
    t.start()
    return t
