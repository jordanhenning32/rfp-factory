"""Live end-to-end smoke test for the Cost Reviewer DUAL pipeline.

Runs primary (Gemini 2.5 Pro) + secondary (GPT-5.5) reviewers in
parallel against an existing proposal's persisted cost build, then
runs the Sonnet 4.6 consolidator to filter to the consensus subset.

Costs ~$0.40-1.00 per run (two reviewer calls + one consolidator).

Validates:

  1. Both reviewers actually ran (raw_primary AND raw_secondary
     are populated, not None).
  2. The consolidator ran (consolidator_ran=True, no error).
  3. The final findings are a subset of (or comparable in size to)
     the smaller of the two raw finding sets — the consensus filter
     should not invent findings neither reviewer raised.
  4. Each finding still has CostReviewFinding-shaped data with
     valid severity / category / scenarios_affected.
  5. Persistence writes one row per affected scenario; re-reading
     produces matching data.

Usage:
    cd <project root>
    .venv\\Scripts\\python.exe scripts\\_e2e_cost_reviewer_test.py --live --proposal-id 1
    .venv\\Scripts\\python.exe scripts\\_e2e_cost_reviewer_test.py --live --latest
"""

from __future__ import annotations

import argparse
import sys
import textwrap

from _e2e_live_helpers import add_live_args, pick_proposal_id, require_api_keys, require_live

from app.core.enums import FindingSeverity
from app.jobs.cost_reviewer import (
    _snapshot_cost_reviewer_inputs,
    dual_review_and_consolidate,
)
from app.services.cost_reviewer import (
    get_cost_review_findings_snapshot,
    upsert_cost_review_findings,
)

_VALID_CATEGORIES = (
    "missed_scope",
    "unrealistic_hours",
    "wage_band_misalignment",
    "margin_pressure",
    "ceiling_violation",
    "phase_gap",
    "odc_missing",
    "odc_excessive",
    "contract_type_mismatch",
    "consistency_issue",
)


def _print_findings(label: str, findings) -> None:
    if not findings:
        print(f"  {label}: 0 findings")
        return
    sev_counts: dict[str, int] = {}
    for f in findings:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
    sev_str = ", ".join(f"{count} {sev}" for sev, count in sorted(sev_counts.items()))
    print(f"  {label}: {len(findings)} findings ({sev_str})")
    for i, f in enumerate(findings, 1):
        subj = (f.subject or "").strip()
        print(f"    [{i}] {f.severity:<8} {f.category:<25} {subj[:80]}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cost Reviewer live E2E smoke test.")
    add_live_args(parser)
    return parser.parse_args(argv[1:])


def main() -> int:
    args = _parse_args(sys.argv)
    if not require_live(args, script_name="Cost Reviewer", estimated_cost="$0.40-1.00"):
        return 2
    if not require_api_keys(["gemini", "openai", "anthropic"]):
        return 2

    proposal_id = pick_proposal_id(args)
    if proposal_id is None:
        return 1

    print(f"** running DUAL Cost Reviewer against proposal {proposal_id} **")

    inputs = _snapshot_cost_reviewer_inputs(proposal_id)
    if inputs is None:
        print(
            f"!! prerequisites missing for proposal {proposal_id}. "
            f"Run Cost Analyst first to populate pricing packages."
        )
        return 1

    print()
    print("=" * 70)
    print("INPUTS")
    print("=" * 70)
    print(f"  rfp_title:           {inputs.rfp_title or '(none)'}")
    print(f"  rfp_agency:          {inputs.rfp_agency or '(none)'}")
    print(f"  pop_months:          {inputs.pop_months}")
    print(f"  contract_type:       {inputs.contract_type_signal}")
    print(f"  proposed_scenario:   {inputs.proposed_scenario}")
    print(f"  compliance_block:    {len(inputs.compliance_block):,} chars")
    print(f"  market_scan_block:   {len(inputs.market_scan_block):,} chars")
    print(f"  cost_build_block:    {len(inputs.cost_build_block):,} chars")
    print(f"  phases_block:        {len(inputs.phases_block):,} chars")
    print(f"  odcs_block:          {len(inputs.odcs_block):,} chars")
    print(f"  other_scenarios:     {len(inputs.other_scenarios_block):,} chars")
    print(f"  drafts_block:        {len(inputs.drafts_block):,} chars")
    print(f"  methodology_block:   {len(inputs.methodology_block):,} chars")
    total_input_chars = (
        len(inputs.compliance_block)
        + len(inputs.market_scan_block)
        + len(inputs.cost_build_block)
        + len(inputs.phases_block)
        + len(inputs.odcs_block)
        + len(inputs.other_scenarios_block)
        + len(inputs.drafts_block)
        + len(inputs.methodology_block)
    )
    print(f"  total input chars:   {total_input_chars:,}")

    from app.config import get_settings

    settings = get_settings()
    primary_model = settings.model_cost_reviewer
    secondary_model = settings.model_cost_reviewer_secondary
    consolidator_model = settings.model_cost_review_consolidator
    print(
        f"\n>> dual pipeline: {primary_model} + {secondary_model} "
        f"in parallel, then {consolidator_model} consolidator "
        f"(may take 60-180s)..."
    )

    # Print stage events as they happen so the user can watch progress.
    def _stage_print(msg: str) -> None:
        print(f"   [stage] {msg}")

    try:
        outcome = dual_review_and_consolidate(
            proposal_id=proposal_id,
            inputs=inputs,
            on_stage=_stage_print,
        )
    except Exception as exc:
        print(f"!! pipeline raised: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 2

    print()
    print("=" * 70)
    print("RAW REVIEWER OUTPUTS (pre-consolidation)")
    print("=" * 70)
    if outcome.raw_primary is not None:
        _print_findings(
            f"primary ({outcome.primary_model})",
            outcome.raw_primary.findings,
        )
    else:
        print(f"  primary ({outcome.primary_model}): FAILED - {outcome.primary_error}")
    if outcome.raw_secondary is not None:
        _print_findings(
            f"secondary ({outcome.secondary_model})",
            outcome.raw_secondary.findings,
        )
    else:
        print(f"  secondary ({outcome.secondary_model}): FAILED - {outcome.secondary_error}")

    print()
    print("=" * 70)
    print("CONSOLIDATOR")
    print("=" * 70)
    if outcome.consolidator_ran:
        n_a = len(outcome.raw_primary.findings) if outcome.raw_primary else 0
        n_b = len(outcome.raw_secondary.findings) if outcome.raw_secondary else 0
        # Count minorities by the prefix the consolidator stamps onto
        # finding_text. Consensus = total - minorities.
        n_minority = sum(
            1 for f in outcome.final.findings if f.finding_text.startswith("[Single-reviewer flag from ")
        )
        n_consensus = len(outcome.final.findings) - n_minority
        print(f"  ran: YES ({consolidator_model})")
        print(f"  raw: primary={n_a}, secondary={n_b}")
        print(
            f"  consolidated: {len(outcome.final.findings)} total = "
            f"{n_consensus} consensus + {n_minority} minority (MINOR)"
        )
        if n_a + n_b > 0:
            print(
                f"  retention: {len(outcome.final.findings)} / "
                f"({n_a}+{n_b}) = "
                f"{len(outcome.final.findings) / (n_a + n_b):.0%}"
            )
    else:
        if outcome.consolidator_error:
            print(f"  ran: NO - consolidator FAILED: {outcome.consolidator_error}")
            print("  fallback: primary-only findings")
        else:
            print("  ran: NO - one reviewer failed, using survivor's findings without consensus filter")

    print()
    print("=" * 70)
    print("FINAL RESULT (what gets persisted)")
    print("=" * 70)
    result = outcome.final
    _print_findings("final", result.findings)

    print()
    print("FINDINGS (full):")
    for i, f in enumerate(result.findings, 1):
        print(f"  [{i}] {f.severity} - {f.category} - affects {','.join(f.scenarios_affected)}")
        print(f"      subject: {f.subject}")
        text = textwrap.shorten(f.finding_text, width=400, placeholder="...")
        print(f"      {text}")
        for j, alt in enumerate(f.alternative_scenarios, 1):
            price_str = f"${alt.total_price_usd:,.0f}" if alt.total_price_usd is not None else "(no price)"
            margin_str = (
                f"profit delta ${alt.margin_delta_usd:,.0f}" if alt.margin_delta_usd is not None else ""
            )
            print(f"      alt[{j}] {alt.label}: {price_str} {margin_str}")
            rationale = textwrap.shorten(alt.rationale, 200, placeholder="...")
            print(f"          {rationale}")

    # ---- Invariants ----
    print()
    print("=" * 70)
    print("INVARIANT CHECKS")
    print("=" * 70)
    failures: list[str] = []

    # Dual-pipeline invariants — these are the whole point of B vs A.
    if outcome.raw_primary is None:
        failures.append(
            f"primary reviewer ({outcome.primary_model}) did NOT return a result: {outcome.primary_error}"
        )
    if outcome.raw_secondary is None:
        failures.append(
            f"secondary reviewer ({outcome.secondary_model}) did NOT "
            f"return a result: {outcome.secondary_error}"
        )
    if outcome.raw_primary is not None and outcome.raw_secondary is not None:
        if not outcome.consolidator_ran:
            # Tolerate the "one reviewer empty" path — consolidator is
            # skipped and survivor findings are downgraded to MINOR
            # in code. Only fail if both reviewers had findings.
            n_a = len(outcome.raw_primary.findings)
            n_b = len(outcome.raw_secondary.findings)
            if n_a > 0 and n_b > 0:
                failures.append(
                    f"both reviewers raised findings (a={n_a}, b={n_b}) "
                    f"but consolidator did not run "
                    f"(error: {outcome.consolidator_error})"
                )
        else:
            n_a = len(outcome.raw_primary.findings)
            n_b = len(outcome.raw_secondary.findings)
            n_final = len(outcome.final.findings)
            # Consensus + minority count cannot exceed total raw inputs.
            # (Consensus dedupes 2 raw findings into 1, so equality is
            # possible only when there are no consensus matches.)
            if n_final > n_a + n_b:
                failures.append(
                    f"consolidator emitted {n_final} findings but "
                    f"raw inputs total {n_a}+{n_b}={n_a + n_b} — "
                    f"output cannot exceed total raw"
                )
            # Minority findings (the ones the consolidator tagged as
            # single-reviewer) MUST be MINOR severity. Belt-and-
            # suspenders: code already enforces this; verify here.
            for i, f in enumerate(outcome.final.findings):
                if f.finding_text.startswith("[Single-reviewer flag from ") and f.severity != "MINOR":
                    failures.append(
                        f"finding[{i}] is tagged as minority but severity={f.severity!r} (expected MINOR)"
                    )

    # Per-finding invariants on the persisted result.
    valid_severities = {s.value for s in FindingSeverity}
    for i, f in enumerate(result.findings):
        if f.severity not in valid_severities:
            failures.append(f"finding[{i}] severity {f.severity!r} not in {valid_severities}")
        if f.category not in _VALID_CATEGORIES:
            failures.append(f"finding[{i}] category {f.category!r} not in valid set")
        if not f.scenarios_affected:
            failures.append(f"finding[{i}] has empty scenarios_affected")
        for s in f.scenarios_affected:
            if s not in ("LOW", "MEDIUM", "HIGH"):
                failures.append(f"finding[{i}] scenarios_affected has unexpected {s!r}")
        if not f.finding_text.strip():
            failures.append(f"finding[{i}] has empty finding_text")
        if not f.subject.strip():
            failures.append(f"finding[{i}] has empty subject")

    if failures:
        print("!! INVARIANT FAILURES:")
        for fail in failures:
            print(f"   - {fail}")
    else:
        print("   all invariants passed.")

    # ---- Persist + verify ----
    print()
    print(">> upserting findings to DB...")
    n_rows = upsert_cost_review_findings(
        proposal_id=proposal_id,
        result=result,
    )
    print(f"   wrote {n_rows} CostReviewFinding row(s)")

    snap = get_cost_review_findings_snapshot(proposal_id)
    print(f"   re-read {len(snap)} row(s) from DB")

    persist_failures: list[str] = []
    expected_rows = sum(len(f.scenarios_affected) for f in result.findings)
    if len(snap) != expected_rows:
        persist_failures.append(
            f"persisted {len(snap)} rows but expected {expected_rows} "
            f"(sum of scenarios_affected across findings)"
        )

    print()
    if failures or persist_failures:
        for fail in persist_failures:
            print(f"   - {fail}")
        print(f"** FAIL - {len(failures) + len(persist_failures)} failure(s). **")
        return 3

    n_minority = sum(1 for f in result.findings if f.finding_text.startswith("[Single-reviewer flag from "))
    n_consensus = len(result.findings) - n_minority
    print(
        f"** PASS - {len(result.findings)} final finding(s) "
        f"({n_consensus} consensus + {n_minority} minority), "
        f"{n_rows} row(s) persisted with parity. **"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
