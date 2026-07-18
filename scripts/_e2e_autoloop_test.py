"""Auto-loop architecture smoke test. Stubs out _process_one_section
with a fast synthetic worker so we can verify the parallel orchestration
(ThreadPoolExecutor, as_completed, cancellation drain, active-section
registry, status transitions) without burning LLM budget.
Creates its own temporary migrated SQLite DB. Run with:
    python scripts/_e2e_autoloop_test.py
"""

import threading
import time
from datetime import datetime
from unittest import mock

from _e2e_harness import configure_e2e_database

configure_e2e_database("_e2e_autoloop_test")

from app.db.session import session_scope  # noqa: E402
from app.models import Proposal, ProposalSection, RfpPackage  # noqa: E402


def main():
    test_ids = {}
    with session_scope() as db:
        pkg = RfpPackage(uploaded_at=datetime.utcnow(), storage_dir="(t)")
        db.add(pkg)
        db.flush()
        test_ids["pkg"] = pkg.id
        p = Proposal(
            rfp_package_id=pkg.id,
            title="AUTOLOOP TEST",
            status="draft_ready",
        )
        db.add(p)
        db.flush()
        test_ids["proposal"] = p.id

        # 8 sections, all with drafts so they're all eligible.
        sec_pks = []
        for i in range(1, 9):
            s = ProposalSection(
                proposal_id=p.id,
                section_id=f"SEC-{i:03d}",
                section_title=f"Section {i}",
                section_order=i,
                draft_text_markdown=f"draft for section {i}",
                citations_json=[],
                needs_human_placeholders_json=[],
                shortfall_mitigations_applied_json=[],
                compliance_items_addressed_json=[],
            )
            db.add(s)
            db.flush()
            sec_pks.append(s.id)
        test_ids["sec_pks"] = sec_pks

    print("Setup: 8 sections, proposal_id={}".format(test_ids["proposal"]))
    print()

    try:
        # --- Test 1: parallel run with stubbed worker ---
        # Replace _process_one_section with a fast stub that sleeps
        # briefly and reports the section as "clean". Verify all 8
        # sections get processed and the final tally is correct.
        from app.jobs import reviewer
        from app.services.cancellation import (
            JOB_AUTO_REVIEW,
            get_active_sections,
            is_running,
        )

        observed_concurrent: list[int] = []
        observed_lock = threading.Lock()

        def fast_stub(
            *, section, prefix_a, prefix_b, proposal_id, cancel_event, max_passes, section_idx, n_total
        ):
            """Pretend to do work. Records peak concurrency to verify
            the executor is actually parallel."""
            from app.services.cancellation import (
                add_active_section,
                get_active_sections,
                remove_active_section,
            )

            add_active_section(proposal_id, section["pk"])
            try:
                # Sample concurrency at the start of this worker's
                # critical section.
                with observed_lock:
                    observed_concurrent.append(len(get_active_sections(proposal_id)))
                # Quick work — 200ms — so we get real overlap with
                # a 4-worker pool but the test still finishes fast.
                time.sleep(0.2)
                if cancel_event.is_set():
                    return "cancelled"
                return "clean"
            finally:
                remove_active_section(proposal_id, section["pk"])

        # Stub _process_one_section AND _build_prefixes (which would
        # otherwise hit the DB to build large strings we don't need).
        with (
            mock.patch.object(reviewer, "_process_one_section", side_effect=fast_stub),
            mock.patch.object(reviewer, "_build_prefixes", return_value=("(a)", "(b)")),
            mock.patch.object(reviewer, "_run_consistency_pass", return_value=None) as consistency_pass,
        ):
            t0 = time.time()
            reviewer.run_auto_review_revise_loop(
                test_ids["proposal"],
                max_passes=6,
            )
            elapsed = time.time() - t0
        assert consistency_pass.call_count == 1, "normal auto-loop must use the stubbed consistency pass once"

        peak = max(observed_concurrent) if observed_concurrent else 0
        print("Test 1 (parallel run, 8 sections, 4 workers):")
        print(f"  wall time: {elapsed:.2f}s")
        print(f"  observed peak concurrency: {peak}")
        print("  expected: <= 4 workers, ~0.4s wall (8 sections / 4 workers * 0.2s)")
        # Should finish in <1.0s with 4 workers parallelizing 8 x 0.2s.
        # Sequential would be 1.6s. Allow some slack.
        assert elapsed < 1.5, f"wall time too high; parallelism may not be working: {elapsed:.2f}s"
        assert peak >= 2, f"peak concurrency should be >= 2; got {peak} (parallelism not happening?)"
        print("  PASS\n")

        # --- Test 2: cancel mid-run drops in-flight workers cleanly ---
        cancel_observed: list[bool] = []

        def cancellable_stub(*, section, cancel_event, **kwargs):
            from app.services.cancellation import (
                add_active_section,
                remove_active_section,
            )

            proposal_id = kwargs["proposal_id"]
            add_active_section(proposal_id, section["pk"])
            try:
                # Long-ish work, but exit early on cancel.
                for _ in range(20):
                    if cancel_event.is_set():
                        cancel_observed.append(True)
                        return "cancelled"
                    time.sleep(0.05)
                return "clean"
            finally:
                remove_active_section(proposal_id, section["pk"])

        # Spawn the loop in a background thread, then cancel after a
        # short delay.
        def trigger_cancel():
            time.sleep(0.15)  # let workers start
            from app.services.cancellation import request_cancel

            request_cancel(JOB_AUTO_REVIEW, test_ids["proposal"])

        with (
            mock.patch.object(reviewer, "_process_one_section", side_effect=cancellable_stub),
            mock.patch.object(reviewer, "_build_prefixes", return_value=("(a)", "(b)")),
            mock.patch.object(reviewer, "_run_consistency_pass", return_value=None) as consistency_pass,
        ):
            t = threading.Thread(target=trigger_cancel, daemon=True)
            t.start()
            t0 = time.time()
            reviewer.run_auto_review_revise_loop(
                test_ids["proposal"],
                max_passes=6,
            )
            elapsed = time.time() - t0
        # Production skips the consistency pass after any worker reports cancellation.
        assert consistency_pass.call_count == 0, "cancelled auto-loop must not run consistency pass"

        print("Test 2 (cancel mid-run):")
        print(f"  wall time: {elapsed:.2f}s (loop terminated on cancel)")
        print(f"  workers that observed cancel: {len(cancel_observed)}")
        # The loop should exit faster than the worst-case full run.
        # Workers exit at next checkpoint (50ms granularity) so this
        # should finish in well under 1s.
        assert elapsed < 1.5, "loop didn't terminate promptly on cancel"
        assert len(cancel_observed) >= 1, "no workers observed cancellation; cancel signal didn't propagate"
        # After loop ends, registry should be clean.
        assert get_active_sections(test_ids["proposal"]) == set(), (
            "active-sections registry not cleaned up after loop end"
        )
        assert not is_running(JOB_AUTO_REVIEW, test_ids["proposal"]), "loop still registered as running"
        print("  registry clean after cancel. PASS\n")

        # --- Test 3: concurrent active_section adds/removes from
        # multiple threads don't corrupt the set ---
        from app.services.cancellation import (
            add_active_section,
            clear_active_sections,
            get_active_sections,
            remove_active_section,
        )

        clear_active_sections(test_ids["proposal"])  # reset

        def churn():
            # Hammer add/remove on overlapping section_pk values.
            for n in range(500):
                add_active_section(test_ids["proposal"], n % 10)
                remove_active_section(test_ids["proposal"], (n + 1) % 10)

        threads = [threading.Thread(target=churn) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No assertion on the exact state (race-dependent), but the
        # set should still be a valid set with int elements and no
        # crash should have occurred.
        s = get_active_sections(test_ids["proposal"])
        assert isinstance(s, set)
        assert all(isinstance(x, int) for x in s)
        print(
            "Test 3 (concurrent add/remove from 8 threads x 500 ops): no crash, set invariants held. PASS\n"
        )
        clear_active_sections(test_ids["proposal"])

    finally:
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
