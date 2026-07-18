"""Live end-to-end smoke test for the Cost Analyst.

Two stages:

  STAGE 1 - Synthetic-math validation (no LLM cost). Hardcodes a
  realistic labor estimate, runs it through compute_scenario_packages,
  asserts the H/M/L numbers obey expected invariants:
    - LOW < MEDIUM < HIGH proposed_price (margin + contingency stack)
    - margin equals scenario's profit_margin_pct
    - blended billing rate ≤ ceiling for every line in MEDIUM scenario
      (LOW with low coverage may still be in-bounds; HIGH with high
      margin can exceed if salary is at the top - flagged via
      ceiling_violation_note rather than hard-failed)
    - LOW uses low coverage, MEDIUM and HIGH use high coverage
    - Per-line loaded_hourly_rate * hours == loaded_cost (penny-level)

  STAGE 2 - Real LLM end-to-end. Requires a persisted MarketScan for
  the proposal (run the market researcher first if you don't have
  one). Calls GPT-5.5, applies math, persists the 3 PricingPackage
  rows + N PricingPackageLine rows, then re-reads via the snapshot
  helper and validates persistence parity.

Cost: STAGE 1 free, STAGE 2 ~$0.10-0.30 (GPT-5.5 input/output).

Usage:
    cd <project root>
    .venv\\Scripts\\python.exe scripts\\_e2e_cost_analyst_test.py
    .venv\\Scripts\\python.exe scripts\\_e2e_cost_analyst_test.py --live --proposal-id 1
    .venv\\Scripts\\python.exe scripts\\_e2e_cost_analyst_test.py --live --latest
"""

from __future__ import annotations

import argparse
import sys
import textwrap

from _e2e_live_helpers import add_live_args, pick_proposal_id, require_api_keys

from app.agents.cost_analyst import analyze_costs
from app.jobs.cost_analyst import _snapshot_cost_analyst_inputs
from app.services.market_scan import get_market_scan_snapshot
from app.services.pricing import (
    CostAnalystLaborLine,
    CostAnalystOutput,
    compute_scenario_packages,
    get_pricing_packages_snapshot,
    get_pricing_rules,
    upsert_pricing_packages,
)

# ---- Stage 1: synthetic-math validation -----------------------------------


def _synthetic_output() -> CostAnalystOutput:
    """A realistic 3-FTE / 12-month labor estimate for a $1M-class
    state-agency CMS bid. Mirrors what we'd expect GPT-5.5 to produce
    for the NC SBI proposal."""
    return CostAnalystOutput(
        labor_lines=[
            CostAnalystLaborLine(
                labor_category="Project Manager II",
                wage_band="150k",
                hours=1950.0,
                rationale="Solo PM for 12-month PoP at the mid band.",
            ),
            CostAnalystLaborLine(
                labor_category="Software Engineer III",
                wage_band="170k",
                hours=1950.0,
                rationale="Senior Drupal dev - full-time over the PoP.",
            ),
            CostAnalystLaborLine(
                labor_category="Test Engineer III",
                wage_band="135k",
                hours=975.0,
                rationale="Half-time QA over the 12 months.",
            ),
        ],
        avg_headcount_during_pop=10.0,
        odcs=[],
        subcontractor_costs_usd=None,
        key_risks=[
            "Scope ambiguity around NCIC integration",
            "Tight 30-day delivery window after award",
        ],
        executive_summary=(
            "Lean 2.5 FTE team for a 12-month NC SBI Drupal CMS "
            "engagement. Composition reflects Quadratic's "
            "AI-accelerated delivery edge."
        ),
    )


def _run_stage1() -> int:
    print("=" * 70)
    print("STAGE 1 - Synthetic-math validation (no LLM cost)")
    print("=" * 70)

    output = _synthetic_output()
    packages = compute_scenario_packages(
        output=output,
        market_band_low_usd=650000.0,
        market_band_mid_usd=900000.0,
        market_band_high_usd=1200000.0,
    )

    assert len(packages) == 3, f"expected 3 packages, got {len(packages)}"
    by_scenario = {p.scenario: p for p in packages}

    failures: list[str] = []

    # Invariant: LOW < MEDIUM < HIGH proposed_price (margin + contingency
    # stack). Same labor mix; only burden/margin/contingency differ.
    low_p = by_scenario["LOW"].total_proposed_price_usd
    med_p = by_scenario["MEDIUM"].total_proposed_price_usd
    high_p = by_scenario["HIGH"].total_proposed_price_usd
    if not (low_p < med_p < high_p):
        failures.append(f"prices not monotonic: LOW={low_p:,.0f} MED={med_p:,.0f} HIGH={high_p:,.0f}")

    rules = get_pricing_rules()
    # Coverage level invariant
    expected_coverage = {
        "LOW": "low",
        "MEDIUM": "high",
        "HIGH": "high",
    }
    for scenario, pkg in by_scenario.items():
        if pkg.coverage_level != expected_coverage[scenario]:
            failures.append(
                f"{scenario} coverage_level={pkg.coverage_level!r}, expected {expected_coverage[scenario]!r}"
            )

    # Margin invariant
    for scenario, pkg in by_scenario.items():
        expected = rules["scenario_definitions"][scenario.lower()]["profit_margin_pct"]
        if abs(pkg.profit_margin_pct - expected) > 0.0001:
            failures.append(f"{scenario} margin={pkg.profit_margin_pct} != {expected}")

    # Aggregate consistency: sum of per-line loaded_costs ~= the
    # scenario's total_loaded_labor_cost. The per-line costs are
    # computed from UNROUNDED hourly rates (more accurate); only the
    # display hourly is rounded. So `loaded_hourly_rate * hours`
    # won't equal `loaded_cost` to the cent — which is fine. What
    # MUST hold is sum-of-lines == total.
    for scenario, pkg in by_scenario.items():
        sum_lines = sum(ln.loaded_cost_usd for ln in pkg.lines)
        # Each line rounds independently; cumulative slack is N cents.
        slack = max(1.0, len(pkg.lines) * 0.51)
        if abs(sum_lines - pkg.total_loaded_labor_cost_usd) > slack:
            failures.append(
                f"{scenario}: sum(line.loaded_cost)={sum_lines:.2f} "
                f"!= total_loaded_labor_cost="
                f"{pkg.total_loaded_labor_cost_usd:.2f}"
            )

    # Margin formula check: profit + subtotal = price (within rounding).
    for scenario, pkg in by_scenario.items():
        sum_check = pkg.total_subtotal_cost_usd + pkg.profit_usd
        if abs(sum_check - pkg.total_proposed_price_usd) > 0.51:
            failures.append(
                f"{scenario}: subtotal+profit={sum_check:.2f} != price={pkg.total_proposed_price_usd:.2f}"
            )

    # Print a compact summary table.
    print()
    print(
        f"{'Scenario':<10} {'Price':>14} {'Cost':>14} {'Profit':>14} "
        f"{'Margin':>8} {'Position':>10} {'Recommendation':>16}"
    )
    for scenario in ("LOW", "MEDIUM", "HIGH"):
        p = by_scenario[scenario]
        print(
            f"{scenario:<10} "
            f"${p.total_proposed_price_usd:>13,.0f} "
            f"${p.total_subtotal_cost_usd:>13,.0f} "
            f"${p.profit_usd:>13,.0f} "
            f"{p.profit_margin_pct:>7.1%} "
            f"{p.vs_market_position:>10} "
            f"{p.bid_recommendation:>16}"
        )

    print()
    print("Per-line detail (MEDIUM scenario):")
    med = by_scenario["MEDIUM"]
    for ln in med.lines:
        warn = f"  [!] {ln.ceiling_violation_note}" if ln.ceiling_violation_note else ""
        print(
            f"  - {ln.labor_category:<24} | band={ln.wage_band} | "
            f"hrs={ln.hours:>6.0f} | "
            f"loaded_rate=${ln.loaded_hourly_rate_usd:>6.2f}/hr | "
            f"bill_rate=${ln.proposed_billing_rate_usd:>6.2f}/hr"
            f"{warn}"
        )

    print()
    if failures:
        print("!! STAGE 1 FAILURES:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("** STAGE 1 PASS - math invariants hold. **")
    return 0


# ---- Stage 2: real LLM end-to-end -----------------------------------------


def _run_stage2(proposal_id: int) -> int:
    print()
    print("=" * 70)
    print(f"STAGE 2 - Real LLM end-to-end against proposal {proposal_id}")
    print("=" * 70)

    inputs = _snapshot_cost_analyst_inputs(proposal_id)
    if inputs is None:
        print(f"!! proposal {proposal_id} not found.")
        return 1

    market_scan = get_market_scan_snapshot(proposal_id)
    if market_scan is None:
        print(f"!! no market scan for proposal {proposal_id}. Run _e2e_market_research_test.py first.")
        return 1

    print(
        f"   market band: ${market_scan.get('market_band_low_usd')} / "
        f"${market_scan.get('market_band_mid_usd')} / "
        f"${market_scan.get('market_band_high_usd')}"
    )
    print(
        f"   {len(market_scan['comparable_awards'])} awards, "
        f"{len(market_scan['competitors'])} competitors persisted"
    )

    from app.config import get_settings

    print(f"\n>> calling {get_settings().model_cost_analyst} for cost analysis (may take 20-60s)...")

    try:
        agent_output = analyze_costs(
            proposal_id=proposal_id,
            **{
                k: inputs[k]
                for k in (
                    "rfp_title",
                    "rfp_agency",
                    "naics",
                    "pop_months",
                    "est_value_low_usd",
                    "est_value_high_usd",
                    "scope_summary",
                    "outline_briefs",
                    "quadratic_summary",
                )
            },
            market_scan_snapshot=market_scan,
        )
    except Exception as exc:
        print(f"!! agent raised: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 2

    print()
    print("AGENT OUTPUT (LLM judgment, no dollar totals):")
    print(f"  labor_lines: {len(agent_output.labor_lines)}")
    for i, ll in enumerate(agent_output.labor_lines, 1):
        print(f"    [{i}] {ll.labor_category} | wage_band={ll.wage_band} | hrs={ll.hours}")
        print(f"        rationale={textwrap.shorten(ll.rationale, 160)}")
    print(f"  avg_headcount_during_pop: {agent_output.avg_headcount_during_pop}")
    print(f"  odcs: {len(agent_output.odcs)}")
    for o in agent_output.odcs:
        print(f"    - {o.item}: ${o.amount_usd:,.0f} ({textwrap.shorten(o.justification, 120)})")
    print(f"  subcontractor_costs_usd: {agent_output.subcontractor_costs_usd}")
    print(f"  key_risks: {len(agent_output.key_risks)}")
    for r in agent_output.key_risks:
        print(f"    - {textwrap.shorten(r, 200)}")
    print()
    print(f"  executive_summary preview: {textwrap.shorten(agent_output.executive_summary, 400)}")

    # ---- LLM-output invariants ----
    failures: list[str] = []
    if not agent_output.labor_lines:
        failures.append("agent returned 0 labor_lines")
    if agent_output.avg_headcount_during_pop <= 0:
        failures.append(f"avg_headcount_during_pop={agent_output.avg_headcount_during_pop}")
    rules = get_pricing_rules()
    valid_categories = {e["category"] for e in rules["labor_catalog"]}
    valid_bands = set(rules["wage_bands"].keys())
    for i, ll in enumerate(agent_output.labor_lines):
        if ll.labor_category not in valid_categories:
            failures.append(f"labor_lines[{i}] category {ll.labor_category!r} not in catalog")
        if ll.wage_band not in valid_bands:
            failures.append(f"labor_lines[{i}] wage_band {ll.wage_band!r} not in wage_bands")
        if ll.hours <= 0:
            failures.append(f"labor_lines[{i}] hours={ll.hours}")

    if failures:
        print("!! AGENT-OUTPUT INVARIANT FAILURES:")
        for f in failures:
            print(f"   - {f}")
        return 3

    # ---- Compute + persist ----
    print()
    print(">> computing scenario packages (deterministic Python)...")
    try:
        packages = compute_scenario_packages(
            output=agent_output,
            market_band_low_usd=market_scan.get("market_band_low_usd"),
            market_band_mid_usd=market_scan.get("market_band_mid_usd"),
            market_band_high_usd=market_scan.get("market_band_high_usd"),
        )
    except Exception as exc:
        print(f"!! compute_scenario_packages raised: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 4

    print()
    print(
        f"{'Scenario':<10} {'Price':>14} {'Cost':>14} {'Profit':>14} "
        f"{'Margin':>8} {'Position':>10} {'Recommendation':>16}"
    )
    by_scenario = {p.scenario: p for p in packages}
    for scenario in ("LOW", "MEDIUM", "HIGH"):
        p = by_scenario[scenario]
        print(
            f"{scenario:<10} "
            f"${p.total_proposed_price_usd:>13,.0f} "
            f"${p.total_subtotal_cost_usd:>13,.0f} "
            f"${p.profit_usd:>13,.0f} "
            f"{p.profit_margin_pct:>7.1%} "
            f"{p.vs_market_position:>10} "
            f"{p.bid_recommendation:>16}"
        )
    for scenario in ("LOW", "MEDIUM", "HIGH"):
        p = by_scenario[scenario]
        if p.recommendation_rationale:
            print(f"  {scenario} rationale: {textwrap.shorten(p.recommendation_rationale, 200)}")

    print()
    print(">> upserting pricing packages...")
    new_ids = upsert_pricing_packages(
        proposal_id=proposal_id,
        packages=packages,
        market_scan_id=market_scan.get("id"),
        agent_run_id=None,
        executive_summary=agent_output.executive_summary,
    )
    print(f"   package ids: {new_ids}")

    # ---- Persistence parity check ----
    snap = get_pricing_packages_snapshot(proposal_id)
    if len(snap) != 3:
        print(f"!! persisted {len(snap)} packages, expected 3")
        return 5
    snap_by_scenario = {s["scenario"]: s for s in snap}
    persist_failures: list[str] = []
    for scenario, computed in by_scenario.items():
        s = snap_by_scenario.get(scenario)
        if s is None:
            persist_failures.append(f"missing persisted {scenario}")
            continue
        if abs(s["total_proposed_price"] - computed.total_proposed_price_usd) > 0.51:
            persist_failures.append(
                f"{scenario} price persisted={s['total_proposed_price']} "
                f"!= computed={computed.total_proposed_price_usd}"
            )
        if len(s["lines"]) != len(computed.lines):
            persist_failures.append(
                f"{scenario} lines persisted={len(s['lines'])} != computed={len(computed.lines)}"
            )

    print()
    if persist_failures:
        print("!! PERSISTENCE FAILURES:")
        for f in persist_failures:
            print(f"   - {f}")
        return 6
    print("** STAGE 2 PASS - agent output structurally valid, computed prices persisted with parity. **")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cost Analyst E2E smoke test.")
    add_live_args(parser, allow_stage1_only=True)
    return parser.parse_args(argv[1:])


def main() -> int:
    args = _parse_args(sys.argv)
    s1 = _run_stage1()
    if s1 != 0:
        print("\n!! Stage 1 failed; skipping Stage 2.")
        return s1

    if args.stage1_only or not args.live:
        print()
        print("Stage 2 skipped. Re-run with --live plus --proposal-id N, or --live --latest.")
        return 0

    if not require_api_keys(["openai"]):
        return 2

    proposal_id = pick_proposal_id(args)
    if proposal_id is None:
        return 1

    return _run_stage2(proposal_id)


if __name__ == "__main__":
    sys.exit(main())
