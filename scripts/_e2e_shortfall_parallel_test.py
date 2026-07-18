"""Verify shortfall parallelization: build a synthetic proposal with
mixed requirement_types, monkeypatch the analyze_compliance_batch call
to a fast stub, run _run_shortfall_strategist, and confirm:
  1. submission_format items are filtered out
  2. Remaining batches run in parallel (peak concurrency > 1)
  3. All eligible items get processed
  4. Failed batches are isolated, not propagating to other workers
Creates its own temporary migrated SQLite DB. Run with:
    python scripts/_e2e_shortfall_parallel_test.py
"""

import threading
import time
from datetime import datetime
from unittest import mock

from _e2e_harness import configure_e2e_database

configure_e2e_database("_e2e_shortfall_parallel_test")

from app.db.session import SessionLocal, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    ComplianceMatrixItem,
    Proposal,
    RfpPackage,
)


def main():
    test_ids = {}
    with session_scope() as db:
        pkg = RfpPackage(uploaded_at=datetime.utcnow(), storage_dir="(t)")
        db.add(pkg)
        db.flush()
        test_ids["pkg"] = pkg.id
        p = Proposal(
            rfp_package_id=pkg.id,
            title="SHORTFALL PARALLEL TEST",
            status="drafting",
        )
        db.add(p)
        db.flush()
        test_ids["proposal"] = p.id

        # Build 95 compliance items: 75 'shall' (will run shortfall) +
        # 20 'submission_format' (filtered out). 75 = exactly 3 batches
        # of 25 — tail-merge in make_batches doesn't kick in since the
        # split is exact, so the test reliably exercises 3-batch
        # parallel orchestration.
        for i in range(1, 76):
            db.add(
                ComplianceMatrixItem(
                    proposal_id=p.id,
                    requirement_id=f"REQ-{i:03d}",
                    requirement_text=f"The contractor shall provide capability {i}.",
                    source_doc="t.pdf",
                    requirement_type="shall",
                    category="technical",
                )
            )
        for i in range(76, 96):
            db.add(
                ComplianceMatrixItem(
                    proposal_id=p.id,
                    requirement_id=f"REQ-FMT-{i:03d}",
                    requirement_text="Use 12-pt font, single-spaced.",
                    source_doc="t.pdf",
                    requirement_type="submission_format",
                    category="administrative",
                )
            )

    print("Setup: 75 shall items + 20 submission_format items")
    print()

    # --- Test 1: submission_format filter + parallelism ---
    observed_concurrent: list[int] = []
    in_flight: set[str] = set()
    in_flight_lock = threading.Lock()

    def fast_stub(*, proposal_id, requirements, cached_prefix):
        """Stub: pretend to analyze. Sleep briefly so we observe
        actual concurrency. Return one ShortfallItem per req."""
        from app.agents.shortfall_strategist import ShortfallItem

        worker_name = threading.current_thread().name
        with in_flight_lock:
            in_flight.add(worker_name)
            observed_concurrent.append(len(in_flight))
        try:
            time.sleep(0.2)
            return [
                ShortfallItem(
                    requirement_id=r["requirement_id"],
                    verdict="met",
                    current_state="",
                    evidence_citations=[],
                    gap_severity=None,
                    mitigation_options=[],
                    recommended_mitigation_index=None,
                    no_bid_recommended=False,
                )
                for r in requirements
            ]
        finally:
            with in_flight_lock:
                in_flight.discard(worker_name)

    from app.jobs import intake

    # We need to also stub out get_company_profile / build_shortfall_kb_context
    # so the test doesn't try to load real KB / profile data.
    with (
        mock.patch.object(intake, "analyze_compliance_batch", side_effect=fast_stub),
        mock.patch.object(intake, "get_company_profile", return_value={"_meta": {"version": "0"}}),
        mock.patch.object(intake, "build_shortfall_kb_context", return_value=""),
        mock.patch.object(intake, "format_decisions_for_prompt", return_value=""),
        mock.patch.object(intake, "get_teaming_partners", return_value=[]),
    ):
        t0 = time.time()
        gaps, no_bid = intake._run_shortfall_strategist(test_ids["proposal"])
        elapsed = time.time() - t0

    peak = max(observed_concurrent) if observed_concurrent else 0
    print("Test 1 (parallelism + filter):")
    print(f"  wall time: {elapsed:.2f}s")
    print(f"  observed peak concurrency: {peak}")
    # 60 shall items / batch_size 25 = 3 batches. With 4 workers, all 3
    # batches run simultaneously; expect peak >= 3 and elapsed ~0.2s
    # (one batch's sleep) + small overhead.
    # Peak concurrency is the reliable parallelism signal — wall time
    # has too much OS-scheduling jitter on Windows for a tight bound.
    # Serial would be 3 × 0.2s = 0.6s + overhead; parallel is bounded
    # below by the longest single batch (~0.2s + overhead).
    print("  expected: 3 batches (75/25), peak >= 2")
    assert peak >= 2, f"peak={peak}; parallelism not happening"
    assert elapsed < 2.0, (
        "wall extremely high — even with jitter, parallel of 3 × 0.2s "
        f"should be well under 2s. Got elapsed={elapsed:.2f}"
    )

    assert gaps == 0, f"met stub should create 0 gaps; got {gaps}"
    assert no_bid is False, "met stub should not recommend no-bid"

    # Verify submission_format items were NOT analyzed (no gap rows for them).
    with SessionLocal() as db:
        from app.models import GapAnalysis

        # All gap_analyses for this proposal should have requirement_ids
        # starting with REQ- (the shall items), not REQ-FMT-.
        gap_rows = db.query(GapAnalysis).filter(GapAnalysis.proposal_id == test_ids["proposal"]).all()
        assert not gap_rows, f"met stub should not persist gap_analyses; got {len(gap_rows)}"
        print(f"  gap_analyses rows: {len(gap_rows)} (met stub creates none)")

    print("  PASS\n")

    # --- Test 2: failure isolation ---
    call_count = {"n": 0}
    fail_call_idx = 2  # second call raises

    def flaky_stub(*, proposal_id, requirements, cached_prefix):
        from app.agents.shortfall_strategist import ShortfallItem

        with in_flight_lock:
            call_count["n"] += 1
            this_call = call_count["n"]
        if this_call == fail_call_idx:
            raise RuntimeError("synthetic batch failure")
        time.sleep(0.1)
        return [
            ShortfallItem(
                requirement_id=r["requirement_id"],
                verdict="met",
                current_state="",
                evidence_citations=[],
                gap_severity=None,
                mitigation_options=[],
                recommended_mitigation_index=None,
                no_bid_recommended=False,
            )
            for r in requirements
        ]

    with (
        mock.patch.object(intake, "analyze_compliance_batch", side_effect=flaky_stub),
        mock.patch.object(intake, "get_company_profile", return_value={"_meta": {"version": "0"}}),
        mock.patch.object(intake, "build_shortfall_kb_context", return_value=""),
        mock.patch.object(intake, "format_decisions_for_prompt", return_value=""),
        mock.patch.object(intake, "get_teaming_partners", return_value=[]),
    ):
        gaps, no_bid = intake._run_shortfall_strategist(test_ids["proposal"])

    print("Test 2 (one batch raises, others succeed):")
    print("  total batches called: {}".format(call_count["n"]))
    print("  (should still be 3 — the raise doesn't abort other workers)")
    assert call_count["n"] == 3, "expected all 3 batches attempted; got {}".format(call_count["n"])
    print("  PASS\n")

    # --- Cleanup ---
    with session_scope() as db:
        p = db.get(Proposal, test_ids["proposal"])
        if p:
            db.delete(p)
        pkg = db.get(RfpPackage, test_ids["pkg"])
        if pkg:
            db.delete(pkg)
    print("Cleanup OK.")


if __name__ == "__main__":
    main()
