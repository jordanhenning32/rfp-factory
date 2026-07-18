"""Strategy Implementer orchestration.

Two entry points:
  - synthesize_strategy_directives(proposal_id) — sync; snapshots
    inputs (cached strategy + outline + active findings + cost
    build summary), runs the implementer agent, returns the
    structured directives for the UI to preview. Does NOT mutate
    any sections — that happens only after user approval.
  - apply_strategy_directives(proposal_id, directives) — spawns
    one daemon thread per approved directive that calls
    run_writer_for_section with user_directive set. Returns the
    count of jobs spawned.

Why split? The user wants to review and edit each directive before
the writer team spends $0.50/section. Synthesis is cheap (~$0.10);
applying is the expensive step.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlalchemy import select

from app.agents.strategy_implementer import (
    synthesize_directives,
)
from app.config import get_settings
from app.db.session import session_scope
from app.jobs.cost_reviewer import _snapshot_cost_reviewer_inputs
from app.jobs.writer import run_writer_for_section
from app.models import ProposalSection
from app.services.cost_reviewer import (
    get_cost_review_findings_snapshot,
    get_cost_review_strategy,
)
from app.services.stages import record_stage as _set_stage

log = logging.getLogger(__name__)


# In-memory state tracking the latest "Apply Strategy" run per
# proposal so the UI can surface a sticky completion toast even
# after the user navigates between tabs. Cleared on process restart
# (acceptable — the tab badge / Pipeline log persists the work
# itself in agent_runs / stages). Mutated from background threads
# AND read from the UI thread, so guard with a lock.
_apply_state_lock = threading.Lock()
_apply_state: dict[int, dict] = {}


def get_strategy_apply_state(proposal_id: int) -> dict | None:
    """Read-only snapshot of the latest Apply Strategy run state
    for this proposal. Returns None when no apply has run since
    process start. Used by the Cost Review tab's completion-poll
    timer."""
    with _apply_state_lock:
        s = _apply_state.get(proposal_id)
        return dict(s) if s else None


def _set_apply_state(proposal_id: int, **updates) -> None:
    """Update the apply-state for a proposal (creates entry if
    absent). Thread-safe."""
    with _apply_state_lock:
        s = _apply_state.setdefault(proposal_id, {})
        s.update(updates)


def _update_section_progress(
    proposal_id: int,
    section_id: str,
    status: str,
) -> None:
    """Atomic update of one section's status inside the apply
    state's sections_progress dict. status in
    {pending, running, done, failed}. The dialog poll reads the
    snapshot to render per-section icons."""
    with _apply_state_lock:
        s = _apply_state.setdefault(proposal_id, {})
        sp = s.setdefault("sections_progress", {})
        sp[section_id] = status


def claim_strategy_apply_notification(
    proposal_id: int,
    completed_at: datetime,
) -> bool:
    """Atomic check-and-set. Returns True iff this caller is the
    one that should fire the completion notify; False otherwise.
    Prevents double-notify when multiple ui.timer instances poll
    concurrently (which can happen when a @ui.refreshable rebuilds
    the tab while a poll is pending).
    """
    with _apply_state_lock:
        s = _apply_state.get(proposal_id)
        if not s:
            return False
        if s.get("completed_at") != completed_at:
            return False
        if s.get("notified_at") == completed_at:
            return False
        s["notified_at"] = completed_at
        return True


def _format_eligible_sections(
    sections: list[dict],
) -> str:
    """Compact rendering of the writer-eligible sections for the
    implementer's prompt. Includes section_id + title + brief +
    word_limit + has_draft so the agent can decide which sections
    the strategy actually touches."""
    if not sections:
        return "  (no eligible sections)"
    rows: list[str] = []
    total = 0
    for s in sections:
        brief = (s.get("section_brief") or "").strip()
        if len(brief) > 400:
            brief = brief[:397] + "..."
        wl = s.get("word_limit")
        wl_str = f"{wl} words" if wl else "no limit"
        draft_tag = " [drafted]" if s.get("has_draft") else " [outline-only]"
        line = (
            f"  - {s['section_id']}{draft_tag}: {s['section_title']} "
            f"({wl_str})\n      {brief or '(no brief)'}"
        )
        if total + len(line) > 6000:
            rows.append(f"  ... ({len(sections) - len(rows)} more sections truncated for prompt budget)")
            break
        rows.append(line)
        total += len(line) + 1
    return "\n".join(rows)


def _format_findings_for_implementer(findings_rows: list[dict]) -> str:
    """Compact one-line-per-finding rendering. The implementer uses
    these to map directives back to specific findings (cited in the
    rationale field). Filtered to non-rejected findings since the
    user has dismissed rejections."""
    if not findings_rows:
        return "  (no active findings)"
    # Group by finding_text to dedupe per-scenario rows. The Cost
    # Review tab does the same grouping for display.
    seen: dict[str, dict] = {}
    for r in findings_rows:
        if r.get("user_action") == "rejected":
            continue
        key = (r.get("finding_text") or "").strip()
        if not key:
            continue
        if key not in seen:
            seen[key] = {
                "severity": r.get("severity") or "MINOR",
                "category": r.get("category") or "?",
                "subject": "",
                "scenarios": set(),
                "user_action": r.get("user_action") or "pending",
            }
        seen[key]["scenarios"].add(r.get("scenario") or "")
        # Pull subject out of the [subject] body prefix if present.
        if not seen[key]["subject"]:
            import re

            m = re.match(r"^\[([^\]]+)\]\s*", key)
            if m:
                seen[key]["subject"] = m.group(1).strip()

    if not seen:
        return "  (no active findings)"
    rows: list[str] = []
    for body, meta in seen.items():
        scenarios = ",".join(sorted(s for s in meta["scenarios"] if s))
        action_tag = "" if meta["user_action"] == "pending" else f" ({meta['user_action']})"
        rows.append(
            f"  - {meta['severity']} · {meta['category']} · "
            f"affects {scenarios}{action_tag}: "
            f"{meta['subject'] or body[:80]}"
        )
    return "\n".join(rows)


def _format_cost_build_summary(packages: list[dict]) -> str:
    """One-line per scenario: price, margin, vs-market, recommendation.
    Compact so the implementer can reference the actual numbers in
    directives without bloating the prompt."""
    if not packages:
        return "  (no cost build)"
    lines: list[str] = []
    for p in packages:
        indirect = p.get("indirect_costs_json") or {}
        lines.append(
            f"  {p.get('scenario', '?')}: price "
            f"${float(p.get('total_proposed_price') or 0):,.0f} | "
            f"margin "
            f"{float(indirect.get('profit_pct') or 0):.1%} | "
            f"vs market {p.get('vs_market_position') or '?'} | "
            f"{p.get('bid_recommendation') or '?'}"
        )
    return "\n".join(lines)


def _snapshot_eligible_sections(proposal_id: int) -> list[dict]:
    """Sections the Writer Team can regenerate via user_directive.
    Excludes cost-deferred (drafted by Cost Volume Writer instead)
    and user-excluded (wrappers / forms / attachments)."""
    out: list[dict] = []
    with session_scope() as db:
        rows = db.execute(
            select(
                ProposalSection.id,
                ProposalSection.section_id,
                ProposalSection.section_title,
                ProposalSection.section_brief,
                ProposalSection.section_order,
                ProposalSection.word_limit,
                ProposalSection.requires_cost_analysis,
                ProposalSection.excluded_from_draft,
                ProposalSection.draft_text_markdown,
            )
            .where(
                ProposalSection.proposal_id == proposal_id,
            )
            .order_by(ProposalSection.section_order)
        ).all()
    for pk, sid, title, brief, order, wl, requires_cost, excluded, draft in rows:
        if requires_cost or excluded:
            continue
        out.append(
            {
                "pk": pk,
                "section_id": sid,
                "section_title": title,
                "section_brief": brief,
                "section_order": order,
                "word_limit": wl,
                "has_draft": bool(draft and str(draft).strip()),
            }
        )
    return out


def synthesize_strategy_directives(
    proposal_id: int,
) -> dict | None:
    """Sync entry — runs the implementer agent. Returns a dict:

        {
          "directives": [ {section_id, directive, rationale,
                           priority, estimated_changes,
                           section_title, section_pk}, ... ],
          "n_eligible_sections": int,
          "n_active_findings": int,
        }

    Returns None when prerequisites are missing (no cached strategy,
    no eligible sections). Caller surfaces the reason via UI.
    """
    cached = get_cost_review_strategy(proposal_id)
    if not cached or not cached.get("markdown"):
        log.info(
            "synthesize_strategy_directives: no cached strategy for proposal %d",
            proposal_id,
        )
        return None
    eligible = _snapshot_eligible_sections(proposal_id)
    if not eligible:
        log.info(
            "synthesize_strategy_directives: no eligible sections "
            "for proposal %d (all cost-deferred or excluded)",
            proposal_id,
        )
        return None

    findings_rows = get_cost_review_findings_snapshot(proposal_id)
    findings_block = _format_findings_for_implementer(findings_rows)
    n_active = sum(1 for r in findings_rows if r.get("user_action") != "rejected")

    # Reuse the cost reviewer snapshot for cost build / packages.
    # _snapshot_cost_reviewer_inputs returns None if pricing is
    # absent — the implementer can still run on pure narrative
    # strategies, so we degrade gracefully.
    try:
        cr_inputs = _snapshot_cost_reviewer_inputs(proposal_id)
    except Exception:
        cr_inputs = None
    cost_build_summary = cr_inputs.cost_build_block if cr_inputs is not None else "(cost build unavailable)"

    sections_block = _format_eligible_sections(eligible)
    eligible_section_ids = {s["section_id"] for s in eligible}
    section_lookup = {s["section_id"]: s for s in eligible}

    result = synthesize_directives(
        proposal_id=proposal_id,
        strategy_markdown=cached["markdown"],
        sections_block=sections_block,
        findings_block=findings_block,
        cost_build_summary=cost_build_summary,
        eligible_section_ids=eligible_section_ids,
    )

    # Decorate directives with section_title and section_pk so the
    # UI can render labels and the apply path can spawn writer jobs
    # without re-querying.
    decorated: list[dict] = []
    for d in result.directives:
        sec = section_lookup.get(d.section_id)
        if sec is None:
            continue
        decorated.append(
            {
                "section_id": d.section_id,
                "section_title": sec["section_title"],
                "section_pk": sec["pk"],
                "directive": d.directive,
                "rationale": d.rationale,
                "priority": d.priority,
                "estimated_changes": d.estimated_changes,
            }
        )

    # Sort by priority (high first) then by section_order so the
    # preview reads as a proposal walk-through.
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    section_order_lookup = {s["section_id"]: s["section_order"] for s in eligible}
    decorated.sort(
        key=lambda d: (
            priority_rank.get(d["priority"], 99),
            section_order_lookup.get(d["section_id"], 99),
        )
    )

    return {
        "directives": decorated,
        "n_eligible_sections": len(eligible),
        "n_active_findings": n_active,
    }


def _apply_one_directive(
    proposal_id: int,
    section_pk: int,
    section_id: str,
    directive: str,
) -> None:
    """Wrapper that runs run_writer_for_section with per-section
    progress book-ending. The Writer Team's existing user_directive
    flow is the load-bearing piece — this just wires the strategy
    directive into it AND publishes per-section status so the
    dialog's progress UI can render icons in real time.

    Raises on writer failure so the surrounding ThreadPoolExecutor
    can count it for the aggregate n_failed total. The progress
    state captures it independently for the per-section icon."""
    _update_section_progress(proposal_id, section_id, "running")
    try:
        run_writer_for_section(
            proposal_id=proposal_id,
            proposal_section_pk=section_pk,
            user_directive=directive,
        )
        _update_section_progress(proposal_id, section_id, "done")
    except Exception:
        _update_section_progress(proposal_id, section_id, "failed")
        log.exception(
            "apply_strategy_directive failed for section pk=%d",
            section_pk,
        )
        raise


def apply_strategy_directives(
    proposal_id: int,
    directives: list[dict],
) -> int:
    """Spawn one daemon thread per directive, each running
    run_writer_for_section with user_directive set. Returns the
    number of jobs spawned.

    Concurrency is bounded by settings.writer_workers so we don't
    hammer Anthropic with N parallel writer calls. Threads are
    daemon so they die with the process if the user closes the app
    mid-run; each writer logs its own AgentRun so the audit trail
    survives.
    """
    if not directives:
        return 0
    settings = get_settings()
    workers = max(1, int(settings.writer_workers or 1))

    # Sanity-check section_pks still belong to this proposal.
    valid: list[dict] = []
    section_ids: list[str] = []
    section_titles: dict[str, str] = {}
    with session_scope() as db:
        for d in directives:
            pk = int(d.get("section_pk") or 0)
            sec = db.get(ProposalSection, pk) if pk else None
            if sec is None or sec.proposal_id != proposal_id:
                log.warning(
                    "apply_strategy_directives: skipping directive with missing/foreign section_pk=%r",
                    pk,
                )
                continue
            if sec.requires_cost_analysis or sec.excluded_from_draft:
                log.warning(
                    "apply_strategy_directives: skipping directive "
                    "for ineligible section pk=%d (cost-deferred or "
                    "excluded)",
                    pk,
                )
                continue
            text = (d.get("directive") or "").strip()
            if not text:
                continue
            valid.append(
                {
                    "section_pk": pk,
                    "section_id": sec.section_id,
                    "directive": text,
                }
            )
            section_ids.append(sec.section_id)
            section_titles[sec.section_id] = sec.section_title

    if not valid:
        return 0

    _set_stage(
        proposal_id,
        f"Strategy Implementer: applying {len(valid)} directive(s) "
        f"across sections {', '.join(section_ids[:6])}"
        f"{' ...' if len(section_ids) > 6 else ''} "
        f"({workers} writer worker(s) in parallel)…",
    )
    # Reset apply-state so the UI poll doesn't surface a stale
    # completion toast from a previous run, and seed per-section
    # progress with 'pending' so the dialog can render the section
    # list immediately on transition to applying mode.
    _set_apply_state(
        proposal_id,
        status="running",
        started_at=datetime.utcnow(),
        completed_at=None,
        notified_at=None,
        n_total=len(valid),
        n_done=0,
        n_failed=0,
        section_ids=list(section_ids),
        section_titles=dict(section_titles),
        sections_progress={sid: "pending" for sid in section_ids},
    )

    # Use ThreadPoolExecutor so we cap concurrency at writer_workers
    # without the caller having to wait on completion. The executor
    # itself runs in a daemon thread to keep the "spawn and return"
    # contract — the user gets immediate UI feedback and watches
    # progress via stage banners while the pool processes.
    def _runner() -> None:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"strategy-impl-{proposal_id}",
        ) as ex:
            futures = [
                ex.submit(
                    _apply_one_directive,
                    proposal_id,
                    v["section_pk"],
                    v["section_id"],
                    v["directive"],
                )
                for v in valid
            ]
            n_done = 0
            n_failed = 0
            for fut in futures:
                try:
                    fut.result()
                    n_done += 1
                except Exception:
                    n_failed += 1
                    log.exception(
                        "apply_strategy_directives: worker raised",
                    )
                # Update progress so the UI poll can surface running
                # totals if we ever wire a progress indicator.
                _set_apply_state(
                    proposal_id,
                    n_done=n_done,
                    n_failed=n_failed,
                )
        _set_stage(
            proposal_id,
            f"Strategy Implementer: applied {n_done}/{len(valid)} "
            f"directive(s)"
            + (f" ({n_failed} failed — see logs)" if n_failed else "")
            + ". Open the Draft tab to review the regenerated "
            "sections.",
        )
        # Mark complete — UI poll picks this up and fires the
        # sticky completion toast on whatever tab the user is on
        # (the timer is registered on the Cost Review tab and
        # survives tab navigation as long as the proposal page
        # is open).
        _set_apply_state(
            proposal_id,
            status="completed",
            completed_at=datetime.utcnow(),
            n_done=n_done,
            n_failed=n_failed,
        )

    t = threading.Thread(
        target=_runner,
        name=f"strategy-impl-runner-{proposal_id}",
        daemon=True,
    )
    t.start()
    return len(valid)


__all__ = [
    "apply_strategy_directives",
    "claim_strategy_apply_notification",
    "get_strategy_apply_state",
    "synthesize_strategy_directives",
]
