"""Reviewer-pipeline integration test. Exercises the orchestration in
_review_one_section without burning LLM budget — review_a / review_b
are monkeypatched to return empty lists, so we verify the pre-flight
phase + persistence + state transitions deterministically. Creates its
own temporary migrated SQLite DB. Run with:
    python scripts/_e2e_pipeline_test.py
"""

from datetime import UTC, datetime
from unittest import mock

from _e2e_harness import configure_e2e_database

configure_e2e_database("_e2e_pipeline_test")

from app.db.session import SessionLocal, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    ComplianceMatrixItem,
    KnowledgeBaseDocument,
    Proposal,
    ProposalSection,
    ReviewerFinding,
    RfpPackage,
)


def main():
    test_ids = {}
    with session_scope() as db:
        pkg = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="(test)",
        )
        db.add(pkg)
        db.flush()
        test_ids["pkg"] = pkg.id
        p = Proposal(
            rfp_package_id=pkg.id,
            title="E2E PIPELINE TEST",
            status="draft_ready",
        )
        db.add(p)
        db.flush()
        test_ids["proposal"] = p.id

        # KB docs: one corporate (non-citable for past-perf), one
        # past_performance_subbed (citable).
        kb_corp = KnowledgeBaseDocument(
            filename="kb_corp.pdf",
            storage_path="(t)",
            document_class="corporate",
        )
        kb_pp = KnowledgeBaseDocument(
            filename="kb_pp.pdf",
            storage_path="(t)",
            document_class="past_performance_subbed",
        )
        db.add_all([kb_corp, kb_pp])
        db.flush()
        test_ids["kb_corp"] = kb_corp.id
        test_ids["kb_pp"] = kb_pp.id

        # Compliance items
        ci = ComplianceMatrixItem(
            proposal_id=p.id,
            requirement_id="REQ-A",
            requirement_text=(
                "The system must implement multi-factor authentication with TOTP and FIDO2 hardware tokens."
            ),
            source_doc="t.pdf",
            requirement_type="shall",
            category="technical",
        )
        db.add(ci)
        db.flush()
        test_ids["req_A"] = ci.id

        # Section with: 1 bad citation (past-perf to corporate) + 1
        # uncovered requirement (REQ-A), so pre-flight should produce
        # both a citation finding AND a coverage finding.
        sec = ProposalSection(
            proposal_id=p.id,
            section_id="SEC-PIPE",
            section_title="Pipeline Test Section",
            section_order=1,
            section_brief="(test)",
            draft_text_markdown=("We have rich personnel with strong skills. [^cite-1]"),
            citations_json=[
                {
                    "marker": "cite-1",
                    "claim": "Quadratic delivered the X modernization for Y agency.",
                    "source_kb_doc": f"KB DOC #{kb_corp.id} corporate",
                    "confidence": "HIGH",
                },
            ],
            needs_human_placeholders_json=[],
            shortfall_mitigations_applied_json=[],
            compliance_items_addressed_json=["REQ-A"],
        )
        db.add(sec)
        db.flush()
        test_ids["section"] = sec.id

    print("Setup:", test_ids)
    print()

    # Build the section dict shape that _review_one_section expects.
    section_dict = {
        "pk": test_ids["section"],
        "section_id": "SEC-PIPE",
        "section_title": "Pipeline Test Section",
        "page_limit": None,
        "word_limit": None,
        "compliance_items_addressed": ["REQ-A"],
        "applied_gaps": [],
        "draft_md": "We have rich personnel...",
        "citations": [],
        "needs_human": [],
        "section_brief": "(test)",
    }

    try:
        # --- Test 1: Happy path with both pre-flights firing ---
        from app.jobs.reviewer import _review_one_section

        # Monkeypatch the LLM-calling reviewers to no-ops.
        with (
            mock.patch("app.jobs.reviewer.review_a", return_value=[]),
            mock.patch("app.jobs.reviewer.review_b", return_value=[]),
        ):
            n_a, n_b = _review_one_section(
                section_dict,
                prefix_a="(test prefix)",
                prefix_b="(test prefix)",
                proposal_id=test_ids["proposal"],
            )

        print("Test 1 (happy path):")
        print(f"  n_a={n_a}, n_b={n_b} (review_a/b stubbed to empty)")

        # Pre-flight should produce: 1 citation finding (past-perf to
        # corporate) + 1 coverage finding (REQ-A salient terms missing).
        assert n_a == 2, f"expected 2 pre-flight findings persisted as A, got {n_a}"
        assert n_b == 0, "B was stubbed empty"

        # Verify persisted state
        with SessionLocal() as db:
            findings = (
                db.query(ReviewerFinding)
                .filter(ReviewerFinding.proposal_section_id == test_ids["section"])
                .all()
            )
            print(f"  persisted findings: {len(findings)}")
            for f in findings:
                cat = f.category.value if hasattr(f.category, "value") else f.category
                sev = f.severity.value if hasattr(f.severity, "value") else f.severity
                print(
                    "    [{}/{}] pass={} agent={} text='{}'".format(
                        sev,
                        cat,
                        f.pass_number,
                        f.reviewer_agent.value if hasattr(f.reviewer_agent, "value") else f.reviewer_agent,
                        f.finding_text[:80],
                    )
                )
        # All findings should be A and pass=1
        for f in findings:
            assert (f.reviewer_agent.value if hasattr(f.reviewer_agent, "value") else f.reviewer_agent) == "A"
            assert f.pass_number == 1
        print("  all findings persisted as A/pass=1. PASS\n")

        # --- Test 2: Pre-flight failure isolation ---
        # Monkeypatch one pre-flight to raise. The other should still run.
        # All this must happen WITHOUT propagating an error.
        # We do NOT pre-clear findings — _review_one_section calls
        # clear_unresolved_for_section itself, AFTER computing next_pass
        # off the existing max. Pre-clearing would reset the pass counter.

        def boom(_):
            raise RuntimeError("synthetic preflight failure")

        with (
            mock.patch("app.services.citation_check.check_section_citations", side_effect=boom),
            mock.patch("app.jobs.reviewer.review_a", return_value=[]),
            mock.patch("app.jobs.reviewer.review_b", return_value=[]),
        ):
            n_a, n_b = _review_one_section(
                section_dict,
                prefix_a="(t)",
                prefix_b="(t)",
                proposal_id=test_ids["proposal"],
            )

        print("Test 2 (one pre-flight raises):")
        print(f"  n_a={n_a}, n_b={n_b}")
        # Coverage check still ran -> 1 finding
        assert n_a == 1, f"coverage check should have run despite citation failure; got n_a={n_a}"
        # Assert pass number incremented to 2 (since pass 1 findings exist)
        with SessionLocal() as db:
            findings = (
                db.query(ReviewerFinding)
                .filter(ReviewerFinding.proposal_section_id == test_ids["section"])
                .all()
            )
            passes = sorted({f.pass_number for f in findings})
            print(f"  pass numbers seen: {passes}")
        assert 2 in passes, "expected pass=2 after re-run"
        print("  coverage pre-flight ran despite citation failure. PASS\n")

        # --- Test 3: get_pass_number_for_section + clear_unresolved ---
        from app.services.findings import (
            clear_unresolved_for_section,
            get_pass_number_for_section,
        )

        max_pass = get_pass_number_for_section(test_ids["section"])
        print("Test 3 (state queries):")
        print(f"  max pass_number = {max_pass}")
        assert max_pass == 2

        n_cleared = clear_unresolved_for_section(test_ids["section"])
        print(f"  clear_unresolved removed {n_cleared} row(s)")
        # No findings were accepted/dismissed/resolved -> all clearable
        with SessionLocal() as db:
            remaining = (
                db.query(ReviewerFinding)
                .filter(ReviewerFinding.proposal_section_id == test_ids["section"])
                .count()
            )
            print(f"  remaining findings: {remaining}")
        assert remaining == 0
        print("  PASS\n")

    finally:
        with session_scope() as db:
            p = db.get(Proposal, test_ids["proposal"])
            if p:
                db.delete(p)
            for kb_key in ("kb_corp", "kb_pp"):
                if kb_key in test_ids:
                    kb = db.get(KnowledgeBaseDocument, test_ids[kb_key])
                    if kb:
                        db.delete(kb)
            pkg = db.get(RfpPackage, test_ids["pkg"])
            if pkg:
                db.delete(pkg)
        print("Cleanup OK.")


if __name__ == "__main__":
    main()
