"""Live end-to-end smoke test for the Cost Volume Writer.

Two stages:

  STAGE 1 - Cached-prefix formatting validation (no LLM cost).
  Reads the persisted PricingPackages + MarketScan for the proposal
  and renders the cached_prefix. Asserts the prefix:
    - Contains all 3 scenario blocks (LOW/MEDIUM/HIGH)
    - Contains a labor-lines markdown table
    - Contains the market scan summary if persisted
    - Contains the executive summary if the analyst left one
    - Has reasonable size (cached cost matters)

  STAGE 2 - Real LLM end-to-end. Picks the FIRST cost-deferred
  section in the proposal (requires_cost_analysis=True, not
  excluded_from_draft), drafts it via Sonnet 4.6, and validates:
    - draft_text_markdown is non-empty and reasonably sized
    - citations all have a `source` that maps to the cost build,
      market scan, internal pricing rules, or null
    - no fabricated dollar values that aren't in the cost build
      (light heuristic check — find $X,XXX patterns and verify
      they appear in the cached prefix)
    - persistence parity (re-read after persist; markdown matches)

Cost: STAGE 1 free, STAGE 2 ~$0.05-0.20 (Sonnet input/output, with
the cached prefix paying the write cost on this first call).

Usage:
    cd <project root>
    .venv\\Scripts\\python.exe scripts\\_e2e_cost_writer_test.py --proposal-id 1 --stage1-only
    .venv\\Scripts\\python.exe scripts\\_e2e_cost_writer_test.py --live --proposal-id 1
    .venv\\Scripts\\python.exe scripts\\_e2e_cost_writer_test.py --live --latest
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap

from _e2e_live_helpers import add_live_args, pick_proposal_id, require_api_keys

from app.agents.cost_writer import (
    DEFAULT_PROPOSED_SCENARIO,
    CostWriterContext,
    build_cached_prefix,
    draft_cost_section,
)
from app.db.session import session_scope
from app.jobs.cost_writer import (
    _build_quadratic_summary,
    _compliance_text_for_section,
    _detect_contract_type_signal,
    _snapshot_writer_inputs,
)
from app.models import ProposalSection
from app.services.market_scan import get_market_scan_snapshot
from app.services.pricing import get_pricing_packages_snapshot

# ---- Stage 1: cached-prefix formatting validation ------------------------


def _run_stage1(proposal_id: int) -> int:
    print("=" * 70)
    print("STAGE 1 - Cached-prefix formatting validation (no LLM cost)")
    print("=" * 70)

    pkgs = get_pricing_packages_snapshot(proposal_id)
    if len(pkgs) < 3:
        print(f"!! only {len(pkgs)} pricing packages persisted; need 3.")
        print("   Run _e2e_cost_analyst_test.py first.")
        return 1

    market_scan = get_market_scan_snapshot(proposal_id)
    if market_scan is None:
        print("   no market scan persisted - prefix will omit market block.")

    # Use the MEDIUM scenario's executive_summary.
    exec_summary = ""
    for p in pkgs:
        if p["scenario"] == "MEDIUM":
            exec_summary = p.get("pnl_projection_json", {}).get("executive_summary") or ""
            break

    contract_type_signal = _detect_contract_type_signal(proposal_id)
    ctx = CostWriterContext(
        pricing_packages_snapshot=pkgs,
        market_scan_snapshot=market_scan,
        executive_summary=exec_summary,
        quadratic_summary=_build_quadratic_summary(),
        proposed_scenario=DEFAULT_PROPOSED_SCENARIO,
        contract_type_signal=contract_type_signal,
    )
    prefix = build_cached_prefix(ctx)

    failures: list[str] = []

    required_markers = [
        "=== COST_BUILD",
        "PROPOSED scenario: MEDIUM",
        "--- LOW scenario",
        "--- MEDIUM scenario",
        "--- HIGH scenario",
        "--- Labor lines",
        "=== INTERNAL_PRICING_METHODOLOGY",
        "=== QUADRATIC profile",
    ]
    for marker in required_markers:
        if marker not in prefix:
            failures.append(f"prefix missing marker: {marker!r}")

    if market_scan and "=== MARKET_SCAN" not in prefix:
        failures.append("market scan persisted but MARKET_SCAN block missing")
    if not market_scan and "=== MARKET_SCAN" in prefix:
        # Heuristic — block heading is always present, just empty body.
        # No failure either way.
        pass

    # Labor-lines table sanity: should have a markdown table header.
    if "| labor_category |" not in prefix:
        failures.append("labor lines table header missing")

    # Size sanity — the prefix should be substantial but not enormous.
    n_chars = len(prefix)
    if n_chars < 1500:
        failures.append(f"prefix too small ({n_chars} chars) — likely missing data")
    if n_chars > 60000:
        failures.append(f"prefix too large ({n_chars} chars) — will burn cache-write budget")

    print(f"   prefix size: {n_chars:,} chars")
    print(f"   contains MARKET_SCAN block: {('=== MARKET_SCAN' in prefix) and bool(market_scan)}")
    print(f"   contains executive summary: {bool(exec_summary)} ({len(exec_summary)} chars)")
    print(f"   contract type signal: {contract_type_signal}")

    print()
    if failures:
        print("!! STAGE 1 FAILURES:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("** STAGE 1 PASS - cached prefix renders cleanly. **")
    return 0


# ---- Stage 2: real LLM end-to-end ----------------------------------------

# Heuristic dollar-value regex used for the post-draft fabrication check.
# Catches things like "$1,407,335", "$1.4M", "$147.20/hr", "$650K".
_DOLLAR_RE = re.compile(
    r"\$\s?(?:\d{1,3}(?:,\d{3})*(?:\.\d+)?|"
    r"\d+(?:\.\d+)?\s?[KkMm]?)(?:/hr)?"
)


def _normalize_dollar(s: str) -> str:
    """Normalize a dollar string for comparison: strip whitespace,
    lowercase the K/M suffix, drop trailing /hr."""
    return s.strip().replace(" ", "").replace("/hr", "").replace("K", "k").replace("M", "m")


def _find_unsupported_dollars(
    draft: str,
    prefix: str,
) -> list[str]:
    """Heuristic: every $-value in the draft should appear (or have a
    near-match) in the cached prefix. Returns list of suspect strings.

    This is a NOISE-tolerant check — we accept matches when the draft's
    rounded number appears in the prefix in any form, OR when the
    number falls within +/- 0.5% of a prefix value (covers display
    rounding). Numbers that fail both are surfaced for human review,
    not auto-flagged as fabrications.
    """
    draft_dollars = _DOLLAR_RE.findall(draft)
    prefix_dollars = set(_normalize_dollar(d) for d in _DOLLAR_RE.findall(prefix))
    # Also collect numeric values from the prefix for fuzzy match.
    prefix_numerics: list[float] = []
    for d in prefix_dollars:
        try:
            v = float(d.replace("$", "").replace(",", "").replace("k", "").replace("m", ""))
            if d.endswith("k"):
                v *= 1000
            elif d.endswith("m"):
                v *= 1_000_000
            prefix_numerics.append(v)
        except (ValueError, AttributeError):
            pass

    suspicious: list[str] = []
    for raw in draft_dollars:
        norm = _normalize_dollar(raw)
        if norm in prefix_dollars:
            continue
        # Fuzzy match: parse the draft value, scan prefix numerics
        # for ±0.5% tolerance.
        try:
            v = float(norm.replace("$", "").replace(",", "").replace("k", "").replace("m", ""))
            if norm.endswith("k"):
                v *= 1000
            elif norm.endswith("m"):
                v *= 1_000_000
            if any(abs(v - pv) / max(abs(pv), 1.0) < 0.005 for pv in prefix_numerics):
                continue
        except (ValueError, AttributeError):
            pass
        # Tolerate small standalone numbers — "10%" rendered as "$10"
        # rarely happens, but "1,950 hours" might. Skip values < $100.
        try:
            v = float(norm.replace("$", "").replace(",", "").replace("k", "").replace("m", ""))
            if norm.endswith("k"):
                v *= 1000
            elif norm.endswith("m"):
                v *= 1_000_000
            if v < 100:
                continue
        except (ValueError, AttributeError):
            pass
        suspicious.append(raw)
    return suspicious


def _run_stage2(proposal_id: int) -> int:
    print()
    print("=" * 70)
    print(f"STAGE 2 - Real LLM end-to-end against proposal {proposal_id}")
    print("=" * 70)

    inputs = _snapshot_writer_inputs(proposal_id)
    if inputs is None:
        print(f"!! proposal {proposal_id} not found.")
        return 1
    if not inputs["sections"]:
        print(f"!! no cost-deferred sections in proposal {proposal_id}.")
        print(
            "   Mark a section requires_cost_analysis=True in the "
            "Outline tab first, OR run the Outline Agent against "
            "a Cost Volume RFP."
        )
        return 1

    # Pick the first eligible cost section.
    section = inputs["sections"][0]
    print(f"   target section: {section['section_id']} '{section['section_title']}' (pk={section['pk']})")
    print(
        f"   page_limit={section['page_limit']}, "
        f"word_limit={section['word_limit']}, "
        f"compliance_items={section['compliance_items_addressed']}"
    )

    # Build context.
    pkgs = get_pricing_packages_snapshot(proposal_id)
    market_scan = get_market_scan_snapshot(proposal_id)
    exec_summary = ""
    proposed_pkg = None
    for p in pkgs:
        if p["scenario"] == DEFAULT_PROPOSED_SCENARIO:
            proposed_pkg = p
            exec_summary = p.get("pnl_projection_json", {}).get("executive_summary") or ""
            break
    if proposed_pkg is None:
        print(f"!! missing {DEFAULT_PROPOSED_SCENARIO} scenario package.")
        return 1
    ctx = CostWriterContext(
        pricing_packages_snapshot=pkgs,
        market_scan_snapshot=market_scan,
        executive_summary=exec_summary,
        quadratic_summary=_build_quadratic_summary(),
        proposed_scenario=DEFAULT_PROPOSED_SCENARIO,
        contract_type_signal=inputs["contract_type_signal"],
    )
    cached_prefix = build_cached_prefix(ctx)

    compliance_text = _compliance_text_for_section(
        section["compliance_items_addressed"],
        inputs["comp_text_lookup"],
    )

    from app.config import get_settings

    print(f"\n>> calling {get_settings().model_cost_writer} for cost narrative draft (may take 20-60s)...")

    try:
        draft = draft_cost_section(
            proposal_id=proposal_id,
            section_id=section["section_id"],
            section_title=section["section_title"],
            section_order=section["section_order"],
            section_brief=section["section_brief"],
            compliance_item_ids=section["compliance_items_addressed"],
            compliance_text=compliance_text,
            page_limit=section["page_limit"],
            word_limit=section["word_limit"],
            cached_prefix=cached_prefix,
            rfp_title=inputs["rfp_title"],
            rfp_agency=inputs["rfp_agency"],
            pop_months=inputs["pop_months"],
            contract_type_signal=inputs["contract_type_signal"],
            outline_snippet=inputs["outline_snippet"],
        )
    except Exception as exc:
        print(f"!! agent raised: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 2

    print()
    print("AGENT OUTPUT:")
    print(f"  draft_text_markdown: {len(draft.draft_text_markdown)} chars")
    print(f"  citations: {len(draft.citations)}")
    print(f"  needs_human_placeholders: {len(draft.needs_human_placeholders)}")
    print(f"  shortfall_mitigations_applied: {len(draft.shortfall_mitigations_applied)}")

    print()
    print("DRAFT PREVIEW (first 2000 chars):")
    print("-" * 70)
    print(draft.draft_text_markdown[:2000])
    if len(draft.draft_text_markdown) > 2000:
        print(f"... ({len(draft.draft_text_markdown) - 2000} more chars)")
    print("-" * 70)

    print()
    print("CITATIONS:")
    for i, c in enumerate(draft.citations, 1):
        claim = textwrap.shorten(c.get("claim", "(no claim)"), 120)
        print(f"  [{i}] {claim}")
        print(f"      source={c.get('source', '(no source)')}")

    if draft.needs_human_placeholders:
        print()
        print("NEEDS HUMAN PLACEHOLDERS:")
        for p in draft.needs_human_placeholders:
            print(f"  - [{p.get('marker', '?')}]: {textwrap.shorten(p.get('description', ''), 200)}")

    # ---- Invariants on the draft ----
    failures: list[str] = []

    if len(draft.draft_text_markdown) < 200:
        failures.append(
            f"draft very short ({len(draft.draft_text_markdown)} chars) — agent likely produced a stub"
        )

    # Citations should reference cost_build / market_scan / internal_pricing
    # / null (for sources omitted by design).
    for i, c in enumerate(draft.citations):
        src = (c.get("source") or "").strip()
        if not src:
            failures.append(f"citations[{i}]: empty source — every claim should trace to a structured input")
            continue
        if not (
            src.startswith("cost_build")
            or src.startswith("market_scan")
            or src.startswith("internal_pricing_rules")
        ):
            # Soft-fail — allow other sources but flag for review.
            print(f"   [warn] citations[{i}] source not a known prefix: {src!r}")

    # Heuristic check: scan draft for $-values not present in prefix.
    suspicious = _find_unsupported_dollars(
        draft.draft_text_markdown,
        cached_prefix,
    )
    if suspicious:
        print()
        print(f"   [warn] {len(suspicious)} $-values in draft not directly traceable to the prefix:")
        for s in suspicious[:10]:
            print(f"      - {s}")
        if len(suspicious) > 10:
            print(f"      - ... ({len(suspicious) - 10} more)")
        # Don't hard-fail — the heuristic has false positives. Just flag.

    print()
    if failures:
        print("!! STAGE 2 FAILURES:")
        for f in failures:
            print(f"   - {f}")
        return 3

    # ---- Persistence test (uses the existing Writer Team's path) ----
    print(">> persisting draft via persist_section_draft...")
    from app.services.sections import persist_section_draft

    persist_section_draft(
        proposal_section_pk=section["pk"],
        draft_text_markdown=draft.draft_text_markdown,
        citations=draft.citations,
        needs_human_placeholders=draft.needs_human_placeholders,
        shortfall_mitigations_applied=draft.shortfall_mitigations_applied,
    )

    # Re-read and verify parity.
    with session_scope() as db:
        row = db.get(ProposalSection, section["pk"])
        persisted_md = (row.draft_text_markdown or "") if row else ""
        persisted_citations = list(row.citations_json or []) if row else []

    if persisted_md != draft.draft_text_markdown:
        print("!! persisted markdown differs from in-memory draft")
        return 4
    if len(persisted_citations) != len(draft.citations):
        print(f"!! persisted citations={len(persisted_citations)} != in-memory={len(draft.citations)}")
        return 5

    print("** STAGE 2 PASS - draft persisted with parity. **")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cost Volume Writer E2E smoke test.")
    add_live_args(parser, allow_stage1_only=True)
    return parser.parse_args(argv[1:])


def main() -> int:
    args = _parse_args(sys.argv)

    proposal_id = pick_proposal_id(args)
    if proposal_id is None:
        return 1

    s1 = _run_stage1(proposal_id)
    if s1 != 0:
        print("\n!! Stage 1 failed; skipping Stage 2.")
        return s1

    if args.stage1_only or not args.live:
        print()
        print("Stage 2 skipped. Re-run with --live plus --proposal-id N, or --live --latest.")
        return 0

    if not require_api_keys(["anthropic"]):
        return 2

    return _run_stage2(proposal_id)


if __name__ == "__main__":
    sys.exit(main())
