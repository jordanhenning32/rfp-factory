"""Cost Reviewer orchestration.

Three entry points:
  - dual_review_and_consolidate(proposal_id, inputs) — pure helper:
    runs primary + secondary reviewers in parallel, then the consolidator,
    and returns a DualCostReviewOutcome with both raw results, the final
    (consensus or fallback) result, and which path was taken. No DB
    persistence, no UI side effects beyond an optional stage callback.
    The smoke test calls this directly so it can verify both reviewers
    actually ran and inspect the consolidator's filter behavior.
  - run_cost_reviewer(proposal_id) — sync; thin wrapper over the helper.
    Snapshots inputs, calls the helper with a stage callback wired to
    the proposal's stage banner, then persists findings.
  - spawn_cost_reviewer(proposal_id) — daemon thread launcher for the
    "Run Cost Reviewer" button on the Cost tab.

Prerequisites: pricing packages must exist (Cost Analyst must have
run first). Market scan is optional but improves margin-vs-market
findings.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import select

from app.agents.cost_review_consolidator import (
    consolidate_cost_review_findings,
)
from app.agents.cost_reviewer import (
    CostReviewerInputs,
    CostReviewResult,
    review_cost_build,
)
from app.agents.cost_writer import (
    _format_market_scan_block,
    _format_methodology_block,
    _format_odcs_block,
    _format_phases_block,
)
from app.config import get_settings
from app.core.company_profile import get_company_profile
from app.core.enums import RequirementCategory
from app.db.session import session_scope
from app.jobs.cost_writer import _detect_contract_type_signal
from app.models import (
    ComplianceMatrixItem,
    Proposal,
    ProposalSection,
)
from app.services.cost_reviewer import (
    auto_accept_consensus_findings,
    upsert_cost_review_findings,
)
from app.services.market_scan import get_market_scan_snapshot
from app.services.pricing import get_pricing_packages_snapshot
from app.services.proposal_access import require_proposal_mutable
from app.services.review_freshness import (
    capture_it_cost_review_basis,
    record_cost_review_coverage,
)
from app.services.stages import record_stage as _set_stage

log = logging.getLogger(__name__)


def _build_quadratic_summary() -> str:
    """Compact firm summary tailored for cost review — emphasizes
    size + competitive edge so the reviewer can sanity-check team
    realism (a 12-person small biz is staffed differently than a
    100-person mid-tier prime)."""
    profile = get_company_profile()
    bits: list[str] = []
    name = profile.get("legal_name") or profile.get("name") or "Quadratic Digital"
    bits.append(name)
    if size := profile.get("employee_count") or profile.get("size"):
        bits.append(f"Headcount: {size}")
    if focus := profile.get("market_focus") or profile.get("focus"):
        bits.append(f"Focus: {focus}")
    bits.append(
        "Competitive edge: AI-accelerated custom delivery — leaner "
        "team than typical for the scope, compensated by accelerated "
        "delivery velocity. Reviewer should account for this when "
        "judging team-size realism."
    )
    return ". ".join(bits)


def _format_compliance_block(proposal_id: int) -> str:
    """Render the compliance matrix for the reviewer. Filtered to
    items requiring labor (TECHNICAL / MANAGEMENT / PERSONNEL) since
    those drive scope-coverage findings. Cap at ~6K chars to keep
    the prompt tractable."""
    with session_scope() as db:
        rows = db.execute(
            select(
                ComplianceMatrixItem.requirement_id,
                ComplianceMatrixItem.requirement_text,
                ComplianceMatrixItem.requirement_type,
                ComplianceMatrixItem.category,
            )
            .where(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
            .order_by(ComplianceMatrixItem.id)
        ).all()
    relevant = [
        r
        for r in rows
        if r[3]
        in (
            RequirementCategory.TECHNICAL,
            RequirementCategory.MANAGEMENT,
            RequirementCategory.PERSONNEL,
        )
    ]
    if not relevant:
        return "(no labor-driving compliance items)"

    lines: list[str] = []
    total = 0
    for req_id, text, req_type, category in relevant:
        text = (text or "").strip()
        if not text:
            continue
        snippet = text[:600] + ("..." if len(text) > 600 else "")
        line = f"  [{req_id}] type={req_type} category={category}: {snippet}"
        if total + len(line) > 6000:
            lines.append(f"  ... ({len(relevant) - len(lines)} more items truncated for prompt budget)")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _format_cost_build_block(packages: list[dict], proposed: str) -> str:
    """Render the proposed-scenario cost build for the reviewer.
    Includes labor lines (table form) plus indirect costs + scenario
    aggregates so the reviewer can cross-reference per-line numbers
    against scenario totals."""
    pkg = next((p for p in packages if p["scenario"] == proposed), None)
    if pkg is None:
        return f"  (proposed scenario {proposed} not persisted)"

    lines: list[str] = []
    lines.append(
        f"  Scenario aggregate: price ${float(pkg.get('total_proposed_price') or 0):,.0f} | "
        f"loaded labor cost ${float(pkg.get('loaded_labor_cost') or 0):,.0f} | "
        f"vs market position {pkg.get('vs_market_position') or '?'} | "
        f"bid recommendation {pkg.get('bid_recommendation') or '?'}"
    )
    rec_rationale = (pkg.get("recommendation_rationale") or "").strip()
    if rec_rationale:
        lines.append(f"  Recommendation rationale: {rec_rationale}")
    indirect = pkg.get("indirect_costs_json") or {}
    lines.append(
        f"  Indirect: G&A hourly ${float(indirect.get('ga_hourly_addon_usd') or 0):.2f}/hr | "
        f"G&A total ${float(indirect.get('ga_total_usd') or 0):,.0f} | "
        f"contingency hrs {float(indirect.get('contingency_hours') or 0):,.0f} | "
        f"contingency cost ${float(indirect.get('contingency_cost_usd') or 0):,.0f} | "
        f"profit ${float(indirect.get('profit_usd') or 0):,.0f} ({float(indirect.get('profit_pct') or 0):.1%}) | "
        f"effective margin {float(indirect.get('effective_profit_pct') or indirect.get('profit_pct') or 0):.1%} | "
        f"subtotal cost ${float(indirect.get('total_subtotal_cost_usd') or 0):,.0f}"
    )
    lines.append("")
    lines.append("  Labor lines:")
    lines.append(
        "  | category | salary | coverage | hrs | loaded $/hr | billed $/hr | billed total | rationale |"
    )
    lines.append("  |---|---|---|---|---|---|---|---|")
    for ln in pkg.get("lines") or []:
        rationale = (ln.get("rationale") or "").strip()
        if len(rationale) > 200:
            rationale = rationale[:197] + "..."
        lines.append(
            f"  | {ln.get('labor_category', '?')} | "
            f"{ln.get('wage_band', '?')} | "
            f"{ln.get('coverage_level', '?')} | "
            f"{float(ln.get('hours') or 0):.0f} | "
            f"${float(ln.get('loaded_hourly_rate_usd') or 0):.2f} | "
            f"${float(ln.get('proposed_billing_rate_usd') or 0):.2f} | "
            f"${float(ln.get('billed_total_usd') or 0):,.0f} | "
            f"{rationale} |"
        )
    return "\n".join(lines)


def _format_other_scenarios_block(
    packages: list[dict],
    proposed: str,
) -> str:
    """One-line summary for each non-proposed scenario so the
    reviewer can compare LOW / HIGH against the proposed (typically
    MEDIUM) without needing the full detail."""
    lines: list[str] = []
    for p in packages:
        if p["scenario"] == proposed:
            continue
        indirect = p.get("indirect_costs_json") or {}
        lines.append(
            f"  {p['scenario']}: price "
            f"${float(p.get('total_proposed_price') or 0):,.0f} | "
            f"margin {float(indirect.get('profit_pct') or 0):.1%} | "
            f"position {p.get('vs_market_position') or '?'} | "
            f"recommendation {p.get('bid_recommendation') or '?'} | "
            f"rationale: {(p.get('recommendation_rationale') or '').strip()[:200]}"
        )
    if not lines:
        return "  (only one scenario persisted)"
    return "\n".join(lines)


def _format_drafts_block(proposal_id: int) -> str:
    """Compact list of drafted technical sections — section_id +
    title + brief + has_draft flag. The reviewer uses these to
    sanity-check that proposed labor maps to actual deliverables."""
    with session_scope() as db:
        rows = db.execute(
            select(
                ProposalSection.section_id,
                ProposalSection.section_title,
                ProposalSection.section_brief,
                ProposalSection.requires_cost_analysis,
                ProposalSection.draft_text_markdown,
                ProposalSection.excluded_from_draft,
            )
            .where(
                ProposalSection.proposal_id == proposal_id,
            )
            .order_by(ProposalSection.section_order)
        ).all()
    if not rows:
        return "  (no drafted sections)"
    lines: list[str] = []
    total = 0
    for sid, title, brief, requires_cost, draft, excluded in rows:
        if requires_cost:
            tag = " [cost-deferred]"
        elif excluded:
            tag = " [excluded]"
        elif draft and str(draft).strip():
            tag = " [drafted]"
        else:
            tag = " [outline-only]"
        brief_text = (brief or "").strip()[:300]
        line = f"  - {sid}{tag}: {title}\n      {brief_text}"
        if total + len(line) > 5000:
            lines.append(f"  ... ({len(rows) - len(lines)} more sections truncated for prompt budget)")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _snapshot_cost_reviewer_inputs(
    proposal_id: int,
) -> CostReviewerInputs | None:
    """Build the agent's structured input bundle from persisted data.
    Returns None if pricing packages aren't available (Cost Analyst
    must have run first)."""
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return None
        rfp_title = (prop.title or "").strip()
        rfp_agency = (prop.agency or "").strip()

    packages = get_pricing_packages_snapshot(proposal_id)
    if not packages:
        return None

    market_scan = get_market_scan_snapshot(proposal_id)
    # Read the user's persisted scenario choice; falls back to
    # DEFAULT_PROPOSED_SCENARIO ("MEDIUM") when nothing is set.
    from app.services.pricing import get_proposed_scenario

    proposed = get_proposed_scenario(proposal_id)
    proposed_pkg = next(
        (p for p in packages if p["scenario"] == proposed),
        None,
    )
    if proposed_pkg is None:
        # Defensive — shouldn't happen if Cost Analyst persisted
        # all 3 scenarios, but allow first-found fallback.
        proposed_pkg = packages[0]
        proposed = proposed_pkg["scenario"]

    pop_months = 12  # same default as cost analyst orchestrator
    contract_type_signal = _detect_contract_type_signal(proposal_id)

    return CostReviewerInputs(
        rfp_title=rfp_title,
        rfp_agency=rfp_agency,
        pop_months=pop_months,
        contract_type_signal=contract_type_signal,
        proposed_scenario=proposed,
        compliance_block=_format_compliance_block(proposal_id),
        market_scan_block=_format_market_scan_block(market_scan),
        cost_build_block=_format_cost_build_block(packages, proposed),
        phases_block=_format_phases_block(proposed_pkg),
        odcs_block=_format_odcs_block(proposed_pkg),
        other_scenarios_block=_format_other_scenarios_block(
            packages,
            proposed,
        ),
        drafts_block=_format_drafts_block(proposal_id),
        methodology_block=_format_methodology_block(),
        quadratic_summary=_build_quadratic_summary(),
    )


@dataclass
class DualCostReviewOutcome:
    """Result of dual_review_and_consolidate. Lets callers (and the
    smoke test) introspect every stage of the pipeline:

    - final: the CostReviewResult that should be persisted. Either the
      consolidated consensus subset (happy path), the surviving
      reviewer's findings (one reviewer failed), or the primary's
      findings (consolidator failed).
    - raw_primary / raw_secondary: each reviewer's full output, or
      None if that reviewer raised. Useful for the test to verify both
      models actually ran and to compare the two finding sets.
    - primary_error / secondary_error: stringified exception when that
      reviewer failed. None on success.
    - consolidator_ran: True only if BOTH reviewers succeeded AND the
      consolidator returned without raising. False on either-failure
      fallback or consolidator-failure fallback.
    - consolidator_error: stringified exception from the consolidator
      when it crashed (in which case final == raw_primary).
    """

    final: CostReviewResult
    raw_primary: CostReviewResult | None
    raw_secondary: CostReviewResult | None
    primary_model: str
    secondary_model: str
    primary_error: str | None
    secondary_error: str | None
    consolidator_ran: bool
    consolidator_error: str | None


def dual_review_and_consolidate(
    *,
    proposal_id: int,
    inputs: CostReviewerInputs,
    on_stage: Callable[[str], None] | None = None,
) -> DualCostReviewOutcome:
    """Pure helper: run primary + secondary cost reviewers in parallel,
    then run the consolidator on the consensus subset. No persistence,
    no DB writes beyond the LLM agent_runs records each reviewer makes
    on its own. Stage banners are emitted only via the optional
    `on_stage` callback so the test can pass None or a print shim.

    Raises RuntimeError only when BOTH reviewers fail — in every other
    case returns a populated DualCostReviewOutcome (with appropriate
    fallbacks indicated by the flags).
    """
    settings = get_settings()
    primary_model = settings.model_cost_reviewer
    secondary_model = settings.model_cost_reviewer_secondary

    def _emit(msg: str) -> None:
        if on_stage is not None:
            on_stage(msg)

    _emit(
        f"Cost Reviewer: dual adversarial pass "
        f"({primary_model} + {secondary_model}) on "
        f"{inputs.proposed_scenario} cost build…"
    )

    # Run both reviewers in parallel — same input bundle, two
    # independent models. ThreadPoolExecutor keeps wall time at
    # ~max(primary, secondary) instead of sum.
    result_a: CostReviewResult | None = None
    result_b: CostReviewResult | None = None
    err_a: str | None = None
    err_b: str | None = None
    with ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="cost-reviewer",
    ) as ex:
        fut_a = ex.submit(
            review_cost_build,
            proposal_id=proposal_id,
            inputs=inputs,
            model=primary_model,
        )
        fut_b = ex.submit(
            review_cost_build,
            proposal_id=proposal_id,
            inputs=inputs,
            model=secondary_model,
        )
        try:
            result_a = fut_a.result()
        except Exception as exc:
            err_a = f"{type(exc).__name__}: {exc}"
            log.exception(
                "cost_reviewer (%s) raised: %s",
                primary_model,
                exc,
            )
        try:
            result_b = fut_b.result()
        except Exception as exc:
            err_b = f"{type(exc).__name__}: {exc}"
            log.exception(
                "cost_reviewer (%s) raised: %s",
                secondary_model,
                exc,
            )

    if result_a is None and result_b is None:
        raise RuntimeError(f"Both cost reviewers failed: primary={err_a}, secondary={err_b}")

    consolidator_ran = False
    consolidator_error: str | None = None

    if result_a is None or result_b is None:
        survivor = result_b if result_a is None else result_a
        survivor_model = secondary_model if result_a is None else primary_model
        failed_model = primary_model if result_a is None else secondary_model
        _emit(
            f"Cost Reviewer: {failed_model} pass failed; "
            f"falling back to {survivor_model}-only findings "
            f"(no consensus filter applied this run). Re-run to "
            f"retry the dual-reviewer flow."
        )
        final = survivor
    else:
        _emit(
            f"Cost Reviewer: consolidating {len(result_a.findings)} "
            f"+ {len(result_b.findings)} findings into the "
            f"consensus subset…"
        )
        try:
            final = consolidate_cost_review_findings(
                proposal_id=proposal_id,
                reviewer_a_result=result_a,
                reviewer_a_model=primary_model,
                reviewer_b_result=result_b,
                reviewer_b_model=secondary_model,
            )
            consolidator_ran = True
        except Exception as exc:
            consolidator_error = f"{type(exc).__name__}: {exc}"
            log.exception(
                "cost_review_consolidator failed: %s — falling back to primary-reviewer findings only",
                exc,
            )
            _emit(
                f"Cost Reviewer: consolidator failed "
                f"({type(exc).__name__}); persisting primary "
                f"reviewer's findings without consensus filter."
            )
            final = result_a

    return DualCostReviewOutcome(
        final=final,
        raw_primary=result_a,
        raw_secondary=result_b,
        primary_model=primary_model,
        secondary_model=secondary_model,
        primary_error=err_a,
        secondary_error=err_b,
        consolidator_ran=consolidator_ran,
        consolidator_error=consolidator_error,
    )


def run_cost_reviewer(proposal_id: int) -> None:
    """Sync entry point. Snapshots inputs, runs the dual reviewer +
    consolidator pipeline, persists the final findings. Catches all
    exceptions and surfaces via stage banner."""
    require_proposal_mutable(proposal_id, operation="run cost review")
    log.info("cost reviewer starting for proposal %d", proposal_id)
    try:
        _set_stage(
            proposal_id,
            "Cost Reviewer: snapshotting cost build + scope + market scan…",
        )
        basis_before = capture_it_cost_review_basis(proposal_id)
        inputs = _snapshot_cost_reviewer_inputs(proposal_id)
        if inputs is None:
            _set_stage(
                proposal_id,
                f"Cost Reviewer: prerequisites missing for proposal "
                f"{proposal_id}. Run Cost Analyst first; the reviewer "
                f"needs the persisted cost build to fact-check.",
                status="failed",
            )
            return

        reviewed_basis = capture_it_cost_review_basis(proposal_id)
        if (
            basis_before is None
            or reviewed_basis is None
            or basis_before != reviewed_basis
            or reviewed_basis["scenario"] != inputs.proposed_scenario
        ):
            _set_stage(
                proposal_id,
                "Cost Reviewer: pricing changed while inputs were being "
                "snapshotted. No review evidence was saved; rerun Cost "
                "Reviewer.",
                status="failed",
            )
            return

        outcome = dual_review_and_consolidate(
            proposal_id=proposal_id,
            inputs=inputs,
            on_stage=lambda msg: _set_stage(proposal_id, msg),
        )
        result = outcome.final

        if capture_it_cost_review_basis(proposal_id) != reviewed_basis:
            _set_stage(
                proposal_id,
                "Cost Reviewer: pricing changed while the review was "
                "running. The stale result was discarded; rerun Cost "
                "Reviewer.",
                status="failed",
            )
            return

        n_rows = upsert_cost_review_findings(
            proposal_id=proposal_id,
            result=result,
        )
        # Auto-accept CRITICAL/MAJOR consensus findings so the user
        # opens the Cost Review tab to AUDIT and reject (rather than
        # to click Accept on each one). LLM-tagged minorities and
        # MINOR consensus findings stay pending — those need human
        # judgment. Auto-accepted rows render with an "AUTO" chip
        # so the user can spot what to scrutinize.
        n_auto_accepted = auto_accept_consensus_findings(proposal_id)
        # Bind readiness evidence to the scenario this persisted result
        # actually reviewed. Pricing mutations invalidate older coverage.
        coverage_saved = record_cost_review_coverage(
            proposal_id,
            inputs.proposed_scenario,
            expected_basis=reviewed_basis,
        )
        if not coverage_saved:
            _set_stage(
                proposal_id,
                "Cost Reviewer: pricing changed before review evidence "
                "could be committed. Submission remains blocked; rerun "
                "Cost Reviewer.",
                status="failed",
            )
            return

        # Stage banner — surface count + severity breakdown so user
        # knows what to look at on the Cost tab.
        sev_counts: dict[str, int] = {}
        for f in result.findings:
            sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
        if not result.findings:
            _set_stage(
                proposal_id,
                "Cost Reviewer: clean — no findings on the cost build. "
                "Open the Cost tab to see the 'no findings' state.",
            )
        else:
            sev_str = " · ".join(
                f"{count} {sev.lower()}"
                for sev in ("CRITICAL", "MAJOR", "MINOR")
                if (count := sev_counts.get(sev, 0)) > 0
            )
            auto_suffix = (
                f" — {n_auto_accepted} CRITICAL/MAJOR auto-accepted (audit before drafting)"
                if n_auto_accepted
                else ""
            )
            _set_stage(
                proposal_id,
                f"Cost Reviewer complete: {len(result.findings)} "
                f"finding(s) ({sev_str}) — {n_rows} row(s) persisted "
                f"across affected scenarios{auto_suffix}. Open the "
                f"Cost tab.",
            )
        log.info(
            "cost reviewer: proposal %d done — %d findings, %d rows persisted",
            proposal_id,
            len(result.findings),
            n_rows,
        )
    except Exception:
        log.exception(
            "cost reviewer failed for proposal %d",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            "Cost Reviewer failed — check logs.",
            status="failed",
        )


def spawn_cost_reviewer(proposal_id: int) -> threading.Thread:
    """Daemon thread launcher for the 'Run Cost Reviewer' button."""
    t = threading.Thread(
        target=run_cost_reviewer,
        args=(proposal_id,),
        name=f"cost-reviewer-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


__all__ = [
    "DualCostReviewOutcome",
    "dual_review_and_consolidate",
    "run_cost_reviewer",
    "spawn_cost_reviewer",
]
