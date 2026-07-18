"""Payment-Systems Cost Reviewer orchestrator.

Runs the payment-aware adversarial cost review when service_line=
payment_systems. Single Sonnet 4.6 call (no dual pipeline for MVP);
the labor flow's Gemini-Pro + GPT-5.5 dual-pipeline pattern can be
added later if quality demands it.

Pipeline:
  1. Service-line gate — bail with stage message when proposal is
     not service_line=payment_systems.
  2. Snapshot inputs — load drafted cost-deferred section markdown
     from ProposalSection rows, the persisted payment market scan,
     and both payment_systems data files.
  3. Call agents.payment_cost_reviewer.review_payment_cost.
  4. Persist findings JSON to proposals.payment_cost_review_findings_
     json. Cost Review tab renders findings from this column.

Failure modes surface via _set_stage and the Run Progress page.
"""

from __future__ import annotations

import json
import logging
import threading

from app.agents.payment_cost_reviewer import (
    PaymentCostReviewInputs,
    review_payment_cost,
)
from app.db.session import session_scope
from app.models import Proposal, ProposalSection
from app.services.service_line import (
    SERVICE_LINE_PAYMENT_SYSTEMS,
    get_service_line,
    load_payment_systems_context,
    load_payment_systems_pricing,
)
from app.services.stages import record_stage as _set_stage

log = logging.getLogger(__name__)


def spawn_payment_cost_reviewer(proposal_id: int) -> None:
    """Fire the payment-systems cost reviewer in a daemon thread.
    Mirrors the spawn_payment_market_research / spawn_intake pattern."""
    t = threading.Thread(
        target=run_payment_cost_reviewer,
        args=(proposal_id,),
        name=f"payment-cost-reviewer-{proposal_id}",
        daemon=True,
    )
    t.start()


def run_payment_cost_reviewer(proposal_id: int) -> None:
    """Sync entry point. Builds inputs, runs the agent, persists.
    All exceptions surface via the stage banner."""
    log.info(
        "payment_cost_reviewer starting for proposal %d",
        proposal_id,
    )
    try:
        if get_service_line(proposal_id) != SERVICE_LINE_PAYMENT_SYSTEMS:
            _set_stage(
                proposal_id,
                "Payment Cost Reviewer: proposal is not service_line=payment_systems; skipping.",
            )
            return

        _set_stage(
            proposal_id,
            "Payment Cost Reviewer: loading drafted section(s) + scan + pricing data…",
        )
        inputs = _snapshot_inputs(proposal_id)
        if inputs is None:
            _set_stage(
                proposal_id,
                f"Payment Cost Reviewer: proposal {proposal_id} not found.",
            )
            return
        if not inputs.sections:
            _set_stage(
                proposal_id,
                "Payment Cost Reviewer: no cost-deferred sections "
                "drafted yet — run Cost Volume Writer first.",
            )
            return

        _set_stage(
            proposal_id,
            f"Payment Cost Reviewer (Sonnet adversarial review): "
            f"fact-checking {len(inputs.sections)} drafted "
            f"section(s) against the payment market scan + "
            f"compliance posture + brand framing…",
        )
        result = review_payment_cost(
            proposal_id=proposal_id,
            inputs=inputs,
        )

        _persist_result(proposal_id, result)

        n_findings = len(result.findings)
        n_critical = sum(1 for f in result.findings if f.severity == "CRITICAL")
        n_major = sum(1 for f in result.findings if f.severity == "MAJOR")
        n_minor = sum(1 for f in result.findings if f.severity == "MINOR")
        if n_findings == 0:
            verdict = (
                "no findings — narrative is bid-ready"
                if result.bid_ready
                else "no findings, but reviewer flagged not bid-ready"
            )
        else:
            verdict = f"{n_findings} finding(s) — {n_critical} CRITICAL, {n_major} MAJOR, {n_minor} MINOR" + (
                " — narrative is bid-ready" if result.bid_ready else " — review before submission"
            )
        _set_stage(
            proposal_id,
            f"Payment Cost Reviewer: review complete — {verdict}. Open the Cost Review tab to triage.",
        )

    except Exception as exc:
        log.exception(
            "payment_cost_reviewer failed for proposal %d",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            f"Payment Cost Reviewer: failed — {exc}",
        )


# ---- Inputs snapshot ----------------------------------------------------


def _snapshot_inputs(proposal_id: int) -> PaymentCostReviewInputs | None:
    """Load drafted cost-deferred sections + persisted scan + pricing
    data files into a structured input bundle. Returns None when the
    proposal isn't found."""
    pricing = load_payment_systems_pricing()
    context = load_payment_systems_context()

    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return None
        rfp_title = p.title or ""
        rfp_agency = p.agency or ""
        scan_raw = p.payment_market_scan_json or ""
        section_rows = (
            db.query(ProposalSection)
            .filter(
                ProposalSection.proposal_id == proposal_id,
                ProposalSection.requires_cost_analysis.is_(True),
            )
            .order_by(ProposalSection.section_order)
            .all()
        )
        sections = [
            {
                "section_id": s.section_id,
                "section_title": s.section_title or "",
                "draft_markdown": s.draft_text_markdown or "",
            }
            for s in section_rows
            if (s.draft_text_markdown or "").strip()
        ]

    payment_market_scan: dict = {}
    if scan_raw.strip():
        try:
            payment_market_scan = json.loads(scan_raw)
        except json.JSONDecodeError:
            log.warning(
                "payment_market_scan_json invalid JSON on proposal %d",
                proposal_id,
            )

    return PaymentCostReviewInputs(
        rfp_title=rfp_title,
        rfp_agency=rfp_agency,
        sections=sections,
        payment_market_scan=payment_market_scan,
        payment_systems_pricing=pricing,
        payment_systems_context=context,
    )


# ---- Persistence --------------------------------------------------------


def _persist_result(proposal_id: int, result) -> None:
    """Serialize the result and write to proposals.payment_cost_
    review_findings_json. Replaces any prior review in full — re-
    running overwrites rather than appends."""
    payload = json.dumps(result.to_json_dict(), indent=2, default=str)
    with session_scope() as db:
        p = db.get(Proposal, proposal_id)
        if p is None:
            return
        p.payment_cost_review_findings_json = payload


__all__ = [
    "spawn_payment_cost_reviewer",
    "run_payment_cost_reviewer",
]
