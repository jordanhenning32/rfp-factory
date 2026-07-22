"""Evaluation-criteria persistence + helpers.

Re-extraction and persistence service for proposals.evaluation_criteria_json
(migration 0032). State is a single JSON document per proposal; structure
mirrors the report_evaluation_criteria tool schema from
app/agents/section_m_extractor.py.

Three public entrypoints:
  extract_and_persist_evaluation_criteria(proposal_id) -> bool
      Fetch RFP package text, run the Section M extractor, persist JSON.
      Returns True on success, False on failure (logged via log.exception).

  load_evaluation_criteria(proposal_id) -> dict | None
      Read and parse evaluation_criteria_json for a proposal.

  format_evaluation_criteria_block(criteria, *, section_targeted_factor_ids) -> str
      Render a compact prompt-ready block for Reviewer A's cached prefix.
      Returns empty string when criteria is None or empty.
"""

from __future__ import annotations

import json
import logging

from app.agents.section_m_extractor import extract_evaluation_criteria
from app.db.session import session_scope
from app.models import Proposal
from app.models.compliance import ComplianceMatrixItem
from app.services.proposal_access import ensure_proposal_mutable

log = logging.getLogger(__name__)


def extract_and_persist_evaluation_criteria(proposal_id: int) -> bool:
    """Fetch the RFP package text, run the Section M extractor, persist the result.

    Returns True on success, False on failure (failure is logged via
    log.exception so the operator can inspect the traceback).
    """
    with session_scope() as db:
        ensure_proposal_mutable(
            db, proposal_id, operation="extract evaluation criteria",
        )

    try:
        # --- snapshot phase: read all needed primitives before LLM call ---
        from app.jobs.intake import _extract_text_for_intake  # local import avoids circular

        doc_snapshots: list[dict] = []
        compliance_items: list[dict] = []

        with session_scope() as db:
            proposal = db.get(Proposal, proposal_id)
            if not proposal or not proposal.rfp_package:
                log.warning(
                    "evaluation_criteria: proposal %d has no RFP package — skipping.",
                    proposal_id,
                )
                return False
            for doc in proposal.rfp_package.documents:
                doc_snapshots.append(
                    {
                        "filename": doc.filename,
                        "storage_path": doc.storage_path,
                        "extracted_text_md": doc.extracted_text_md,
                    }
                )
            for item in (
                db.query(ComplianceMatrixItem)
                .filter(
                    ComplianceMatrixItem.proposal_id == proposal_id,
                    ComplianceMatrixItem.status == "active",
                )
                .all()
            ):
                compliance_items.append(
                    {
                        "requirement_id": item.requirement_id,
                        "requirement_text": item.requirement_text or "",
                    }
                )

        if not doc_snapshots:
            log.warning(
                "evaluation_criteria: proposal %d has no documents — skipping.",
                proposal_id,
            )
            return False

        # --- text extraction phase ---
        body_parts: list[str] = []
        first_filename = doc_snapshots[0]["filename"]
        for snap in doc_snapshots:
            text = snap["extracted_text_md"]
            if not text:
                try:
                    text, _ = _extract_text_for_intake(
                        snap["storage_path"],
                        snap["filename"],
                    )
                except Exception:
                    log.exception(
                        "evaluation_criteria: failed to extract text from %s",
                        snap["filename"],
                    )
                    text = ""
            if text:
                body_parts.append(f"\n--- RFP FILE: {snap['filename']} ---\n{text}\n")

        concatenated = "".join(body_parts)

        # --- extraction phase ---
        criteria = extract_evaluation_criteria(
            proposal_id=proposal_id,
            document_text=concatenated,
            filename=first_filename,
            compliance_items=compliance_items or None,
        )

        # --- persistence phase ---
        with session_scope() as db:
            proposal = ensure_proposal_mutable(
                db, proposal_id, operation="persist evaluation criteria",
            )
            if proposal is None:
                log.error(
                    "evaluation_criteria: proposal %d vanished before persist.",
                    proposal_id,
                )
                return False
            proposal.evaluation_criteria_json = json.dumps(criteria.as_dict())

        return True

    except Exception:
        log.exception(
            "evaluation_criteria: extract_and_persist failed for proposal %d",
            proposal_id,
        )
        return False


def load_evaluation_criteria(proposal_id: int) -> dict | None:
    """Read and parse the evaluation_criteria_json column.

    Returns the parsed dict, or None when the column is NULL or the JSON
    is malformed.
    """
    with session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        raw = proposal.evaluation_criteria_json if proposal is not None else None

    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(
            "evaluation_criteria: proposal %d has malformed evaluation_criteria_json.",
            proposal_id,
        )
        return None

    if not isinstance(data, dict):
        return None
    return data


def format_evaluation_criteria_block(
    criteria: dict | None,
    *,
    section_targeted_factor_ids: list[str] | None = None,
) -> str:
    """Render a compact prompt-ready block for Reviewer A's cached prefix.

    Returns empty string when criteria is None or empty (backwards compatible
    — old proposals with NULL evaluation_criteria_json pass through cleanly).

    Args:
        criteria: Parsed evaluation criteria dict (from load_evaluation_criteria).
        section_targeted_factor_ids: Optional list of factor IDs this section
            targets; when provided and non-empty, appended as a trailing block.
    """
    if not criteria:
        return ""

    method = criteria.get("evaluation_method", "unknown")
    factors = criteria.get("factors") or []
    if not method and not factors:
        return ""

    lines = [
        "=== EVALUATION CRITERIA — WHAT THE BUYER ACTUALLY SCORES ===",
        f"Evaluation method: {method}",
    ]

    if factors:
        lines.append("Factors (sorted by weight desc, undisclosed weights last):")

        # Sort: known weight desc, then null-weight in original order.
        # tuple ordering: (1 if weight known, 0 if null) DESC via reverse=True,
        # then weight value DESC via -w. None comes last.
        def _sort_key(f: dict):
            w = f.get("weight_pct")
            return (1 if w is None else 0, -(w or 0))

        sorted_factors = sorted(factors, key=_sort_key)
        for f in sorted_factors:
            fid = f.get("factor_id", "?")
            fname = f.get("factor_name", "?")
            wpct = f.get("weight_pct")
            wdesc = f.get("weight_descriptive")
            scale = f.get("scoring_scale")
            evidence = f.get("evidence_required")
            subfactors = f.get("subfactors") or []

            if wpct is not None:
                weight_str = f"{wpct}%"
            elif wdesc:
                weight_str = wdesc
            else:
                weight_str = "(undisclosed)"

            factor_line = f"  - {fid} {fname} — {weight_str}"
            if scale:
                factor_line += f" — scale: {scale}"
            lines.append(factor_line)
            if evidence:
                lines.append(f"      evidence: {evidence}")
            for sf in subfactors[:10]:  # cap subfactors to avoid wall-of-text
                sf_name = sf.get("name", "?")
                sf_wpct = sf.get("weight_pct")
                sf_notes = sf.get("notes") or ""
                sf_str = f"      subfactor: {sf_name}"
                if sf_wpct is not None:
                    sf_str += f" ({sf_wpct}%)"
                if sf_notes:
                    sf_str += f" — {sf_notes}"
                lines.append(sf_str)
    else:
        lines.append("Factors: (none enumerated by the RFP)")

    sl_map = criteria.get("section_l_to_m_map") or {}
    if sl_map:
        map_repr = json.dumps(sl_map)
        lines.append(f"Compliance-item → factor map: {map_repr}")
    else:
        lines.append("Compliance-item → factor map: (no map disclosed by RFP)")

    trade_off = criteria.get("trade_off_language")
    if trade_off:
        lines.append(f'Trade-off language (verbatim): "{trade_off}"')

    lpta = criteria.get("lowest_price_clause")
    if lpta:
        lines.append(f'Lowest-price clause (verbatim): "{lpta}"')

    notes = criteria.get("extraction_notes")
    if notes:
        lines.append(f"Extraction notes: {notes}")

    if section_targeted_factor_ids:
        ids_str = ", ".join(section_targeted_factor_ids)
        lines.append(f"THIS SECTION'S ASSIGNED FACTORS: {ids_str}")

    return "\n".join(lines)
