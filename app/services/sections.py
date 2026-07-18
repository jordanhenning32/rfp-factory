"""ProposalSection persistence helpers used by the Outline Agent and Writer Team.

The Outline Agent creates the section list and writes the briefs. The Writer
Team agent fills in draft_text_markdown, citations, and needs_human placeholders
section by section. Re-runs of either stage should clear prior state without
nuking unrelated rows — these helpers centralize that logic.
"""

from __future__ import annotations

import logging
import re as _re_citations
from collections.abc import Iterable
from dataclasses import dataclass

from app.db.session import session_scope
from app.models import ProposalSection

log = logging.getLogger(__name__)


@dataclass
class OutlineSection:
    """One section as returned by the Outline Agent."""

    section_id: str
    section_title: str
    section_order: int
    section_brief: str
    compliance_items_addressed: list[str]
    page_limit: int | None = None
    word_limit: int | None = None
    # True when the Writer Team should skip this section (cost / pricing
    # content drafted later by the Cost Analysis Agent).
    requires_cost_analysis: bool = False


def replace_outline(proposal_id: int, sections: Iterable[OutlineSection]) -> int:
    """Wipe any existing ProposalSection rows for this proposal and write fresh
    rows from the Outline Agent's output. Returns the number of rows written.

    Wipes are total — drafts written under a previous outline are discarded.
    Re-running the outline is treated as starting over because compliance-item
    assignments may have changed and stale drafts could cite the wrong section.
    """
    written = 0
    with session_scope() as db:
        db.query(ProposalSection).filter(ProposalSection.proposal_id == proposal_id).delete(
            synchronize_session=False
        )

        for s in sections:
            db.add(
                ProposalSection(
                    proposal_id=proposal_id,
                    section_id=s.section_id,
                    section_title=s.section_title,
                    section_order=s.section_order,
                    section_brief=s.section_brief,
                    page_limit=s.page_limit,
                    word_limit=s.word_limit,
                    requires_cost_analysis=bool(s.requires_cost_analysis),
                    compliance_items_addressed_json=list(s.compliance_items_addressed),
                    citations_json=[],
                    needs_human_placeholders_json=[],
                    shortfall_mitigations_applied_json=[],
                    draft_text_markdown=None,
                    current_revision_number=0,
                )
            )
            written += 1
    log.info("replace_outline: wrote %d section(s) for proposal %d", written, proposal_id)
    return written


def mark_compliance_item_outline_excluded(
    *,
    proposal_id: int,
    req_id: str,
    excluded: bool = True,
) -> bool:
    """Toggle the excluded_from_outline flag on a single compliance
    item. Used by the Outline tab's "Mark N/A — not a narrative item"
    dropdown option, so a user can dismiss an unassigned item that
    genuinely doesn't belong on the outline without re-running the
    Outline Agent. Returns True when the row was found and updated."""
    from sqlalchemy import select as _select

    from app.models import ComplianceMatrixItem

    with session_scope() as db:
        # Active row only — after an amendment supersedes a requirement,
        # two rows share the same (proposal_id, requirement_id); without
        # the status filter, scalar_one_or_none() raises MultipleResultsFound.
        ci = db.execute(
            _select(ComplianceMatrixItem).where(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.requirement_id == req_id,
                ComplianceMatrixItem.status == "active",
            )
        ).scalar_one_or_none()
        if ci is None:
            return False
        ci.excluded_from_outline = bool(excluded)
    return True


# Inline citation markers the writer agents insert as traceability
# hooks. The actual citation source data lives in
# ProposalSection.citations_json — the bracketed inline tokens are
# noise in any human-facing output (preview / DOCX / Submission
# Checklist). Stripped at compile time, never persisted-cleaned so
# the per-section drafts retain the markers for any future audit /
# regen work that wants them. Patterns covered:
#   [^cite-1], [^cite-12], [^cite1]      — markdown-footnote style
#   [^cite]                              — bare bracketed token
#   [cite-1], [cite: foo, bar]           — square-bracket prose forms
#   ^cite-1, ^cite                       — caret-only escapes (rare)
_CITE_MARKER_RE = _re_citations.compile(
    r"\[\s*\^?cite[-:_\s]*[^\]]*?\]"  # bracketed forms
    r"|\^cite[-:_]?\d*",  # bare-caret fallback
    _re_citations.IGNORECASE,
)


def strip_citation_markers(md: str) -> str:
    """Remove inline [^cite-N] / [cite] / [Cite: ...] markers from
    rendered text. Idempotent. Cleans up the surrounding whitespace
    + punctuation so the resulting prose reads naturally:

      "...delivery model.[^cite-1] We propose..."
        → "...delivery model. We propose..."
      "Quadratic[^cite-2], a small business,"
        → "Quadratic, a small business,"
      "[^cite-1] [^cite-2] We led..."
        → "We led..."

    Empty / None input returns "" so callers can chain safely."""
    if not md:
        return ""
    cleaned = _CITE_MARKER_RE.sub("", md)
    # Collapse runs of whitespace that result from adjacent removals.
    cleaned = _re_citations.sub(r"[ \t]{2,}", " ", cleaned)
    # Drop space-before-punctuation: "text . " / "text , " etc.
    cleaned = _re_citations.sub(r"[ \t]+([.,;:!?\)\]])", r"\1", cleaned)
    # Drop stranded leading whitespace on every line (handles the
    # first line + any line that started with a now-removed marker).
    cleaned = _re_citations.sub(
        r"(^|\n)[ \t]+",
        r"\1",
        cleaned,
    )
    return cleaned


def compile_proposal_markdown(
    proposal_id: int,
    *,
    include_cost_deferred: bool = True,
) -> dict:
    """Concatenate every drafted section into one markdown document for
    end-to-end preview. Used by the Final Polish tab's "View final
    draft" modal so the user can read the polished proposal as a
    single unit before committing to submit.

    Sections appear in section_order. Each section is preceded by an
    `## SEC-### — Title` header so the preview looks like the actual
    deliverable's table of contents. Excluded-from-draft sections are
    skipped (user-flagged wrappers). Sections without drafts are
    skipped (Writer Team or Cost Writer hasn't run yet for those).

    Returns:
        {
            "markdown": full concatenated markdown (str),
            "sections_included": list of dicts:
                [{"section_id", "section_title", "section_order",
                  "revision", "char_count", "is_cost_deferred",
                  "last_updated_at"}],
            "sections_skipped": list of dicts (excluded / undrafted),
            "total_chars": int — sum of section markdown chars,
            "total_sections": int — included only,
        }
    """
    from sqlalchemy import select as _select

    from app.models import ProposalSection

    with session_scope() as db:
        rows = (
            db.execute(
                _select(ProposalSection)
                .where(ProposalSection.proposal_id == proposal_id)
                .order_by(
                    ProposalSection.section_order,
                    ProposalSection.id,
                )
            )
            .scalars()
            .all()
        )
        section_meta = []
        skipped = []
        parts: list[str] = []
        for s in rows:
            # Strip [^cite-N] / [Cite: ...] traceability markers
            # before any downstream consumer sees the text. The
            # writer agents emit these as breadcrumbs to the
            # citations_json source list; they're noise in user-
            # facing output. Citations themselves live elsewhere
            # (ProposalSection.citations_json) and aren't lost.
            raw_md = (s.draft_text_markdown or "").strip()
            md = strip_citation_markers(raw_md)
            is_cost_deferred = bool(s.requires_cost_analysis)
            if s.excluded_from_draft:
                skipped.append(
                    {
                        "section_id": s.section_id,
                        "section_title": s.section_title,
                        "section_order": s.section_order,
                        "reason": "excluded_from_draft",
                    }
                )
                continue
            if is_cost_deferred and not include_cost_deferred:
                skipped.append(
                    {
                        "section_id": s.section_id,
                        "section_title": s.section_title,
                        "section_order": s.section_order,
                        "reason": "cost_deferred (excluded by caller)",
                    }
                )
                continue
            if not md:
                skipped.append(
                    {
                        "section_id": s.section_id,
                        "section_title": s.section_title,
                        "section_order": s.section_order,
                        "reason": "no_draft_yet",
                    }
                )
                continue

            header = f"## {s.section_id} — {s.section_title}"
            parts.append(header)
            parts.append(md)
            parts.append("")  # blank line between sections
            section_meta.append(
                {
                    "section_id": s.section_id,
                    "section_title": s.section_title,
                    "section_order": s.section_order,
                    "revision": s.current_revision_number or 0,
                    "char_count": len(md),
                    "is_cost_deferred": is_cost_deferred,
                    "last_updated_at": s.updated_at,
                }
            )

    full_markdown = "\n".join(parts).rstrip() + "\n"
    return {
        "markdown": full_markdown,
        "sections_included": section_meta,
        "sections_skipped": skipped,
        "total_chars": sum(m["char_count"] for m in section_meta),
        "total_sections": len(section_meta),
    }


def assign_compliance_item_to_section(
    *,
    req_id: str,
    section_pk: int,
) -> bool:
    """Append a single compliance-item req_id to a section's
    compliance_items_addressed_json list. Idempotent — returns False
    (no-op) when the item is already present on that section.

    Used by the Outline tab's "unassigned items" recovery UI: when the
    Outline Agent missed mapping a narrative item, the user can attach
    it to any existing section without having to regenerate the whole
    outline (which would wipe drafts). Safe to call mid-pipeline.
    """
    with session_scope() as db:
        sec = db.get(ProposalSection, section_pk)
        if sec is None:
            return False
        current = list(sec.compliance_items_addressed_json or [])
        if req_id in current:
            return False
        current.append(req_id)
        sec.compliance_items_addressed_json = current
    return True


def persist_section_draft(
    *,
    proposal_section_pk: int,
    draft_text_markdown: str,
    citations: list[dict],
    needs_human_placeholders: list[dict],
    shortfall_mitigations_applied: list[str],
) -> None:
    """Save a Writer Team draft into one ProposalSection row. Bumps the
    revision number so users can see when a section was regenerated.
    """
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None:
            log.warning("persist_section_draft: section pk=%d not found", proposal_section_pk)
            return
        sec.draft_text_markdown = draft_text_markdown
        sec.citations_json = citations
        sec.needs_human_placeholders_json = needs_human_placeholders
        sec.shortfall_mitigations_applied_json = shortfall_mitigations_applied
        sec.current_revision_number = (sec.current_revision_number or 0) + 1


def clear_section_draft(proposal_section_pk: int) -> None:
    """Wipe the drafted content for one section so it can be regenerated.
    Leaves the outline metadata (title, brief, compliance assignments) intact.
    """
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None:
            return
        sec.draft_text_markdown = None
        sec.citations_json = []
        sec.needs_human_placeholders_json = []
        sec.shortfall_mitigations_applied_json = []


def set_section_cost_deferred(proposal_section_pk: int, value: bool) -> bool:
    """Manual toggle for the Outline Agent's cost-deferred flag. Used when
    the agent missed a cost section (e.g., a Pricing section embedded in
    the technical volume) and the user needs to mark it skipped before
    the Reviewer / Writer Team waste budget on it.

    Returns True if the section exists and was updated.
    """
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None:
            return False
        sec.requires_cost_analysis = bool(value)
        log.info(
            "set_section_cost_deferred: section pk=%d -> requires_cost_analysis=%s",
            proposal_section_pk,
            value,
        )
    return True


def set_section_excluded_from_draft(proposal_section_pk: int, value: bool) -> bool:
    """Manual toggle for 'don't draft this section.' Used when the
    Outline Agent produced a wrapper section for a form / attachment /
    instructions item that doesn't need narrative — the user flips this
    so the Writer Team skips it. Resets on outline regenerate because
    all sections are replaced.

    Returns True if the section exists and was updated.
    """
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None:
            return False
        sec.excluded_from_draft = bool(value)
        log.info(
            "set_section_excluded_from_draft: section pk=%d -> excluded_from_draft=%s",
            proposal_section_pk,
            value,
        )
    return True


def save_manual_edit(proposal_section_pk: int, new_markdown: str) -> bool:
    """Replace a section's draft markdown with user-edited text. Bumps the
    revision number; reconciliation of needs_human_placeholders_json against
    the new text is handled by reconcile_placeholders() in a follow-up
    transaction.

    Citations are NOT reconciled: citations are claim/source records, not
    text spans. Manual edits that delete a [^cite-N] marker leave the
    citation entry orphaned but harmless — the user can clean it up by
    regenerating the section if they care.

    Returns True if the section exists and was updated.
    """
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None:
            return False
        sec.draft_text_markdown = new_markdown
        sec.current_revision_number = (sec.current_revision_number or 0) + 1
        log.info(
            "save_manual_edit: section pk=%d, %d chars, rev=%d",
            proposal_section_pk,
            len(new_markdown or ""),
            sec.current_revision_number,
        )
    # Run reconcile in its own transaction so the new markdown is committed
    # first; reconcile reads it back to find inline markers.
    from app.services.needs_human import reconcile_placeholders

    reconcile_placeholders(proposal_section_pk)
    return True
