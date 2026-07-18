"""Live end-to-end smoke test for the Cost Market Researcher.

Hits the REAL Gemini 3.5 Pro grounded API + Haiku structuring API.
Costs ~$0.15-0.30 per run. Use to validate:

  1. The prompt template produces a structured brief Gemini can ground.
  2. The Haiku structuring tool schema parses the brief without
     dropping all rows.
  3. Comparable awards have source URLs (citation requirement).
  4. Competitors have rate inference math + URLs.
  5. The sparse-data warning fires correctly when <3 awards.
  6. Persisted MarketScan + detail rows match what the agent returned.

Usage:
    cd <project root>
    .venv\\Scripts\\python.exe scripts\\_e2e_market_research_test.py --live --proposal-id 42
    .venv\\Scripts\\python.exe scripts\\_e2e_market_research_test.py --live --latest

Reads from your local DB. Does NOT mock anything. Re-running replaces
the persisted scan for the proposal (per the unique constraint).

Required env: GEMINI_API_KEY (or GOOGLE_API_KEY) + ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import sys
import textwrap

from _e2e_live_helpers import add_live_args, pick_proposal_id, require_api_keys, require_live

from app.agents.market_researcher import research_market
from app.jobs.market_researcher import _snapshot_market_research_inputs
from app.services.market_scan import (
    get_market_scan_snapshot,
    upsert_market_scan,
)


def _print_inputs(inputs) -> None:
    print()
    print("=" * 70)
    print("MARKET RESEARCH INPUTS")
    print("=" * 70)
    print(f"  rfp_title:       {inputs.rfp_title or '(none)'}")
    print(f"  rfp_agency:      {inputs.rfp_agency or '(none)'}")
    print(f"  naics:           {inputs.naics or '(none)'}")
    print(f"  pop_months:      {inputs.pop_months}")
    print(f"  est_fte:         {inputs.est_fte}")
    print(f"  est_value range: ${inputs.est_value_low_usd:,.0f} - ${inputs.est_value_high_usd:,.0f}")
    print(f"  scope_summary:   {len(inputs.scope_summary)} chars")
    if inputs.scope_summary:
        snippet = textwrap.shorten(
            inputs.scope_summary,
            width=400,
            placeholder="…",
        )
        print(f"    └─ preview:  {snippet}")
    print(f"  quadratic_summary: {inputs.quadratic_summary[:140]}…")


def _print_result(result) -> None:
    print()
    print("=" * 70)
    print("AGENT RESULT (in-memory, before persistence)")
    print("=" * 70)
    print(f"  market_band_low_usd:  ${_fmt_dollars(result.market_band_low_usd)}")
    print(f"  market_band_mid_usd:  ${_fmt_dollars(result.market_band_mid_usd)}")
    print(f"  market_band_high_usd: ${_fmt_dollars(result.market_band_high_usd)}")
    print(f"  insufficient_data_warning: {result.insufficient_data_warning!r}")
    print(f"  methodology: {(result.methodology or '')[:300]}")
    print()
    print(f"  comparable_awards: {len(result.comparable_awards)}")
    for i, a in enumerate(result.comparable_awards, 1):
        print(f"    [{i}] {a.award_title[:60]}")
        print(
            f"        value=${_fmt_dollars(a.award_value_usd)} | "
            f"PoP={a.period_of_performance_months}mo | "
            f"awardee={a.awardee_name} | rel={a.relevance_score}"
        )
        print(f"        url={a.source_url}")
        if a.notes:
            print(f"        notes={a.notes[:120]}")
    print()
    print(f"  competitors: {len(result.competitors)}")
    for i, c in enumerate(result.competitors, 1):
        print(f"    [{i}] {c.name} (likelihood={c.likelihood_to_bid})")
        rate_low = f"${c.estimated_rate_low_usd}/hr" if c.estimated_rate_low_usd is not None else "?"
        rate_high = f"${c.estimated_rate_high_usd}/hr" if c.estimated_rate_high_usd is not None else "?"
        print(f"        est_rate={rate_low} - {rate_high}")
        if c.rate_estimation_basis:
            print(f"        basis={c.rate_estimation_basis[:160]}")
        for u in c.source_urls:
            print(f"        url={u}")


def _fmt_dollars(v) -> str:
    if v is None:
        return "(none)"
    return f"{float(v):,.0f}"


def _validate_invariants(result) -> list[str]:
    """Check that the result obeys the contracts the prompt promises.
    Returns a list of failure messages (empty = passed)."""
    failures: list[str] = []

    if not result.methodology or not result.methodology.strip():
        failures.append("methodology is empty — agent should always explain.")

    for i, a in enumerate(result.comparable_awards):
        if not (a.source_url or "").strip():
            failures.append(
                f"comparable_award[{i}] '{a.award_title[:40]}' has no "
                f"source_url — should have been dropped by the agent."
            )
        if a.relevance_score is not None and not (0.0 <= a.relevance_score <= 1.0):
            failures.append(f"comparable_award[{i}] relevance_score={a.relevance_score} out of [0,1] range.")

    for i, c in enumerate(result.competitors):
        if not c.source_urls:
            failures.append(
                f"competitor[{i}] '{c.name}' has no source_urls — should have been dropped by the agent."
            )
        if c.likelihood_to_bid not in ("high", "medium", "low"):
            failures.append(
                f"competitor[{i}] likelihood_to_bid='{c.likelihood_to_bid}' not one of high/medium/low."
            )
        # Rate range sanity: if both populated, low <= high.
        if (
            c.estimated_rate_low_usd is not None
            and c.estimated_rate_high_usd is not None
            and c.estimated_rate_low_usd > c.estimated_rate_high_usd
        ):
            failures.append(
                f"competitor[{i}] '{c.name}' rate_low="
                f"{c.estimated_rate_low_usd} > rate_high="
                f"{c.estimated_rate_high_usd}."
            )
        if (c.estimated_rate_low_usd is not None or c.estimated_rate_high_usd is not None) and not (
            c.rate_estimation_basis or ""
        ).strip():
            failures.append(f"competitor[{i}] '{c.name}' has rate values but no rate_estimation_basis.")

    # Sparse-data invariant: if <3 awards and no warning, the post-process
    # in the agent should have set one automatically.
    if len(result.comparable_awards) < 3 and not result.insufficient_data_warning:
        failures.append(
            f"only {len(result.comparable_awards)} comparable_awards but "
            f"insufficient_data_warning is None — auto-warn should fire."
        )

    return failures


def _verify_persistence(proposal_id: int, result) -> list[str]:
    """Read back the persisted scan and confirm row counts match what
    we just wrote. Catches any cascade-delete or upsert bugs."""
    failures: list[str] = []
    snap = get_market_scan_snapshot(proposal_id)
    if snap is None:
        failures.append("get_market_scan_snapshot returned None after upsert.")
        return failures
    if len(snap["comparable_awards"]) != len(result.comparable_awards):
        failures.append(
            f"persisted awards={len(snap['comparable_awards'])} != in-memory={len(result.comparable_awards)}"
        )
    if len(snap["competitors"]) != len(result.competitors):
        failures.append(
            f"persisted competitors={len(snap['competitors'])} != in-memory={len(result.competitors)}"
        )
    if snap["market_band_low_usd"] != result.market_band_low_usd:
        failures.append(
            f"persisted band_low={snap['market_band_low_usd']} != in-memory={result.market_band_low_usd}"
        )
    return failures


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cost Market Researcher live E2E smoke test.")
    add_live_args(parser)
    return parser.parse_args(argv[1:])


def main() -> int:
    args = _parse_args(sys.argv)
    if not require_live(args, script_name="Cost Market Researcher", estimated_cost="$0.15-0.30"):
        return 2
    if not require_api_keys(["gemini", "anthropic"]):
        return 2

    proposal_id = pick_proposal_id(args)
    if proposal_id is None:
        return 1

    print(f"** running Cost Market Researcher against proposal {proposal_id} **")

    inputs = _snapshot_market_research_inputs(proposal_id)
    if inputs is None:
        print(f"!! proposal {proposal_id} not found in DB.")
        return 1
    _print_inputs(inputs)

    from app.config import get_settings as _gs

    print()
    print(f">> calling {_gs().model_market_researcher} grounded + Haiku structuring (may take 30-90s)...")
    try:
        result = research_market(
            proposal_id=proposal_id,
            inputs=inputs,
        )
    except Exception as exc:
        print(f"!! agent raised: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 2

    _print_result(result)

    # ---- Invariants on the in-memory result ----
    print()
    print("=" * 70)
    print("INVARIANT CHECKS (on in-memory result)")
    print("=" * 70)
    failures = _validate_invariants(result)
    if failures:
        print("!! INVARIANT FAILURES:")
        for f in failures:
            print(f"   - {f}")
    else:
        print("   all invariants passed.")

    # ---- Persist + verify ----
    print()
    print(">> upserting scan to DB...")
    scan_id = upsert_market_scan(
        proposal_id=proposal_id,
        result=result,
        agent_run_id=None,
    )
    print(f"   scan_id={scan_id}")

    print()
    print("=" * 70)
    print("PERSISTENCE CHECKS")
    print("=" * 70)
    persist_failures = _verify_persistence(proposal_id, result)
    if persist_failures:
        print("!! PERSISTENCE FAILURES:")
        for f in persist_failures:
            print(f"   - {f}")
    else:
        print("   row counts + band match in-memory result.")

    total_failures = len(failures) + len(persist_failures)
    print()
    if total_failures == 0:
        print(
            f"** PASS — {len(result.comparable_awards)} awards, "
            f"{len(result.competitors)} competitors persisted to "
            f"market_scan id={scan_id}. **"
        )
        return 0
    print(f"** FAIL — {total_failures} invariant/persistence failure(s) (see above). **")
    return 3


if __name__ == "__main__":
    sys.exit(main())
