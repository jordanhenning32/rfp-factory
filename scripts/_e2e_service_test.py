"""Temporary E2E service-layer test. Self-contained: builds synthetic
proposal + sections + items, exercises every service module's public
surface, then cleans up. Creates its own temporary migrated SQLite DB
and does NOT call any LLM. Run with:
    python scripts/_e2e_service_test.py
"""

from datetime import UTC, datetime

from _e2e_harness import configure_e2e_database

configure_e2e_database("_e2e_service_test")

from app.db.session import SessionLocal, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    ComplianceMatrixItem,
    KnowledgeBaseDocument,
    LearnedRule,
    Proposal,
    ProposalSection,
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
            title="E2E TEST PROPOSAL",
            status="drafting",
        )
        db.add(p)
        db.flush()
        test_ids["proposal"] = p.id
        kb = KnowledgeBaseDocument(
            filename="e2e_past_perf.pdf",
            storage_path="(test)",
            document_class="past_performance_won",
        )
        db.add(kb)
        db.flush()
        test_ids["kb_won"] = kb.id
        kb2 = KnowledgeBaseDocument(
            filename="e2e_corporate.pdf",
            storage_path="(test)",
            document_class="corporate",
        )
        db.add(kb2)
        db.flush()
        test_ids["kb_corp"] = kb2.id

        for i, txt in enumerate(
            [
                "The contractor shall implement multi-factor authentication using TOTP and FIDO2 hardware tokens.",
                "The system must encrypt all data at rest using AES-256 and in transit using TLS 1.3.",
            ],
            start=1,
        ):
            ci = ComplianceMatrixItem(
                proposal_id=p.id,
                requirement_id=f"REQ-{i:03d}",
                requirement_text=txt,
                source_doc="test.pdf",
                requirement_type="shall",
                category="technical",
            )
            db.add(ci)
            db.flush()
            test_ids[f"req{i}"] = ci.id

        sec = ProposalSection(
            proposal_id=p.id,
            section_id="SEC-001",
            section_title="Security",
            section_order=1,
            section_brief="(test)",
            draft_text_markdown=(
                "We implement multi-factor authentication with TOTP and "
                "FIDO2 tokens. All data is encrypted at rest and in "
                "transit. [^cite-1]"
            ),
            citations_json=[
                {
                    "marker": "cite-1",
                    "claim": "Quadratic's platform encrypts data.",
                    "source_kb_doc": f"KB DOC #{kb2.id} corporate",
                    "confidence": "HIGH",
                },
                {
                    "marker": "cite-2",
                    "claim": "Quadratic delivered the modernization for HHS.",
                    "source_kb_doc": f"KB DOC #{kb2.id} corporate",
                    "confidence": "HIGH",
                },
                {
                    "marker": "cite-3",
                    "claim": "Quadratic implemented X for Y.",
                    "source_kb_doc": "KB DOC #99999 imaginary",
                    "confidence": "MEDIUM",
                },
            ],
            needs_human_placeholders_json=[
                {
                    "marker_text": "confirm staffing plan",
                    "description": "test",
                    "category": "specific_personnel",
                },
            ],
            shortfall_mitigations_applied_json=[],
            compliance_items_addressed_json=["REQ-001", "REQ-002"],
        )
        db.add(sec)
        db.flush()
        test_ids["section"] = sec.id

    print("Setup OK:", {k: v for k, v in test_ids.items()})
    print()

    try:
        # --- citation_check ---
        from app.services.citation_check import check_section_citations

        findings = check_section_citations(test_ids["section"])
        print(f"citation_check: {len(findings)} finding(s)")
        for f in findings:
            print(f"  [{f.severity}/{f.category}] {f.finding_text[:120]}")
        assert len(findings) == 2, "expected 2 citation findings"
        cats = {f.category for f in findings}
        assert cats == {"uncited_claim", "hallucination"}, cats
        print("  PASS\n")

        # --- preflight_checks (compliance coverage) ---
        from app.services.preflight_checks import check_compliance_coverage

        findings = check_compliance_coverage(test_ids["section"])
        print(f"compliance_coverage: {len(findings)} finding(s)")
        for f in findings:
            print(f"  [{f.severity}/{f.category}] {f.finding_text[:120]}")
        print("  (info — expected 0 since draft mentions all key terms)\n")

        # --- needs_human ---
        from app.services.needs_human import reconcile_placeholders

        changed = reconcile_placeholders(test_ids["section"])
        print(f"reconcile_placeholders: changed={changed}")
        with SessionLocal() as db:
            s = db.get(ProposalSection, test_ids["section"])
            phs = list(s.needs_human_placeholders_json)
        # The "confirm staffing plan" marker isn't inline → reconcile
        # auto-resolves as manual_edit.
        assert phs[0].get("resolved") is True, phs
        assert phs[0].get("resolution_kind") == "manual_edit"
        print("  reconcile auto-resolved marker not in draft. PASS\n")

        # --- findings (CRUD) ---
        from app.services.findings import (
            accept_finding,
            build_directive_from_findings,
            clear_unresolved_for_section,
            dismiss_finding,
            get_accepted_findings_for_section,
            get_unresolved_findings_for_section,
            mark_findings_resolved,
            persist_findings,
        )

        draft_findings = check_section_citations(test_ids["section"])
        n = persist_findings(
            proposal_section_pk=test_ids["section"],
            reviewer_agent="A",
            pass_number=1,
            findings=draft_findings,
        )
        assert n == 2, f"expected 2 persisted, got {n}"
        print(f"persist_findings: persisted {n}")

        unresolved = get_unresolved_findings_for_section(test_ids["section"])
        assert len(unresolved) == 2
        print(f"get_unresolved: {len(unresolved)}")

        accept_finding(unresolved[0]["id"])
        dismiss_finding(unresolved[1]["id"], reason="not applicable")

        unresolved = get_unresolved_findings_for_section(test_ids["section"])
        accepted = get_accepted_findings_for_section(test_ids["section"])
        assert len(unresolved) == 1
        assert len(accepted) == 1
        print(f"  after accept+dismiss: {len(unresolved)} unresolved, {len(accepted)} accepted")

        directive = build_directive_from_findings(accepted)
        assert "[CRITICAL]" in directive
        print(f"  build_directive: {len(directive)} chars, severity tag included")

        mark_findings_resolved([accepted[0]["id"]], pass_number=1)
        # get_unresolved_findings_for_section filters out BOTH resolved
        # AND dismissed — semantics is "still pending". With one
        # finding now resolved and the other dismissed, both are gone.
        unresolved = get_unresolved_findings_for_section(test_ids["section"])
        assert len(unresolved) == 0, (
            "after resolving the accepted one and the other being "
            f"dismissed, nothing should be pending; got {len(unresolved)}"
        )
        print(f"  after mark_resolved: {len(unresolved)} pending")

        n_cleared = clear_unresolved_for_section(test_ids["section"])
        # All 2 findings were already protected (1 dismissed, 1 resolved
        # via accepted_at + resolved_in_pass_number both set), so 0 cleared.
        print(f"  clear_unresolved removed {n_cleared} (accepted/dismissed/resolved protected)")
        print("  PASS\n")

        # --- lessons ---
        from app.services.lessons import (
            archive_rule,
            format_writer_guidance,
            get_category_action_rates,
        )

        with session_scope() as db:
            rule = LearnedRule(
                kind="writer_avoid",
                rule_text="Test rule: do not foo without bar.",
                source_action="accept",
                source_category="uncited_claim",
                source_severity="CRITICAL",
                source_reviewer="A",
                status="approved",
            )
            db.add(rule)
            db.flush()
            test_ids["rule"] = rule.id

        writer_block = format_writer_guidance()
        assert "do not foo without bar" in writer_block, writer_block[:200]
        print(f"format_writer_guidance picks up approved rule ({len(writer_block)} chars)")

        rates = get_category_action_rates()
        print(f"get_category_action_rates: {rates}")

        archive_rule(test_ids["rule"])
        writer_block = format_writer_guidance()
        assert "do not foo without bar" not in writer_block
        print("after archive: rule no longer injected. PASS\n")

        # --- cancellation ---
        from app.services.cancellation import (
            JOB_AUTO_REVIEW,
            add_active_section,
            clear_active_sections,
            get_active_sections,
            is_cancelled,
            is_running,
            register,
            remove_active_section,
            request_cancel,
            unregister,
        )

        ev = register(JOB_AUTO_REVIEW, test_ids["proposal"])
        assert ev is not None
        assert is_running(JOB_AUTO_REVIEW, test_ids["proposal"])
        add_active_section(test_ids["proposal"], 100)
        add_active_section(test_ids["proposal"], 200)
        add_active_section(test_ids["proposal"], 300)
        assert get_active_sections(test_ids["proposal"]) == {100, 200, 300}
        remove_active_section(test_ids["proposal"], 200)
        assert get_active_sections(test_ids["proposal"]) == {100, 300}
        clear_active_sections(test_ids["proposal"])
        assert get_active_sections(test_ids["proposal"]) == set()
        request_cancel(JOB_AUTO_REVIEW, test_ids["proposal"])
        assert is_cancelled(JOB_AUTO_REVIEW, test_ids["proposal"])
        assert not is_running(JOB_AUTO_REVIEW, test_ids["proposal"])
        unregister(JOB_AUTO_REVIEW, test_ids["proposal"])
        print("cancellation: register/cancel/active-set OK. PASS\n")

        # --- stages ---
        from app.services.stages import record_stage

        record_stage(test_ids["proposal"], "test stage 1")
        record_stage(99999999, "FK-safe test against deleted proposal")
        print("stages.record_stage OK on real + nonexistent. PASS\n")

        # --- sections ---
        from app.services.sections import (
            save_manual_edit,
            set_section_cost_deferred,
        )

        save_manual_edit(test_ids["section"], "edited content")
        with SessionLocal() as db:
            s = db.get(ProposalSection, test_ids["section"])
            assert s.draft_text_markdown == "edited content"
        print("sections.save_manual_edit OK")
        set_section_cost_deferred(test_ids["section"], True)
        with SessionLocal() as db:
            s = db.get(ProposalSection, test_ids["section"])
            assert s.requires_cost_analysis is True
        set_section_cost_deferred(test_ids["section"], False)
        print("sections.set_section_cost_deferred OK. PASS\n")

        # --- llm (no-network) ---
        from app.services.llm import (
            _is_transient_error,
            estimate_anthropic_cost,
            estimate_gemini_cost,
            estimate_openai_cost,
        )

        assert estimate_anthropic_cost("claude-opus-4-7", 1000, 100) > 0
        assert estimate_openai_cost("gpt-5.5", 1000, 100, cached_input_tokens=500) > 0
        assert estimate_gemini_cost("gemini-2.5-flash", 1000, 100) > 0
        print("llm cost estimators OK")

        # Rate-limit class names + message patterns.
        class _RL(Exception):
            pass

        _RL.__name__ = "RateLimitError"
        assert _is_transient_error(_RL("rate"))
        assert _is_transient_error(Exception("HTTP 429"))
        assert _is_transient_error(Exception("quota exceeded"))

        # Server-unavailable class names + message patterns (added when
        # Gemini started returning 503s during peak periods).
        class _SU(Exception):
            pass

        _SU.__name__ = "ServiceUnavailable"
        assert _is_transient_error(_SU("temporary"))
        assert _is_transient_error(Exception("503 Service Unavailable"))
        assert _is_transient_error(Exception("502 Bad Gateway"))
        assert _is_transient_error(Exception("504 Gateway Timeout"))

        # 500 stays out of scope — could be a deterministic code-side bug.
        assert not _is_transient_error(Exception("500 internal"))
        # Random other errors are not retryable.
        assert not _is_transient_error(ValueError("bad input"))
        print("_is_transient_error classification OK. PASS\n")

    finally:
        with session_scope() as db:
            if "rule" in test_ids:
                r = db.get(LearnedRule, test_ids["rule"])
                if r:
                    db.delete(r)
            p = db.get(Proposal, test_ids["proposal"])
            if p:
                db.delete(p)  # cascades sections + findings
            for kb_key in ("kb_won", "kb_corp"):
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
