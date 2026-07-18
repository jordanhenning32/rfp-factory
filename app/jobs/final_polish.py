"""Final Polish Pass — cross-section consistency cleanup as a single
job. Reads every drafted section as one corpus, surfaces drifts via
the Polish Detector (Gemini 2.5 Pro), then auto-applies each fix via
the Polish Applier (Sonnet 4.6) section-by-section, persisting each
edit with a bumped revision so the user can see what changed.

Designed for "as little human interaction as possible": no per-issue
accept/dismiss step, no Apply button per section. Click once → all
auto-applicable fixes land. The user reviews via the activity log
afterward and reverts via the standard per-section regenerate path
if any fix was wrong.

Status: DOES NOT change proposal_status. Polish runs on top of
DRAFT_READY (or REVIEWING if mid-loop) and leaves the status alone.
The reviewer can be re-run after polish if desired.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from sqlalchemy import select

from app.agents.final_polish_applier import apply_polish_issue
from app.agents.final_polish_detector import (
    detect_polish_issues,
)
from app.db.session import session_scope
from app.models import ProposalSection
from app.services.cancellation import (
    add_active_section,
    remove_active_section,
)
from app.services.polish import record_polish_edit
from app.services.sections import persist_section_draft
from app.services.stages import record_stage as _set_stage

log = logging.getLogger(__name__)


def _build_corpus(sections: list[dict]) -> str:
    """Stitch every drafted section's markdown into one corpus. Each
    section is bracketed with a clear `=== SEC-### "Title" ===` header
    so the detector's `section_id` references resolve unambiguously
    when the applier later picks one section to edit.
    """
    parts: list[str] = []
    for s in sections:
        if not s.get("draft_md"):
            continue
        if s.get("excluded_from_draft"):
            continue
        header = f'=== {s["section_id"]} "{s["section_title"]}" ==='
        parts.append(header)
        parts.append(s["draft_md"].rstrip())
        parts.append("")  # blank line between sections
    return "\n".join(parts)


def _snapshot_sections(proposal_id: int) -> list[dict]:
    """Plain-dict snapshot of every section + its current draft.
    Held by the orchestrator across the detector + per-issue applier
    calls so we can correlate detector-output `section_id` strings
    back to ProposalSection.pk for persistence."""
    with session_scope() as db:
        rows = (
            db.execute(
                select(ProposalSection)
                .where(ProposalSection.proposal_id == proposal_id)
                .order_by(ProposalSection.section_order)
            )
            .scalars()
            .all()
        )
        return [
            {
                "pk": s.id,
                "section_id": s.section_id,
                "section_title": s.section_title,
                "section_order": s.section_order,
                "draft_md": s.draft_text_markdown or "",
                "citations": list(s.citations_json or []),
                "needs_human_placeholders": list(
                    s.needs_human_placeholders_json or [],
                ),
                "shortfall_mitigations_applied": list(
                    s.shortfall_mitigations_applied_json or [],
                ),
                "excluded_from_draft": bool(s.excluded_from_draft),
                "requires_cost_analysis": bool(s.requires_cost_analysis),
            }
            for s in rows
        ]


def run_final_polish(proposal_id: int) -> None:
    """Orchestrate the full polish pass:
      1. Snapshot every drafted section.
      2. Build the corpus + run the Detector (Gemini Pro).
      3. For each issue, run the Applier (Sonnet) on the affected
         section's CURRENT draft (not the snapshot — earlier issues
         in the loop may have already edited it).
      4. Persist successful edits via persist_section_draft (bumps
         section revision so the activity history shows the change).
      5. Stage banner reports applied / skipped / failed counts.

    Best-effort. Per-issue failures are logged and skipped — they
    don't abort the rest of the pass.
    """
    log.info("final polish starting for proposal %d", proposal_id)
    # Single timestamp shared by every edit applied in THIS pass —
    # the UI groups edits by `applied_in_run_at` to render
    # "Run @ 15:21 — 6 edits" cards. Captured before any LLM work
    # so the wave-grouping is stable even if the detector takes
    # ~60s to return.
    run_started_at = datetime.utcnow()
    try:
        _set_stage(
            proposal_id,
            "Final Polish: assembling cross-section corpus…",
        )
        sections = _snapshot_sections(proposal_id)
        # Filter to writer-team sections that actually have a draft.
        # Cost-deferred sections drafted by the Cost Volume Writer ARE
        # included if they have a draft — voice / numerical drift
        # between technical sections and the cost narrative is
        # exactly one of the things polish should catch.
        polishable = [s for s in sections if s["draft_md"] and not s["excluded_from_draft"]]
        if not polishable:
            _set_stage(
                proposal_id,
                "Final Polish: no drafted sections — nothing to polish.",
            )
            return

        # Index by section_id so issue-application can find the right
        # ProposalSection.pk + the latest draft markdown.
        by_section_id: dict[str, dict] = {s["section_id"]: s for s in polishable}

        corpus = _build_corpus(polishable)
        n_sections = len(polishable)
        n_chars = len(corpus)

        _set_stage(
            proposal_id,
            f"Final Polish: detecting cross-section issues across "
            f"{n_sections} section(s) ({n_chars:,} chars)…",
        )

        try:
            issues = detect_polish_issues(
                proposal_id=proposal_id,
                corpus=corpus,
            )
        except Exception:
            log.exception(
                "final_polish: detector failed for proposal %d",
                proposal_id,
            )
            _set_stage(
                proposal_id,
                "Final Polish failed at detection step — check logs.",
            )
            return

        if not issues:
            _set_stage(
                proposal_id,
                f"Final Polish: corpus is consistent — no cross-"
                f"section issues found across {n_sections} section(s). "
                f"Nothing to apply.",
            )
            log.info(
                "final_polish: proposal %d clean — 0 issues across %d section(s)",
                proposal_id,
                n_sections,
            )
            return

        # Distribution for the stage banner.
        sev_counts: dict[str, int] = {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0}
        type_counts: dict[str, int] = {}
        for it in issues:
            sev_counts[it.severity] = sev_counts.get(it.severity, 0) + 1
            type_counts[it.issue_type] = type_counts.get(it.issue_type, 0) + 1
        sev_str = " / ".join(f"{n} {s}" for s, n in sev_counts.items() if n)
        type_str = ", ".join(
            f"{n} {t}"
            for t, n in sorted(
                type_counts.items(),
                key=lambda kv: -kv[1],
            )
        )
        _set_stage(
            proposal_id,
            f"Final Polish: {len(issues)} cross-section issue(s) "
            f"detected ({sev_str}). Auto-applying: {type_str}…",
        )

        # Apply each issue. The section's draft mutates as we go, so
        # always re-read the LATEST draft from the by_section_id index
        # (which we'll update after each successful persist).
        n_applied = 0
        n_section_not_found = 0
        n_apply_failed = 0
        n_text_not_in_draft = 0
        applied_summaries: list[str] = []

        for issue in issues:
            target = by_section_id.get(issue.section_id)
            if target is None:
                n_section_not_found += 1
                log.warning(
                    "final_polish: detector cited section_id=%r but "
                    "no such section exists for proposal %d — skipping.",
                    issue.section_id,
                    proposal_id,
                )
                continue

            section_pk = target["pk"]
            current_md = target["draft_md"]
            # Mark in-flight so concurrent regenerate paths don't race
            # us. Per-issue grain — releases between issues so a long
            # polish pass doesn't lock the whole section.
            add_active_section(proposal_id, section_pk)
            try:
                result = apply_polish_issue(
                    proposal_id=proposal_id,
                    issue=issue,
                    current_markdown=current_md,
                )
            except Exception:
                log.exception(
                    "final_polish: applier failed on section %s issue=%s — skipping.",
                    issue.section_id,
                    issue.issue_type,
                )
                n_apply_failed += 1
                remove_active_section(proposal_id, section_pk)
                continue

            if not result.edit_applied:
                # Text wasn't found in the current draft — most likely
                # the section was regenerated between detector and
                # applier OR a prior issue's edit already changed the
                # surface form. Move on; not an error.
                n_text_not_in_draft += 1
                remove_active_section(proposal_id, section_pk)
                continue

            if not result.polished_markdown.strip():
                # Defensive — refuse to wipe a section.
                log.error(
                    "final_polish: applier returned empty markdown "
                    "for section %s issue=%s — skipping persist to "
                    "avoid wiping the draft.",
                    issue.section_id,
                    issue.issue_type,
                )
                n_apply_failed += 1
                remove_active_section(proposal_id, section_pk)
                continue

            try:
                persist_section_draft(
                    proposal_section_pk=section_pk,
                    draft_text_markdown=result.polished_markdown,
                    citations=target["citations"],
                    needs_human_placeholders=target["needs_human_placeholders"],
                    shortfall_mitigations_applied=target["shortfall_mitigations_applied"],
                )
            except Exception:
                log.exception(
                    "final_polish: persist failed for section %s issue=%s — skipping.",
                    issue.section_id,
                    issue.issue_type,
                )
                n_apply_failed += 1
                remove_active_section(proposal_id, section_pk)
                continue

            # Update the working snapshot so subsequent issues on the
            # same section see the latest text.
            target["draft_md"] = result.polished_markdown
            n_applied += 1
            summary = result.edit_summary or (f"{issue.issue_type} fix on {issue.section_id}")
            applied_summaries.append(f"{issue.section_id} [{issue.severity}]: {summary}")
            # Audit row — surfaces in the Final Polish tab's
            # "Recent edits" list so the user sees what changed
            # without diffing section revisions manually.
            try:
                record_polish_edit(
                    proposal_id=proposal_id,
                    proposal_section_id=section_pk,
                    section_id_label=issue.section_id,
                    issue_type=issue.issue_type,
                    severity=issue.severity,
                    edit_summary=summary,
                    rationale=issue.rationale,
                    problematic_text=issue.problematic_text,
                    suggested_fix=issue.suggested_fix,
                    applied_at=datetime.utcnow(),
                    applied_in_run_at=run_started_at,
                    cost_usd=result.cost_usd,
                )
            except Exception:
                log.exception(
                    "final_polish: failed to persist polish-edit "
                    "audit row for section %s issue=%s — non-fatal "
                    "(the draft update DID succeed; only the audit "
                    "log entry is missing).",
                    issue.section_id,
                    issue.issue_type,
                )
            remove_active_section(proposal_id, section_pk)

        # Final summary banner.
        msg_parts = [f"Final Polish complete: {n_applied} fix(es) applied"]
        if n_text_not_in_draft:
            msg_parts.append(
                f"{n_text_not_in_draft} skipped (text not in current "
                f"draft — section may have been regenerated)"
            )
        if n_section_not_found:
            msg_parts.append(f"{n_section_not_found} skipped (unknown section_id from detector)")
        if n_apply_failed:
            msg_parts.append(f"{n_apply_failed} failed")
        _set_stage(proposal_id, " · ".join(msg_parts) + ".")

        log.info(
            "final_polish: proposal %d — %d applied, %d skipped "
            "(text gone), %d skipped (no section), %d failed",
            proposal_id,
            n_applied,
            n_text_not_in_draft,
            n_section_not_found,
            n_apply_failed,
        )
        if applied_summaries:
            log.info(
                "final_polish applied summaries: %s",
                "; ".join(applied_summaries),
            )

    except Exception:
        log.exception("final polish failed for proposal %d", proposal_id)
        _set_stage(proposal_id, "Final Polish failed — check logs.")


def spawn_final_polish(proposal_id: int) -> threading.Thread:
    """Daemon thread launcher for the Final Polish button. UI handler
    returns immediately while the polish pass runs in the background."""
    t = threading.Thread(
        target=run_final_polish,
        args=(proposal_id,),
        name=f"final-polish-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


__all__ = [
    "run_final_polish",
    "spawn_final_polish",
]
