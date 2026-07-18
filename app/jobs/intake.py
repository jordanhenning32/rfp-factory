"""Intake pipeline orchestrator.

Runs in a background thread spawned from the New Proposal Run handler.
Stages:
  1. Parse all supported docs in the package — PDF / DOCX / XLSX (text extraction)
  2. Run Compliance Matrix Agent against each parsed document
  3. Persist results; transition proposal to DRAFTING

Each stage updates Proposal.status and Proposal.notes (treated as a working
status line for the Run Progress page polling). Errors get logged and
recorded in agent_runs.error_text via the LLM client wrapper; the proposal
is left in INTAKING so the user can inspect and retry.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.agents.compliance_matrix import (
    ExtractedComplianceItem,
    extract_compliance_items,
)
from app.agents.compliance_validator import validate_compliance_items
from app.agents.section_m_extractor import extract_evaluation_criteria
from app.agents.shortfall_strategist import (
    ShortfallItem,
    analyze_compliance_batch,
    build_cached_prefix,
    make_batches,
)
from app.agents.teaming_researcher import research_partners_for_gap
from app.config import get_settings
from app.core.company_profile import get_company_profile
from app.core.decisions import format_decisions_for_prompt
from app.core.enums import (
    ComplianceStatus,
    GapSeverity,
    ProposalStatus,
    RequirementCategory,
    RequirementType,
)
from app.core.teaming_partners import get_teaming_partners
from app.db.session import session_scope
from app.models import (
    ComplianceMatrixItem,
    GapAnalysis,
    Proposal,
    RfpPackageDocument,
)
from app.services.kb_context import build_shortfall_kb_context
from app.services.pdf_extract import (
    extract_docx_text,
    extract_pdf_text,
    extract_xlsx_text,
)

log = logging.getLogger(__name__)


# Stage-message logger lives in app.services.stages so all four job
# modules share one FK-safe implementation. Aliased to _set_stage so
# the existing call sites in this module stay unchanged.
from app.services.stages import record_stage as _set_stage  # noqa: E402


def _extract_text_for_intake(storage_path: str, filename: str) -> tuple[str, int]:
    """Dispatch by file suffix and return (text, page_count) with the
    `--- Page N ---` markers the compliance-matrix chunker requires.

    DOCX has no native pages: the whole doc is treated as Page 1.
    XLSX: each sheet becomes one page (the extractor's `=== Sheet: X ===`
    headers are rewritten to `--- Page N ---`). PDFs use the existing
    pdfplumber path which already emits page markers.
    """
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(storage_path)
    if suffix == ".docx":
        body, _ = extract_docx_text(storage_path)
        return f"--- Page 1 ---\n{body}", 1
    if suffix == ".xlsx":
        body, sheet_count = extract_xlsx_text(storage_path)
        # Rewrite sheet headers as page markers so the compliance-matrix
        # chunker can split on them. Sheet boundaries are the natural
        # split points for a workbook.
        lines = body.split("\n")
        page_no = 0
        rewritten: list[str] = []
        for line in lines:
            if line.startswith("=== Sheet: ") and line.endswith(" ==="):
                page_no += 1
                sheet_name = line[len("=== Sheet: ") : -len(" ===")]
                rewritten.append(f"--- Page {page_no} ---")
                rewritten.append(f"[Sheet: {sheet_name}]")
            else:
                rewritten.append(line)
        if page_no == 0:
            # Defensive: empty workbook or unexpected header format.
            rewritten = ["--- Page 1 ---", *lines]
            page_no = 1
        return "\n".join(rewritten), page_no
    raise ValueError(f"unsupported file type for intake: {suffix or filename!r}")


def _parse_documents(proposal_id: int) -> int:
    """Extract text from every supported doc in the package (PDF / DOCX /
    XLSX); return count parsed."""
    parsed = 0
    # Read IDs while the session is open — accessing .id on detached
    # instances triggers a refresh and raises DetachedInstanceError.
    with session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        if not proposal or not proposal.rfp_package:
            return 0
        doc_ids = [d.id for d in proposal.rfp_package.documents]

    for doc_id in doc_ids:
        with session_scope() as db:
            doc = db.get(RfpPackageDocument, doc_id)
            if not doc or doc.extracted_text_md:
                continue
            try:
                text, page_count = _extract_text_for_intake(doc.storage_path, doc.filename)
                doc.extracted_text_md = text
                doc.page_count = page_count
                parsed += 1
                log.info(
                    "parsed %s — %d pages, %d chars",
                    doc.filename,
                    page_count,
                    len(text),
                )
            except Exception:
                log.exception("failed to parse %s", doc.filename)
    return parsed


# When the Compliance Matrix Agent occasionally confuses requirement_type
# with category (returning e.g. 'certification' where the schema expects a
# RequirementType), the closest semantic fallback isn't "should" — these
# items typically need a form/document submitted, so map them to
# 'mandatory_form'. Hits about 1% of items per pass; better than logging
# noisy warnings and defaulting to a less-actionable type.
_REQUIREMENT_TYPE_FALLBACKS: dict[str, RequirementType] = {
    "certification": RequirementType.MANDATORY_FORM,
    "administrative": RequirementType.MANDATORY_FORM,
    "personnel": RequirementType.SHALL,
    "past_performance": RequirementType.SHALL,
    "pricing": RequirementType.SUBMISSION_FORMAT,
    "technical": RequirementType.SHALL,
    "management": RequirementType.SHALL,
}

# Mirror table for the inverse drift — TYPE values appearing where a
# RequirementCategory is expected. 'submission_format' / 'mandatory_form'
# are both administrative submissions; 'evaluation_criterion' is usually
# scoring a technical or management dimension; modal verbs default to
# 'technical' since most "shall" requirements in govt RFPs are technical.
_REQUIREMENT_CATEGORY_FALLBACKS: dict[str, RequirementCategory] = {
    "submission_format": RequirementCategory.ADMINISTRATIVE,
    "mandatory_form": RequirementCategory.ADMINISTRATIVE,
    "evaluation_criterion": RequirementCategory.TECHNICAL,
    "shall": RequirementCategory.TECHNICAL,
    "must": RequirementCategory.TECHNICAL,
    "should": RequirementCategory.TECHNICAL,
}


# How many chars to read off the start of a truncated requirement_text
# when probing the source for its full version. Long enough to be
# discriminating, short enough to survive minor whitespace/typo drift.
_REPAIR_PROBE_CHARS = 60

# Maximum chars to read forward from the probe match when reconstructing
# the full sentence. Bounds runaway extraction if the source has weird
# formatting; tuned for the longest typical RFP requirement.
_REPAIR_MAX_FORWARD = 2_500

_REPAIR_MIN_PROBE_CHARS = 18  # too short = unsafe match


def _looks_truncated(text: str) -> bool:
    """Heuristic: requirement_text looks truncated if it ends with the
    ellipsis character or three dots. Empirically Sonnet's truncations
    always include this marker; using it as the sole signal avoids the
    false-positive risk of a "short final word" heuristic (e.g.,
    legitimate text ending with "data" or "year")."""
    t = (text or "").rstrip()
    if not t:
        return False
    return t.endswith("…") or t.endswith("...")


def _strip_trailing_ellipsis(text: str) -> str:
    """Remove a trailing "..." or "…" so we can probe the source with
    the actual prefix."""
    t = text.rstrip()
    while t.endswith("…") or t.endswith("..."):
        if t.endswith("…"):
            t = t[:-1].rstrip()
        else:
            t = t[:-3].rstrip()
    return t


def _find_full_sentence_in_source(
    truncated_text: str,
    source_text: str,
) -> str | None:
    """Locate the start of `truncated_text` in `source_text` and return
    the full sentence(s) starting at that position. Returns None if we
    can't find a confident match.

    Sentence-end detection (in priority order):
      1. paragraph break ("\\n\\n")
      2. period followed by newline + capital letter (new bullet/sentence)
      3. period at end of line (".\\n")
      4. closing punctuation (.!?) followed by whitespace + capital letter

    Capped at _REPAIR_MAX_FORWARD chars to avoid runaway capture.
    """
    if not truncated_text or not source_text:
        return None
    probe_source = _strip_trailing_ellipsis(truncated_text).strip()
    if len(probe_source) < _REPAIR_MIN_PROBE_CHARS:
        return None

    # Use the START of the truncated text as the probe — that's the part
    # the model copied verbatim before chopping the tail.
    probe = probe_source[:_REPAIR_PROBE_CHARS]

    # Try exact match first; fall back to whitespace-collapsed match for
    # multi-line tolerance.
    idx = source_text.find(probe)
    if idx == -1:
        # Collapse whitespace on both sides — handles cases where the
        # model joined lines that pdfplumber kept separate.
        ws_collapsed_source = re.sub(r"\s+", " ", source_text)
        ws_collapsed_probe = re.sub(r"\s+", " ", probe)
        ci = ws_collapsed_source.find(ws_collapsed_probe)
        if ci == -1:
            return None
        # Map collapsed-index back to original-index by walking forward.
        # Conservative: just return None — risk of misaligned indices is
        # higher than the value of catching this edge case.
        return None

    # Walk forward from the match to find the sentence end.
    end_search_start = idx + len(probe_source)  # past everything we already have
    forward = source_text[end_search_start : end_search_start + _REPAIR_MAX_FORWARD]

    # Look for the earliest strong sentence boundary in `forward`.
    candidates: list[int] = []

    # Paragraph break — strongest signal
    pb = forward.find("\n\n")
    if pb != -1:
        candidates.append(pb)

    # Period at end of line followed by something non-letter on next
    # line (a new bullet, blank line, or capital letter starting a new
    # sentence).
    for m in re.finditer(r"\.\s*\n+\s*([A-Z•\-•]|\d+\.)", forward):
        candidates.append(m.start() + 1)  # include the period
        break  # earliest match wins

    # Period followed by space + Capital letter (sentence boundary on
    # the same line).
    for m in re.finditer(r"\.\s+[A-Z]", forward):
        # Skip abbreviations that look like sentence boundaries.
        # If the period is preceded by a single capital letter we
        # treat it as an abbreviation and skip.
        period_idx = m.start()
        prev_chars = forward[max(0, period_idx - 3) : period_idx]
        if prev_chars and len(prev_chars) >= 2 and prev_chars[-1].isupper() and not prev_chars[-2].isalpha():
            continue
        candidates.append(period_idx + 1)
        break

    if not candidates:
        # Conservative — don't try to extract beyond what we can confirm
        # ends a sentence. Return None to leave the text alone.
        return None

    end_offset = min(candidates)
    full_text = source_text[idx : end_search_start + end_offset]
    # Tidy up trailing whitespace.
    full_text = full_text.rstrip()
    return full_text


def _repair_truncated_items(
    items: Iterable[ExtractedComplianceItem],
    source_text: str,
) -> None:
    """In-place repair pass: for each item that looks truncated, locate
    its start in `source_text` and replace requirement_text with the
    full sentence. No-op for items that look fine, can't be located,
    or where the source equally appears truncated. Logged at INFO so
    the user sees how much was repaired."""
    if not source_text:
        return
    repaired = 0
    skipped = 0
    items_list = list(items)
    for item in items_list:
        if not _looks_truncated(item.requirement_text):
            continue
        repaired_text = _find_full_sentence_in_source(
            item.requirement_text,
            source_text,
        )
        if repaired_text is None:
            skipped += 1
            continue
        if len(repaired_text) > len(item.requirement_text) and not _looks_truncated(repaired_text):
            log.info(
                "truncation repair: %s %d -> %d chars",
                item.requirement_id,
                len(item.requirement_text),
                len(repaired_text),
            )
            item.requirement_text = repaired_text
            repaired += 1
        else:
            skipped += 1

    if repaired or skipped:
        log.info(
            "truncation repair: %d repaired, %d still flagged "
            "(probe not found in source / source also looked truncated)",
            repaired,
            skipped,
        )


def _persist_compliance_items(
    proposal_id: int, source_doc: str, items: Iterable[ExtractedComplianceItem]
) -> int:
    """Save items to the DB. De-coerce string enums to the model enum types."""
    saved = 0
    with session_scope() as db:
        for item in items:
            try:
                rtype = RequirementType(item.requirement_type)
            except ValueError:
                key = (item.requirement_type or "").lower().strip()
                rtype = _REQUIREMENT_TYPE_FALLBACKS.get(key, RequirementType.SHOULD)
                log.warning(
                    "unknown requirement_type %r — defaulting to %r",
                    item.requirement_type,
                    rtype.value,
                )
            try:
                cat = RequirementCategory(item.category)
            except ValueError:
                key = (item.category or "").lower().strip()
                cat = _REQUIREMENT_CATEGORY_FALLBACKS.get(
                    key,
                    RequirementCategory.ADMINISTRATIVE,
                )
                log.warning(
                    "unknown category %r — defaulting to %r",
                    item.category,
                    cat.value,
                )

            db.add(
                ComplianceMatrixItem(
                    proposal_id=proposal_id,
                    requirement_id=item.requirement_id,
                    requirement_text=item.requirement_text,
                    source_doc=source_doc,
                    source_section=item.source_section,
                    source_page=item.source_page,
                    requirement_type=rtype,
                    category=cat,
                    weight=item.weight,
                    compliance_status=ComplianceStatus.TO_BE_DRAFTED,
                )
            )
            saved += 1
    return saved


def _extract_one_doc_for_matrix(
    proposal_id: int,
    doc: dict,
) -> tuple[str, list]:
    """Worker — extract + repair + validate for a single RFP doc.
    Returns (filename, items). Persistence happens in the main
    thread to keep DB writes serialized."""
    result = extract_compliance_items(
        document_text=doc["text"],
        filename=doc["filename"],
        proposal_id=proposal_id,
    )
    items = result.items
    # Deterministic repair for residual truncation. The drafter
    # sometimes still ends requirement_text with "…" mid-word
    # despite the prompt rule; this pass finds the truncated start
    # in the source PDF and copies the full sentence. Pure
    # substring + sentence-boundary detection — no LLM call. Runs
    # BEFORE the validator so the validator sees clean text.
    try:
        _repair_truncated_items(items, doc["text"])
    except Exception:
        log.exception(
            "truncation repair pass failed for %s — continuing with raw extraction.",
            doc["filename"],
        )
    # Cheap Haiku validation pass: catches type/category drift
    # the drafter occasionally lets through (e.g.,
    # requirement_type='certification' which is a category value).
    # HIGH-confidence corrections apply in-place; MEDIUM/LOW
    # surface as warnings without mutating the data. Failures are
    # best-effort — extraction output still persists if validation
    # breaks.
    try:
        _validate_and_apply_corrections(items, proposal_id)
    except Exception:
        log.exception(
            "compliance_validator: pass failed for %s — persisting extraction unchanged.",
            doc["filename"],
        )
    return doc["filename"], items


def _run_compliance_matrix(proposal_id: int) -> int:
    """For each parsed PDF, run the Compliance Matrix Agent and
    persist items. Returns total items saved across all documents.

    Documents are extracted in PARALLEL via ThreadPoolExecutor —
    each doc gets its own worker (capped at shortfall_workers,
    defaulting to 6). Persistence runs back in the main thread
    inside as_completed so DB writes stay serialized. Per-doc
    failures isolate (logged + skipped); remaining docs continue.
    """
    with session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        if not proposal or not proposal.rfp_package:
            return 0
        docs = [
            {"id": d.id, "filename": d.filename, "text": d.extracted_text_md or ""}
            for d in proposal.rfp_package.documents
            if d.extracted_text_md
        ]

    if not docs:
        return 0

    settings = get_settings()
    workers = max(1, min(len(docs), int(settings.shortfall_workers or 1)))
    log.info(
        "compliance_matrix: %d doc(s) × %d worker(s) in parallel",
        len(docs),
        workers,
    )

    total = 0
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"compmat-{proposal_id}",
    ) as executor:
        future_to_doc = {
            executor.submit(
                _extract_one_doc_for_matrix,
                proposal_id,
                d,
            ): d
            for d in docs
        }
        for future in as_completed(future_to_doc):
            d = future_to_doc[future]
            try:
                filename, items = future.result()
            except Exception:
                log.exception(
                    "compliance matrix failed for %s",
                    d["filename"],
                )
                continue
            total += _persist_compliance_items(
                proposal_id,
                filename,
                items,
            )
    return total


def _validate_and_apply_corrections(
    items: list[ExtractedComplianceItem],
    proposal_id: int,
) -> None:
    """Run the Haiku validator on the extracted items and mutate them
    in-place for HIGH-confidence corrections. MEDIUM / LOW issues are
    logged + staged but the items are not modified — the user sees the
    flag and can edit manually if needed.

    No-op when `items` is empty.
    """
    if not items:
        return

    _set_stage(
        proposal_id,
        f"Validating compliance matrix ({len(items)} items, Haiku, ~$0.05)…",
    )

    item_dicts = [
        {
            "requirement_id": it.requirement_id,
            "requirement_text": it.requirement_text,
            "requirement_type": it.requirement_type,
            "category": it.category,
        }
        for it in items
    ]
    results = validate_compliance_items(item_dicts, proposal_id=proposal_id)

    if not results:
        log.info(
            "compliance_validator: %d item(s) audited, no issues found.",
            len(items),
        )
        _set_stage(
            proposal_id,
            f"Validation: {len(items)} items checked, no issues.",
        )
        return

    # Index items by ID for O(1) lookup during apply.
    items_by_id = {it.requirement_id: it for it in items}

    n_auto_applied = 0
    n_warned = 0
    n_blocked = 0
    n_dropped_noop = 0
    for r in results:
        item = items_by_id.get(r.requirement_id)
        if item is None:
            log.warning(
                "compliance_validator: result references unknown REQ-ID %r — skipping",
                r.requirement_id,
            )
            continue

        # Drop no-op suggestions: validator occasionally returns a
        # "type_misclassified" / "category_misclassified" issue where
        # the suggested value equals the current value. Nothing to do
        # and the warn-path log message ("type_misclassified") is
        # actively misleading because the type is correct.
        type_is_noop = not r.suggested_type or r.suggested_type == item.requirement_type
        cat_is_noop = not r.suggested_category or r.suggested_category == item.category
        # Only drop when the issue is one of the misclassification kinds;
        # text/header/truncation/duplicate flags don't carry suggestions
        # but are still worth surfacing.
        misclass_issues = {
            "type_misclassified",
            "category_misclassified",
            "type_and_category_misclassified",
        }
        if r.issue in misclass_issues and type_is_noop and cat_is_noop:
            n_dropped_noop += 1
            log.debug(
                "compliance_validator: dropping no-op suggestion on %s "
                "(suggested type/category match current values)",
                r.requirement_id,
            )
            continue

        confidence = (r.confidence or "").upper()
        applied: list[str] = []
        blocked_flip = False
        if confidence == "HIGH":
            if r.suggested_type and r.suggested_type != item.requirement_type:
                if _is_unsupported_verb_strictness_flip(
                    item.requirement_text,
                    item.requirement_type,
                    r.suggested_type,
                ):
                    # Defense-in-depth: even if the prompt slips, refuse
                    # to auto-apply a {shall,must,should} flip when the
                    # target verb isn't in the visible text. The upstream
                    # Compliance Matrix Agent had full PDF context; the
                    # validator only sees the extracted snippet.
                    blocked_flip = True
                    n_blocked += 1
                    log.warning(
                        "compliance_validator: BLOCKED type %r->%r on %s "
                        "(target verb not in requirement_text; reason=%r)",
                        item.requirement_type,
                        r.suggested_type,
                        r.requirement_id,
                        r.reason,
                    )
                else:
                    old = item.requirement_type
                    item.requirement_type = r.suggested_type
                    applied.append(f"type {old!r}->{r.suggested_type!r}")
            if r.suggested_category and r.suggested_category != item.category:
                old = item.category
                item.category = r.suggested_category
                applied.append(f"category {old!r}->{r.suggested_category!r}")

        if applied:
            n_auto_applied += 1
            log.info(
                "compliance_validator: AUTO-APPLIED %s on %s (issue=%s, reason=%s)",
                ", ".join(applied),
                r.requirement_id,
                r.issue,
                r.reason,
            )
        elif not blocked_flip:
            n_warned += 1
            log.warning(
                "compliance_validator: %s [%s/%s] reason=%r (suggested_type=%r, suggested_category=%r)",
                r.requirement_id,
                confidence,
                r.issue,
                r.reason,
                r.suggested_type,
                r.suggested_category,
            )

    blocked_part = f", {n_blocked} unsupported flip(s) blocked" if n_blocked else ""
    noop_part = f", {n_dropped_noop} no-op suggestion(s) dropped" if n_dropped_noop else ""
    summary = (
        f"Validation: {n_auto_applied} auto-fix(es), "
        f"{n_warned} item(s) flagged for review{blocked_part}{noop_part}."
    )
    log.info("compliance_validator: %s", summary)
    _set_stage(proposal_id, summary)


# Mandatory-verb tokens that justify a `shall` / `must` classification
# when present in the requirement_text itself. Word-boundary matched
# (case-insensitive) so "musty" / "shallow" don't false-match.
_MANDATORY_VERB_RE = re.compile(
    r"\b(shall|must|is required to|are required to|will be required)\b",
    re.IGNORECASE,
)
_VERB_STRICTNESS_TYPES = {"shall", "must", "should"}


def _is_unsupported_verb_strictness_flip(text: str, current_type: str, suggested_type: str) -> bool:
    """True if this is a {shall,must,should} ↔ {shall,must,should} flip
    where the target verb isn't visible in the requirement_text. Such
    flips defer to the upstream Compliance Matrix Agent, which had full
    PDF context that the validator does not see."""
    if current_type not in _VERB_STRICTNESS_TYPES:
        return False
    if suggested_type not in _VERB_STRICTNESS_TYPES:
        return False
    # `should` doesn't need a mandatory verb in the text; flips toward
    # `should` (loosening) are always safe to apply when HIGH-confidence.
    if suggested_type == "should":
        return False
    # Tightening to shall/must requires the verb in the visible text.
    return not _MANDATORY_VERB_RE.search(text or "")


def _persist_shortfall_results(
    proposal_id: int,
    results: list[ShortfallItem],
    req_id_to_pk: dict[str, int],
) -> tuple[int, bool]:
    """Persist shortfall results: update each compliance item's status and
    create GapAnalysis rows for partial/gap verdicts.

    Returns (gap_rows_created, any_no_bid_recommended).
    """
    saved = 0
    no_bid = False

    with session_scope() as db:
        # Continue gap_id numbering from any existing rows for this proposal.
        existing_nums: list[int] = []
        for g in db.query(GapAnalysis).filter(GapAnalysis.proposal_id == proposal_id).all():
            try:
                existing_nums.append(int(g.gap_id.replace("GAP-", "")))
            except (ValueError, AttributeError):
                pass
        next_idx = max(existing_nums) + 1 if existing_nums else 1

        for r in results:
            comp_pk = req_id_to_pk.get(r.requirement_id)
            if comp_pk is None:
                log.warning(
                    "shortfall: requirement_id %r not in compliance matrix — skipping",
                    r.requirement_id,
                )
                continue

            comp = db.get(ComplianceMatrixItem, comp_pk)
            if comp is not None:
                if r.verdict == "met":
                    comp.compliance_status = ComplianceStatus.REVIEWED_PASS
                else:
                    comp.compliance_status = ComplianceStatus.GAP_FLAGGED

            if r.verdict == "met":
                continue

            sev_str = r.gap_severity or ("minor" if r.verdict == "partial" else "major")
            try:
                sev = GapSeverity(sev_str)
            except ValueError:
                sev = GapSeverity.MINOR

            if r.no_bid_recommended:
                no_bid = True

            db.add(
                GapAnalysis(
                    proposal_id=proposal_id,
                    requirement_id_fk=comp_pk,
                    gap_id=f"GAP-{next_idx:03d}",
                    gap_severity=sev,
                    gap_description=r.current_state[:1000] or "(no description)",
                    current_state=r.current_state,
                    mitigation_options_json=r.mitigation_options,
                    recommended_mitigation_index=r.recommended_mitigation_index,
                )
            )
            next_idx += 1
            saved += 1

    return saved, no_bid


def _run_shortfall_strategist(proposal_id: int) -> tuple[int, bool]:
    """Run the Shortfall Strategist over every compliance item for a proposal.

    Returns (gap_rows_created, any_no_bid_recommended).
    """
    profile_json = json.dumps(get_company_profile(), indent=2)
    teaming_partners_json = json.dumps(get_teaming_partners(), indent=2)
    decisions_text = format_decisions_for_prompt()
    kb_context = build_shortfall_kb_context()
    cached_prefix = build_cached_prefix(
        profile_json=profile_json,
        teaming_partners_json=teaming_partners_json,
        decisions_text=decisions_text,
        kb_context=kb_context,
    )

    with session_scope() as db:
        # Active rows only — shortfall must not gap-analyze against
        # superseded or removed requirements.
        items = (
            db.query(ComplianceMatrixItem)
            .filter(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
            .order_by(ComplianceMatrixItem.id)
            .all()
        )
        item_dicts = [
            {
                "pk": i.id,
                "requirement_id": i.requirement_id,
                "requirement_text": i.requirement_text,
                "requirement_type": i.requirement_type.value
                if hasattr(i.requirement_type, "value")
                else str(i.requirement_type),
                "category": i.category.value if hasattr(i.category, "value") else str(i.category),
                "source_doc": i.source_doc,
                "source_section": i.source_section,
                "source_page": i.source_page,
                "weight": float(i.weight) if i.weight is not None else None,
            }
            for i in items
        ]

    if not item_dicts:
        log.warning("shortfall: no compliance items for proposal %d", proposal_id)
        return 0, False

    req_id_to_pk = {d["requirement_id"]: d["pk"] for d in item_dicts}

    # Filter out items the Shortfall Strategist always returns "no_gap"
    # for. Pure submission rules (page limits, font size, due date
    # format) aren't capability gaps — they're checklist items that
    # surface on the Submission Checklist tab. Skipping them saves
    # ~$0.20 + ~3min per ~25 items dropped, with zero downside (the
    # rest of the pipeline doesn't depend on them having gap_analyses).
    eligible_dicts = [
        d for d in item_dicts if d["requirement_type"] != RequirementType.SUBMISSION_FORMAT.value
    ]
    n_skipped = len(item_dicts) - len(eligible_dicts)
    if n_skipped:
        log.info(
            "shortfall: skipping %d submission_format item(s) — those are "
            "checklist items, not capability gaps.",
            n_skipped,
        )

    if not eligible_dicts:
        log.info("shortfall: no eligible items after filter; nothing to analyze")
        return 0, False

    batches = make_batches(eligible_dicts)
    settings = get_settings()
    workers = max(1, int(settings.shortfall_workers or 1))

    log.info(
        "shortfall: %d eligible items across %d batch(es) for proposal %d (%d worker(s))",
        len(eligible_dicts),
        len(batches),
        proposal_id,
        workers,
    )
    _set_stage(
        proposal_id,
        f"Shortfall analysis: {len(batches)} batch(es) × {workers} "
        f"worker(s) in parallel ({len(eligible_dicts)} items"
        + (f"; {n_skipped} submission_format items skipped" if n_skipped else "")
        + ")…",
    )

    total_gaps = 0
    any_no_bid = False
    failed_batches: list[int] = []
    completed = 0

    # Run batches concurrently. Each batch sends the same cached_prefix —
    # whichever batch lands first writes the Anthropic prompt cache; the
    # rest read it (~10% input cost on those tokens). Each batch's
    # persistence runs only after the LLM call returns, so workers don't
    # contend on the same gap_analyses rows.
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"shortfall-{proposal_id}",
    ) as executor:
        future_to_idx = {
            executor.submit(
                analyze_compliance_batch,
                proposal_id=proposal_id,
                requirements=batch,
                cached_prefix=cached_prefix,
            ): idx
            for idx, batch in enumerate(batches, 1)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            batch = batches[idx - 1]
            completed += 1
            try:
                results = future.result()
            except Exception as exc:
                log.exception(
                    "shortfall batch %d failed for proposal %d",
                    idx,
                    proposal_id,
                )
                failed_batches.append(idx)
                _set_stage(
                    proposal_id,
                    f"⚠ Shortfall batch {idx}/{len(batches)} failed "
                    f"({len(batch)} items skipped): "
                    f"{type(exc).__name__}: {str(exc)[:140]}. "
                    f"Re-run shortfall analysis from Proposal Review when "
                    f"intake completes.",
                )
                continue
            gaps, no_bid = _persist_shortfall_results(
                proposal_id,
                results,
                req_id_to_pk,
            )
            total_gaps += gaps
            any_no_bid = any_no_bid or no_bid
            _set_stage(
                proposal_id,
                f"Shortfall analysis: batch {idx}/{len(batches)} done "
                f"({completed}/{len(batches)} complete) — "
                f"{len(results)} analyses, {gaps} gap(s)",
            )

    if failed_batches:
        _set_stage(
            proposal_id,
            f"Shortfall pipeline finished with {len(failed_batches)} "
            f"failed batch(es) (#{', #'.join(str(b) for b in failed_batches)}). "
            f"Use 'Re-run shortfall analysis' on Proposal Review to fill the gaps.",
        )
    return total_gaps, any_no_bid


# Approaches we treat as "teaming-style" — anything starting with
# "teaming" (e.g., "teaming with X") AND any approach where the upstream
# Strategist already populated partner_suggestions. Belt-and-suspenders.
def _is_teaming_option(opt: dict) -> bool:
    approach = (opt.get("approach") or "").strip().lower()
    if approach.startswith("teaming"):
        return True
    if opt.get("partner_suggestions"):
        return True
    return False


def _quadratic_summary_for_research() -> str:
    """Compact firm summary for the Teaming Researcher prompt. Pulled
    from the canonical company profile so it stays in sync."""
    profile = get_company_profile()
    bits: list[str] = []
    name = profile.get("legal_name") or profile.get("name") or "Quadratic Digital"
    bits.append(name)
    if loc := profile.get("hq_location") or profile.get("headquarters"):
        bits.append(f"HQ: {loc}")
    if size := profile.get("employee_count") or profile.get("size"):
        bits.append(f"Size: {size}")
    if focus := profile.get("market_focus") or profile.get("focus"):
        bits.append(f"Focus: {focus}")
    if certs := profile.get("certifications"):
        if isinstance(certs, list) and certs:
            bits.append(f"Certifications: {', '.join(str(c) for c in certs[:6])}")
    if vehicles := profile.get("contract_vehicles"):
        if isinstance(vehicles, list) and vehicles:
            bits.append(f"Contract vehicles: {', '.join(str(v) for v in vehicles[:6])}")
    bits.append(
        "Competitive edge: rapid AI-assisted custom software development for state and federal agencies."
    )
    return ". ".join(bits)


def _enrich_teaming_partners(proposal_id: int) -> None:
    """For each GapAnalysis row with a teaming-style mitigation, run
    the DUAL teaming-research pipeline:
      Pass A — Gemini Pro + Google Search grounding
      Pass B — Claude Sonnet 4.6 + web_search_20250305 tool
      Consolidate — pure-Python merge by canonicalized firm name;
                    annotates each partner with confirmed_by[] and
                    bumps confidence one tier on cross-provider
                    agreement.

    Cross-provider agreement is itself evidence the firm is real and
    fits — single-provider partners get `needs_review: True` so the UI
    can flag them for the user to verify before reaching out.

    Best-effort per gap. Per-provider failures degrade gracefully
    (single-provider results still surface, just without the consensus
    boost). Runs gaps in parallel; each gap fans out the two providers
    in parallel internally so wall-clock per gap is max(A, B), not A+B.
    """
    settings = get_settings()
    workers = max(1, int(settings.shortfall_workers or 1))

    # Snapshot proposal context + gaps. We hold primitives only after
    # session_scope exit — never the ORM rows.
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return
        rfp_title = prop.title or ""
        rfp_agency = prop.agency or ""
        rfp_scope = (prop.notes or "").strip()[:600]

        gap_rows = db.query(GapAnalysis).filter(GapAnalysis.proposal_id == proposal_id).all()
        # Pull requirement_text via the relationship while we're still in
        # the session.
        gap_snapshots: list[dict] = []
        for g in gap_rows:
            req_text = ""
            req_id = ""
            if g.requirement is not None:
                req_text = g.requirement.requirement_text or ""
                req_id = g.requirement.requirement_id or ""
            gap_snapshots.append(
                {
                    "pk": g.id,
                    "gap_id": g.gap_id,
                    "severity": (
                        g.gap_severity.value if hasattr(g.gap_severity, "value") else str(g.gap_severity)
                    ),
                    "requirement_id": req_id,
                    "requirement_text": req_text,
                    "current_state": g.current_state or "",
                    "mitigation_options": list(g.mitigation_options_json or []),
                }
            )

    # Filter to gaps with at least one teaming-style option.
    teaming_gaps = [
        gs for gs in gap_snapshots if any(_is_teaming_option(opt) for opt in gs["mitigation_options"])
    ]
    if not teaming_gaps:
        log.info(
            "teaming_researcher: no teaming-style mitigations on proposal %d — skipping market research.",
            proposal_id,
        )
        return

    quadratic_summary = _quadratic_summary_for_research()

    _set_stage(
        proposal_id,
        f"Teaming research: {len(teaming_gaps)} teaming gap(s) × "
        f"{workers} worker(s) (Gemini Pro + Claude+web_search dual)…",
    )

    # Run gaps in parallel.
    completed = 0
    failed: list[str] = []
    enriched_by_gap_pk: dict[int, list[dict]] = {}

    from app.agents.teaming_consolidator import (
        consolidate_partner_research,
    )
    from app.agents.teaming_researcher import TeamingPartnerResearch
    from app.agents.teaming_researcher_claude import (
        research_partners_for_gap_claude,
    )

    def _research_one(gs: dict) -> tuple[int, list[dict]]:
        # Pull only the partner_suggestions from teaming options to give
        # the researcher visibility into what's already been proposed.
        prior_partners: list[dict] = []
        for opt in gs["mitigation_options"]:
            if _is_teaming_option(opt):
                prior_partners.extend(opt.get("partner_suggestions") or [])

        common_kwargs = dict(
            gap_id=gs["gap_id"],
            gap_severity=gs["severity"],
            requirement_id=gs["requirement_id"],
            requirement_text=gs["requirement_text"],
            current_state=gs["current_state"],
            strategist_partner_suggestions=prior_partners,
            rfp_title=rfp_title,
            rfp_agency=rfp_agency,
            rfp_scope=rfp_scope,
            quadratic_summary=quadratic_summary,
            proposal_id=proposal_id,
        )

        # Fan out the two providers concurrently. Each gap takes
        # max(A, B) wall-clock instead of A+B. Per-provider failures
        # degrade gracefully — we still consolidate with whatever
        # results we got, so a transient Gemini outage doesn't lose
        # the gap entirely.
        empty_result = TeamingPartnerResearch(
            gap_id=gs["gap_id"],
            partners=[],
            citations=[],
            cost_usd=0.0,
        )
        with ThreadPoolExecutor(max_workers=2) as inner:
            fut_a = inner.submit(research_partners_for_gap, **common_kwargs)
            fut_b = inner.submit(
                research_partners_for_gap_claude,
                **common_kwargs,
            )
            try:
                pass_a = fut_a.result()
            except Exception:
                log.exception(
                    "teaming_researcher_a (gemini): gap=%s failed; consolidating with B-only results.",
                    gs["gap_id"],
                )
                pass_a = empty_result
            try:
                pass_b = fut_b.result()
            except Exception:
                log.exception(
                    "teaming_researcher_b (claude): gap=%s failed; consolidating with A-only results.",
                    gs["gap_id"],
                )
                pass_b = empty_result

        consolidated = consolidate_partner_research(
            gap_id=gs["gap_id"],
            pass_a=pass_a,
            pass_b=pass_b,
        )
        return gs["pk"], consolidated.partners

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"teaming-{proposal_id}",
    ) as ex:
        future_to_gs = {ex.submit(_research_one, gs): gs for gs in teaming_gaps}
        for future in as_completed(future_to_gs):
            gs = future_to_gs[future]
            try:
                pk, partners = future.result()
            except Exception:
                log.exception(
                    "teaming_researcher: gap=%s failed",
                    gs["gap_id"],
                )
                failed.append(gs["gap_id"])
                continue
            enriched_by_gap_pk[pk] = partners
            completed += 1
            _set_stage(
                proposal_id,
                f"Teaming research: {completed}/{len(teaming_gaps)} "
                f"gap(s) — {gs['gap_id']} → {len(partners)} candidate(s)",
            )

    # Merge enriched partner_suggestions back into mitigation_options.
    # Strategy: replace partner_suggestions on each teaming option with
    # the Gemini-researched list. Strategist's library entries are
    # preserved by transcribing matching names from the OLD list.
    if enriched_by_gap_pk:
        with session_scope() as db:
            for pk, new_partners in enriched_by_gap_pk.items():
                g = db.get(GapAnalysis, pk)
                if g is None:
                    continue
                opts = list(g.mitigation_options_json or [])
                for opt in opts:
                    if not _is_teaming_option(opt):
                        continue
                    # Carry forward any library-confirmed entries from
                    # the Strategist's list — they're real teaming
                    # relationships we don't want Gemini to drop.
                    library_keepers = [
                        p
                        for p in (opt.get("partner_suggestions") or [])
                        if p.get("from_library") and p.get("confirmed")
                    ]
                    library_names = {(p.get("name") or "").lower() for p in library_keepers}
                    deduped_new = [
                        p for p in new_partners if (p.get("name") or "").lower() not in library_names
                    ]
                    opt["partner_suggestions"] = library_keepers + deduped_new
                g.mitigation_options_json = opts

    summary = f"Teaming research complete: {completed}/{len(teaming_gaps)} gap(s) enriched"
    if failed:
        summary += f"; {len(failed)} failed (#{', #'.join(failed)})"
    _set_stage(proposal_id, summary)


# Approaches the Strategist generates that are SAFE to auto-resolve when
# they're the only option. These are honest by construction or by
# definition:
#   - self-perform               — Quadratic does it themselves
#   - custom-build               — transparent about building from scratch
#   - acknowledge-and-risk-frame — explicit honest disclosure
# Other approaches always require human review:
#   - teaming                    — partner confirmation per honesty rule
#   - equivalent-experience      — defensibility judgment
#   - in-progress                — concrete-plan check
#   - no-bid                     — strategic decision
_AUTO_SAFE_APPROACHES = frozenset({"self-perform", "custom-build", "acknowledge-and-risk-frame"})


def _auto_resolve_obvious_gaps(proposal_id: int) -> int:
    """Auto-select the only mitigation option on gaps where there's no
    decision to make: exactly one option AND that option's approach is
    in _AUTO_SAFE_APPROACHES AND its proposal_language_draft has no
    unresolved [NEEDS_HUMAN] markers. Returns the count auto-selected.

    Honest by construction — these gaps have a single honest path; the
    user clicking 'Choose this' adds nothing. Anything ambiguous (teaming
    requiring partner confirm, in-progress requiring plan check, etc.)
    is left for human review. Skips gaps the user has already touched.
    """
    n = 0
    auto_resolved_ids: list[str] = []
    with session_scope() as db:
        gaps = (
            db.query(GapAnalysis)
            .filter(
                GapAnalysis.proposal_id == proposal_id,
                GapAnalysis.selected_mitigation_index.is_(None),
                GapAnalysis.resolved == False,  # noqa: E712
            )
            .all()
        )
        for g in gaps:
            opts = list(g.mitigation_options_json or [])
            if len(opts) != 1:
                continue
            opt = opts[0]
            approach = (opt.get("approach") or "").strip()
            if approach not in _AUTO_SAFE_APPROACHES:
                continue
            language = opt.get("proposal_language_draft") or ""
            if "[NEEDS_HUMAN" in language:
                continue
            g.selected_mitigation_index = 0
            n += 1
            auto_resolved_ids.append(g.gap_id)
    if n:
        log.info(
            "intake: auto-selected mitigation on %d obvious gap(s) for "
            "proposal %d (single honest option): %s",
            n,
            proposal_id,
            ", ".join(auto_resolved_ids),
        )
    return n


# Word-boundary patterns for COTS-leaning RFP signals. Case-sensitive on
# COTS (the lowercase word means "small bed" — never the procurement
# acronym); case-insensitive on the spelled-out forms because RFPs
# capitalize inconsistently.
_COTS_ACRONYM_RE = re.compile(r"\bCOTS\b")
_OFF_THE_SHELF_RE = re.compile(r"\b(?:commercial[\s-]+)?off[\s-]the[\s-]shelf\b", re.IGNORECASE)


def _detect_cots_orientation(proposal_id: int) -> None:
    """Scan the parsed RFP text for COTS / off-the-shelf signals and set
    proposals.cots_orientation accordingly. Pure regex — no LLM. Runs
    after PDF parsing so the flag is visible to every downstream stage.

    Detection is deliberately conservative: only the strong acronym
    forms ('COTS', 'commercial off-the-shelf', 'off-the-shelf') count.
    Avoids false positives on generic 'commercially available' phrasing
    that doesn't actually signal a COTS preference."""
    with session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        if not proposal or not proposal.rfp_package:
            return
        texts = [d.extracted_text_md or "" for d in proposal.rfp_package.documents]

    cots_hits = 0
    ots_hits = 0
    for t in texts:
        cots_hits += len(_COTS_ACRONYM_RE.findall(t))
        ots_hits += len(_OFF_THE_SHELF_RE.findall(t))
    is_cots = cots_hits > 0 or ots_hits > 0

    with session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        if proposal:
            proposal.cots_orientation = is_cots

    if is_cots:
        msg = (
            f"COTS-orientation detected ({cots_hits} 'COTS' / "
            f"{ots_hits} 'off-the-shelf' mention(s)) — writer will lead "
            f"with COTS-equivalence positioning."
        )
    else:
        msg = "No COTS-orientation signals detected."
    log.info("intake: %s", msg)
    _set_stage(proposal_id, msg)


def _run_section_m_extractor(proposal_id: int) -> int:
    """Extract Section M evaluation criteria and persist to the proposal row.

    Snapshots document text and compliance items, calls the extractor,
    persists JSON, and returns the number of factors extracted.
    Returns 0 when no documents with text are available.
    """
    import json as _json

    doc_snapshots: list[dict] = []
    compliance_items: list[dict] = []

    with session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        if not proposal or not proposal.rfp_package:
            return 0
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

    docs_with_text = [s for s in doc_snapshots if s["extracted_text_md"]]
    if not docs_with_text:
        log.warning(
            "section_m: proposal %d has no extracted document text — skipping.",
            proposal_id,
        )
        return 0

    body_parts: list[str] = []
    first_filename = docs_with_text[0]["filename"]
    for snap in docs_with_text:
        body_parts.append(f"\n--- RFP FILE: {snap['filename']} ---\n{snap['extracted_text_md']}\n")

    concatenated = "".join(body_parts)

    result = extract_evaluation_criteria(
        proposal_id=proposal_id,
        document_text=concatenated,
        filename=first_filename,
        compliance_items=compliance_items or None,
    )

    with session_scope() as db:
        proposal = db.get(Proposal, proposal_id)
        if proposal is not None:
            proposal.evaluation_criteria_json = _json.dumps(result.as_dict())

    return len(result.factors)


def run_intake_pipeline(proposal_id: int) -> None:
    """Full intake. Runs in a background thread. Sync end-to-end.

    Stages:
      1. Parse PDFs (pdfplumber, DOCX, XLSX via the dispatcher).
      2. Compliance Matrix Agent (Sonnet) per RFP doc.
      3. Shortfall Strategist (Sonnet, batched, cached prefix) over every item.
    Status flips: intaking → awaiting_scope_signoff (the design doc's
    human-review gate before further drafting / pricing).
    """
    log.info("intake pipeline starting for proposal %d", proposal_id)
    try:
        _set_stage(proposal_id, "Parsing PDFs…")
        parsed = _parse_documents(proposal_id)
        _set_stage(proposal_id, f"Parsed {parsed} document(s).")

        _detect_cots_orientation(proposal_id)

        _set_stage(proposal_id, "Extracting compliance matrix (Sonnet)…")
        items = _run_compliance_matrix(proposal_id)
        _set_stage(proposal_id, f"Compliance matrix: {items} item(s) extracted.")

        try:
            _set_stage(proposal_id, "Extracting evaluation criteria (Section M)\u2026")
            n_factors = _run_section_m_extractor(proposal_id)
            _set_stage(proposal_id, f"Evaluation criteria: {n_factors} factor(s) extracted.")
        except Exception:
            log.exception(
                "section_m extraction failed for proposal %d \u2014 continuing with intake.",
                proposal_id,
            )
            _set_stage(
                proposal_id,
                "\u26a0 Evaluation criteria extraction failed \u2014 see logs. "
                "Re-run from the Evaluation Criteria tab.",
            )

        if items > 0:
            _set_stage(proposal_id, "Running shortfall analysis (Sonnet)\u2026")
            gaps, no_bid = _run_shortfall_strategist(proposal_id)
            msg = f"Shortfall analysis: {gaps} gap(s) flagged."
            if no_bid:
                msg += " ⚠ Deal-breaker(s) detected — see Gaps tab."
            _set_stage(proposal_id, msg)

            # NOTE: Teaming Researcher (Gemini Pro grounded) NO LONGER
            # runs automatically here. It only makes sense to spend
            # ~$0.05/gap of Gemini cost when the user has actually
            # decided to pursue teaming on at least one gap. The user
            # triggers it on demand from the Gaps tab → "Teaming
            # partners" sub-tab via the "Run Teaming Research" button
            # (see run_teaming_research_only / spawn_teaming_research
            # below). Self-perform-everywhere proposals now skip the
            # teaming cost entirely.

            # Auto-resolve gaps where there's no real decision to make:
            # single mitigation option AND honest-by-construction approach
            # AND no [NEEDS_HUMAN] markers. Saves the user from clicking
            # 'Choose this' on gaps with one honest path. Best-effort.
            try:
                n_auto = _auto_resolve_obvious_gaps(proposal_id)
                if n_auto:
                    _set_stage(
                        proposal_id,
                        f"Auto-selected mitigation on {n_auto} gap(s) with a single honest option.",
                    )
            except Exception:
                log.exception(
                    "auto-resolve obvious gaps failed for proposal %d — "
                    "user can still pick mitigations manually.",
                    proposal_id,
                )

        with session_scope() as db:
            proposal = db.get(Proposal, proposal_id)
            if proposal:
                proposal.status = ProposalStatus.AWAITING_SCOPE_SIGNOFF
        log.info(
            "intake pipeline complete for proposal %d (%d items)",
            proposal_id,
            items,
        )
    except Exception:
        log.exception("intake pipeline failed for proposal %d", proposal_id)
        _set_stage(proposal_id, "Pipeline failed — check logs.")


def run_shortfall_only(proposal_id: int) -> None:
    """Run just the shortfall stage on an existing proposal. Clears any
    existing GapAnalysis rows for this proposal first so re-runs don't
    accumulate. Used by the 'Run shortfall analysis' button on the
    Proposal Review page."""
    log.info("shortfall-only run starting for proposal %d", proposal_id)
    try:
        _set_stage(proposal_id, "Clearing previous shortfall analysis…")
        with session_scope() as db:
            db.query(GapAnalysis).filter(GapAnalysis.proposal_id == proposal_id).delete(
                synchronize_session=False
            )
            db.query(ComplianceMatrixItem).filter(ComplianceMatrixItem.proposal_id == proposal_id).update(
                {ComplianceMatrixItem.compliance_status: ComplianceStatus.TO_BE_DRAFTED},
                synchronize_session=False,
            )

        _set_stage(proposal_id, "Running shortfall analysis (Sonnet)…")
        gaps, no_bid = _run_shortfall_strategist(proposal_id)
        msg = f"Shortfall analysis: {gaps} gap(s) flagged."
        if no_bid:
            msg += " ⚠ Deal-breaker(s) detected — see Gaps tab."
        _set_stage(proposal_id, msg)

        with session_scope() as db:
            proposal = db.get(Proposal, proposal_id)
            if proposal:
                proposal.status = ProposalStatus.AWAITING_SCOPE_SIGNOFF
    except Exception:
        log.exception("shortfall-only run failed for proposal %d", proposal_id)
        _set_stage(proposal_id, "Shortfall analysis failed — check logs.")


def run_teaming_research_only(proposal_id: int) -> None:
    """Run JUST the Teaming Researcher (Gemini Pro grounded) on an
    existing proposal. Used by the 'Run Teaming Research' button on
    the Gaps tab → "Teaming partners" sub-tab so the user pays the
    ~$0.05/gap cost only when they're actually exploring teaming as
    a mitigation path. Idempotent — running twice just refreshes
    partner data on each gap with a teaming-style mitigation."""
    log.info("teaming-only run starting for proposal %d", proposal_id)
    try:
        _enrich_teaming_partners(proposal_id)
    except Exception:
        log.exception(
            "teaming-only run failed for proposal %d",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            "Teaming research failed — check logs.",
        )


def spawn_intake(proposal_id: int) -> threading.Thread:
    """Fire-and-forget background thread. Daemon so it doesn't block app exit."""
    t = threading.Thread(
        target=run_intake_pipeline,
        args=(proposal_id,),
        name=f"intake-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


def spawn_shortfall(proposal_id: int) -> threading.Thread:
    """Standalone shortfall run for retroactive use on existing proposals."""
    t = threading.Thread(
        target=run_shortfall_only,
        args=(proposal_id,),
        name=f"shortfall-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


def spawn_teaming_research(proposal_id: int) -> threading.Thread:
    """Standalone teaming-research run for the on-demand button on
    the Gaps tab → "Teaming partners" sub-tab. Daemon thread so the
    UI handler returns immediately while Gemini does its grounded
    calls."""
    t = threading.Thread(
        target=run_teaming_research_only,
        args=(proposal_id,),
        name=f"teaming-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


def run_section_m_only(proposal_id: int) -> None:
    """Run just the Section M extractor on an existing proposal.

    Used by spawn_section_m_only (daemon thread) so the user can
    re-extract evaluation criteria from the Evaluation Criteria tab
    without running a full intake re-run.
    """
    log.info("section_m_only starting for proposal %d", proposal_id)
    try:
        _set_stage(proposal_id, "Re-extracting evaluation criteria (Section M)…")
        from app.services.evaluation_criteria import extract_and_persist_evaluation_criteria

        ok = extract_and_persist_evaluation_criteria(proposal_id)
        if ok:
            _set_stage(proposal_id, "Evaluation criteria re-extracted.")
        else:
            _set_stage(
                proposal_id,
                "⚠ Section M extraction failed — see logs.",
            )
    except Exception:
        log.exception(
            "section_m_only failed for proposal %d",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            "⚠ Section M extraction failed — see logs.",
        )


def spawn_section_m_only(proposal_id: int) -> threading.Thread:
    """Fire-and-forget daemon thread for on-demand Section M re-extraction.

    Wired to the 'Re-extract evaluation criteria' button on the
    Evaluation Criteria tab via app/ui/pages.py.
    """
    t = threading.Thread(
        target=run_section_m_only,
        args=(proposal_id,),
        name=f"section-m-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t
