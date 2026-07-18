"""UI-layer test. Most NiceGUI rendering needs a live HTTP context, but
the queries that drive the badges are pure functions over the DB —
we can exercise those directly. Also spot-checks that every @ui.page
handler is registered and the badge computation doesn't crash on
synthetic data. Creates its own temporary migrated SQLite DB. Run with:
    python scripts/_e2e_ui_test.py
"""

from datetime import datetime

from _e2e_harness import configure_e2e_database

configure_e2e_database("_e2e_ui_test")

from app.db.session import session_scope  # noqa: E402
from app.models import (  # noqa: E402
    ComplianceMatrixItem,
    GapAnalysis,
    LearnedRule,
    Proposal,
    ProposalSection,
    ReviewerFinding,
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
            title="UI TEST",
            status="draft_ready",
        )
        db.add(p)
        db.flush()
        test_ids["proposal"] = p.id

        # Build a section with placeholders so Needs Human badge fires
        sec = ProposalSection(
            proposal_id=p.id,
            section_id="SEC-1",
            section_title="UI test section",
            section_order=1,
            draft_text_markdown="some draft [^cite-1] [NEEDS_HUMAN: confirm X]",
            citations_json=[],
            needs_human_placeholders_json=[
                {"marker_text": "confirm X", "description": "test", "category": "specific_personnel"},
            ],
            shortfall_mitigations_applied_json=[],
            compliance_items_addressed_json=[],
        )
        db.add(sec)
        db.flush()
        test_ids["section"] = sec.id

        # Compliance item NOT assigned to any section -> Outline badge
        ci = ComplianceMatrixItem(
            proposal_id=p.id,
            requirement_id="REQ-UNASSIGNED",
            requirement_text="orphan req",
            source_doc="t.pdf",
            requirement_type="shall",
            category="technical",
        )
        db.add(ci)
        db.flush()
        test_ids["req"] = ci.id

        # Gap with no mitigation selected -> Gaps badge
        gap = GapAnalysis(
            proposal_id=p.id,
            requirement_id_fk=ci.id,
            gap_id="GAP-001",
            gap_severity="major",
            gap_description="test",
            current_state="open",
            mitigation_options_json=[
                {"approach": "self", "proposal_language_draft": "...", "honesty_check": "ok"},
            ],
        )
        db.add(gap)
        db.flush()
        test_ids["gap"] = gap.id

        # Pending finding -> Findings badge
        rf = ReviewerFinding(
            proposal_section_id=sec.id,
            reviewer_agent="A",
            pass_number=1,
            severity="MAJOR",
            category="compliance_gap",
            finding_text="test",
            suggested_fix="test",
        )
        db.add(rf)
        db.flush()
        test_ids["finding"] = rf.id

        # Draft learned rule -> Learned Guidance badge (KB page)
        rule = LearnedRule(
            kind="writer_avoid",
            rule_text="test rule",
            source_action="accept",
            status="draft",
        )
        db.add(rule)
        db.flush()
        test_ids["rule"] = rule.id

    print("Setup:", test_ids)
    print()

    # --- _compute_tab_badges exercises the badge-counting logic ---
    from app.ui.pages import _compute_tab_badges

    badges = _compute_tab_badges(test_ids["proposal"])
    print(f"badges = {badges}")
    # Expected:
    # gaps: 1 (one gap with no selection)
    # draft: 1 (one unresolved NEEDS_HUMAN placeholder)
    # findings: 1 (one pending finding)
    # submission: 0, cost_review: 0
    # outline / needs_human are intentionally unbadged
    assert badges["gaps"] == 1, badges
    assert badges["draft"] == 1, badges
    assert badges["findings"] == 1, badges
    assert badges["submission"] == 0, badges
    assert badges["cost_review"] == 0, badges
    assert "outline" not in badges, badges
    assert "needs_human" not in badges, badges
    print("  PASS\n")

    # --- count_rules_by_status (drives KB tab's Learned Guidance badge) ---
    from app.services.lessons import count_rules_by_status

    counts = count_rules_by_status()
    print(f"learned_rules counts: {counts}")
    assert counts.get("draft", 0) >= 1, counts
    print("  PASS\n")

    # --- Page registration: confirm every @ui.page route is in
    # NiceGUI's router. We don't actually navigate to any of them
    # (would need a live HTTP server) but we DO confirm they're
    # registered without errors.
    from nicegui import app as nicegui_app

    import app.ui.pages  # noqa: F401

    routes = [r.path for r in nicegui_app.routes if hasattr(r, "path")]
    expected_routes = [
        "/",
        "/proposals/new",
        "/proposals/{proposal_id}/progress",
        "/proposals/{proposal_id}",
        "/kb",
        "/config",
        "/admin",
    ]
    found = sum(1 for r in expected_routes if r in routes)
    print(f"page routes: {found}/{len(expected_routes)} expected routes registered")
    print(f"  registered: {sorted([r for r in routes if r in expected_routes])}")
    missing = [r for r in expected_routes if r not in routes]
    if missing:
        print(f"  MISSING: {missing}")
    assert found == len(expected_routes), "some pages didn't register"
    print("  PASS\n")

    # --- Cleanup ---
    with session_scope() as db:
        if "rule" in test_ids:
            r = db.get(LearnedRule, test_ids["rule"])
            if r:
                db.delete(r)
        p = db.get(Proposal, test_ids["proposal"])
        if p:
            db.delete(p)
        pkg = db.get(RfpPackage, test_ids["pkg"])
        if pkg:
            db.delete(pkg)
    print("Cleanup OK.")


if __name__ == "__main__":
    main()
