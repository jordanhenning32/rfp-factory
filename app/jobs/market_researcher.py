"""Cost Market Researcher orchestration.

Two entry points:
  - run_market_research(proposal_id) — sync; called by background thread
  - spawn_market_research(proposal_id) — daemon thread launcher for the
    "Run Market Research" button on the Cost tab

Builds the MarketResearchInputs snapshot from the proposal +
compliance matrix, runs the two-step agent, and persists the result
via app.services.market_scan. Stage banners surface progress + sparse-
data warnings to the UI.

Re-running replaces the existing scan (per the unique constraint on
market_scans.proposal_id). Idempotent in the sense that two runs in
a row produce two scans with similar content; the second wins.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from app.agents.market_consolidator import consolidate_market_research
from app.agents.market_researcher import (
    MarketResearchInputs,
    MarketScanResult,
    research_market,
)
from app.agents.market_researcher_claude import research_market_claude
from app.config import get_settings
from app.core.company_profile import get_company_profile
from app.core.enums import RequirementCategory
from app.db.session import session_scope
from app.models import (
    ComplianceMatrixItem,
    Proposal,
    RfpPackageDocument,
)
from app.services.market_scan import upsert_market_scan
from app.services.proposal_access import require_proposal_mutable
from app.services.stages import record_stage as _set_stage

log = logging.getLogger(__name__)


# Default period-of-performance assumption when nothing is extracted.
# 12 months is the common federal task-order PoP — over-estimate is
# safer than under (drives value-range up, gives Gemini broader query
# scope to work with). Agent can refine via grounded search.
_DEFAULT_POP_MONTHS = 12

# Loose bounds on the rough contract-value estimate the agent uses for
# query scoping. Wide on purpose — the WHOLE POINT of the agent is to
# narrow the band. This is just to keep Gemini's queries in the right
# ballpark (federal IT services, not consumer goods or hardware).
_FALLBACK_RATE_LOW_USD_PER_HR = 100.0
_FALLBACK_RATE_HIGH_USD_PER_HR = 200.0


def _snapshot_market_research_inputs(
    proposal_id: int,
) -> MarketResearchInputs | None:
    """Build the agent's input bundle from the proposal + compliance
    matrix + main solicitation text. Returns None if the proposal
    doesn't exist.
    """
    with session_scope() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            return None

        rfp_title = (prop.title or "").strip()
        rfp_agency = (prop.agency or "").strip()
        naics = (prop.naics or "").strip()

        # Pull compliance items — used to count personnel mentions for
        # FTE inference and to assemble a brief scope summary.
        rows = db.execute(
            select(
                ComplianceMatrixItem.requirement_text,
                ComplianceMatrixItem.category,
            ).where(
                ComplianceMatrixItem.proposal_id == proposal_id,
                ComplianceMatrixItem.status == "active",
            )
        ).all()

        # Brief scope = first ~2.5K chars of TECHNICAL / MANAGEMENT
        # requirement text. Avoids pulling form-fill / certification
        # items that don't describe scope.
        scope_chunks: list[str] = []
        scope_total = 0
        n_personnel_items = 0
        for text, category in rows:
            if category == RequirementCategory.PERSONNEL:
                n_personnel_items += 1
            if category in (
                RequirementCategory.TECHNICAL,
                RequirementCategory.MANAGEMENT,
            ):
                t = (text or "").strip()
                if not t:
                    continue
                if scope_total + len(t) + 2 <= 2500:
                    scope_chunks.append(t)
                    scope_total += len(t) + 2
        scope_summary = "\n\n".join(scope_chunks)

        # Pull the main solicitation's preamble as a fallback when
        # compliance-derived scope is thin.
        if not scope_summary:
            doc = db.execute(
                select(RfpPackageDocument.extracted_text_md)
                .where(
                    RfpPackageDocument.rfp_package_id == prop.rfp_package_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            scope_summary = (doc or "")[:2500]

    # Heuristic estimates — agent refines via grounded search.
    # FTE: at least 3 (one PM + 2 ICs), more when personnel-heavy.
    est_fte = max(3.0, float(n_personnel_items) / 2.0)
    pop_months = _DEFAULT_POP_MONTHS

    annual_hours_per_fte = 1950
    est_value_low = est_fte * annual_hours_per_fte * _FALLBACK_RATE_LOW_USD_PER_HR * (pop_months / 12.0)
    est_value_high = est_fte * annual_hours_per_fte * _FALLBACK_RATE_HIGH_USD_PER_HR * (pop_months / 12.0)

    quadratic_summary = _build_quadratic_summary()

    return MarketResearchInputs(
        rfp_title=rfp_title,
        rfp_agency=rfp_agency,
        naics=naics,
        pop_months=pop_months,
        est_fte=est_fte,
        est_value_low_usd=est_value_low,
        est_value_high_usd=est_value_high,
        scope_summary=scope_summary,
        quadratic_summary=quadratic_summary,
    )


def _build_quadratic_summary() -> str:
    """Compact firm summary for the market-research prompt — same
    pattern as intake._quadratic_summary_for_research, kept local to
    avoid cross-module underscore-import. Drift between the two is
    fine: each agent gets a summary tailored to what it cares about."""
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
    bits.append(
        "Competitive edge: AI-accelerated custom delivery — COTS-like "
        "speed and predictability without COTS rigidity."
    )
    return ". ".join(bits)


def run_market_research(proposal_id: int) -> None:
    """Sync entry point. Builds inputs, runs the agent, persists the
    result. Catches all exceptions and surfaces via stage banner."""
    require_proposal_mutable(
        proposal_id, operation="run cost market research",
    )
    log.info("market researcher starting for proposal %d", proposal_id)
    try:
        _set_stage(
            proposal_id,
            "Cost Market Researcher: building research inputs…",
        )
        inputs = _snapshot_market_research_inputs(proposal_id)
        if inputs is None:
            _set_stage(
                proposal_id,
                f"Market research: proposal {proposal_id} not found.",
                status="failed",
            )
            return

        settings = get_settings()
        _set_stage(
            proposal_id,
            f"Cost Market Researcher (Gemini "
            f"{settings.model_market_researcher} + Claude "
            f"{settings.model_market_researcher_b} dual-pipeline): "
            f"researching comparable awards + competitor rates for "
            f"'{inputs.rfp_title or '(untitled)'}'…",
        )

        # Fan out the two providers concurrently. Wall-clock per scan
        # is max(A, B) ~= 60-90s. Per-provider failure degrades
        # gracefully — the consolidator unions whatever results we got,
        # so a transient outage on one side doesn't lose the scan.
        empty_result = MarketScanResult(
            market_band_low_usd=None,
            market_band_mid_usd=None,
            market_band_high_usd=None,
            methodology="",
        )
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"market-research-{proposal_id}",
        ) as inner:
            fut_a = inner.submit(
                research_market,
                proposal_id=proposal_id,
                inputs=inputs,
            )
            fut_b = inner.submit(
                research_market_claude,
                proposal_id=proposal_id,
                inputs=inputs,
            )
            try:
                pass_a = fut_a.result()
            except Exception:
                log.exception(
                    "market_researcher_a (gemini): proposal %d failed; consolidating with B-only results.",
                    proposal_id,
                )
                pass_a = empty_result
            try:
                pass_b = fut_b.result()
            except Exception:
                log.exception(
                    "market_researcher_b (claude): proposal %d failed; consolidating with A-only results.",
                    proposal_id,
                )
                pass_b = empty_result

        # If both failed, surface that explicitly — the consolidator
        # would otherwise produce an empty result with no warning.
        if (
            not pass_a.comparable_awards
            and not pass_a.competitors
            and not pass_b.comparable_awards
            and not pass_b.competitors
        ):
            raise RuntimeError(
                "market_researcher: both providers returned empty results. Check agent_runs for details."
            )

        result = consolidate_market_research(
            proposal_id=proposal_id,
            pass_a=pass_a,
            pass_b=pass_b,
            target_pop_months=inputs.pop_months,
        )

        # Persist. agent_run_id linkage would require threading the
        # agent_runs row id through the agent's return value — defer
        # that polish until we know we need cross-link queries.
        scan_id = upsert_market_scan(
            proposal_id=proposal_id,
            result=result,
            agent_run_id=None,
        )

        # Stage banner — surface key numbers so the user knows what
        # to look at on the Cost tab without opening it.
        band_str = "no band"
        if result.market_band_low_usd is not None and result.market_band_high_usd is not None:
            band_str = f"${result.market_band_low_usd:,.0f}–${result.market_band_high_usd:,.0f}"

        warning_str = ""
        if result.insufficient_data_warning:
            warning_str = " · ⚠ sparse data — band weakly grounded"

        _set_stage(
            proposal_id,
            f"Market research complete: {len(result.comparable_awards)} "
            f"comparable award(s) · {len(result.competitors)} "
            f"competitor(s) · band {band_str}{warning_str}. Open the "
            f"Cost tab.",
        )
        log.info(
            "market researcher: proposal %d done, scan_id=%d",
            proposal_id,
            scan_id,
        )
    except Exception:
        log.exception(
            "market research failed for proposal %d",
            proposal_id,
        )
        _set_stage(
            proposal_id,
            "Market research failed — check logs.",
            status="failed",
        )


def spawn_market_research(proposal_id: int) -> threading.Thread:
    """Daemon thread launcher for the 'Run Market Research' button on
    the Cost tab. UI handler returns immediately while Gemini does
    its grounded calls."""
    t = threading.Thread(
        target=run_market_research,
        args=(proposal_id,),
        name=f"market-research-{proposal_id}",
        daemon=True,
    )
    t.start()
    return t


__all__ = [
    "run_market_research",
    "spawn_market_research",
]
