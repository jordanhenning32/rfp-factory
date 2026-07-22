"""Tests for app/services/amendments.py::apply_amendment_delta.

Seeds an in-memory proposal with three active compliance items + two
proposal sections, builds a synthetic ComplianceExtractionResult, and
verifies that apply_amendment_delta:
  - inserts new items with fresh REQ-NNN ids
  - supersedes modified items (new row + old row marked superseded)
  - marks removed items with status='removed'
  - flips compliance_drift_pending on the right ProposalSection rows
"""

from __future__ import annotations

from datetime import UTC, datetime


def test_apply_amendment_delta_mutates_rows_and_flags_sections(inmemory_db):
    """Exercises the full apply contract against a real in-memory DB."""
    from app.agents.compliance_matrix import (
        ComplianceExtractionResult,
        ExtractedComplianceItem,
    )
    from app.core.enums import (
        ComplianceStatus,
        ProposalRole,
        ProposalStatus,
        RequirementCategory,
        RequirementType,
    )
    from app.db.session import session_scope
    from app.models import (
        ComplianceMatrixItem,
        Proposal,
        ProposalSection,
        RfpPackage,
        RfpPackageDocument,
    )
    from app.services.amendments import apply_amendment_delta

    # ── seed ──────────────────────────────────────────────────────────
    with session_scope() as db:
        pkg = RfpPackage(
            uploaded_by="pytest",
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://pkg",
        )
        db.add(pkg)
        db.flush()
        pkg_id = pkg.id

        proposal = Proposal(
            rfp_package_id=pkg_id,
            title="Test Proposal",
            role=ProposalRole.PRIME,
            status=ProposalStatus.INTAKING,
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id

        # 3 active compliance items: REQ-001, REQ-002, REQ-003
        seeded_items = {}
        for rid, txt in [
            ("REQ-001", "Submit a 25-page technical narrative."),
            ("REQ-002", "Submit a separate cost narrative."),
            ("REQ-003", "Provide three past-performance references."),
        ]:
            item = ComplianceMatrixItem(
                proposal_id=proposal_id,
                requirement_id=rid,
                requirement_text=txt,
                source_doc="original_rfp.pdf",
                source_section="Section 3",
                source_page=5,
                requirement_type=RequirementType.SHALL,
                category=RequirementCategory.TECHNICAL,
                compliance_status=ComplianceStatus.TO_BE_DRAFTED,
                status="active",
            )
            db.add(item)
            seeded_items[rid] = item

        # 1 amendment document
        doc = RfpPackageDocument(
            rfp_package_id=pkg_id,
            filename="Amendment_0001.pdf",
            storage_path="memory://pkg/Amendment_0001.pdf",
            document_role="amendment",
            sequence_number=1,
        )
        db.add(doc)
        db.flush()
        doc_id = doc.id

        # S1 addresses REQ-001 (will be modified) — should be marked stale
        section_1 = ProposalSection(
            proposal_id=proposal_id,
            section_id="S1",
            section_title="Technical Approach",
            compliance_items_addressed_json=["REQ-001"],
        )
        db.add(section_1)
        # S2 addresses REQ-003 (will NOT change) — should stay clean
        section_2 = ProposalSection(
            proposal_id=proposal_id,
            section_id="S2",
            section_title="Management Approach",
            compliance_items_addressed_json=["REQ-003"],
        )
        db.add(section_2)
        db.flush()
        section_1_id = section_1.id
        seeded_items["REQ-001"].linked_response_section_id = section_1_id
        seeded_items["REQ-003"].linked_response_section_id = section_2.id

    # ── build a synthetic delta and apply it ─────────────────────────
    delta = ComplianceExtractionResult(
        new_items=[
            ExtractedComplianceItem(
                requirement_id="(ignored)",
                requirement_text="The contractor shall submit weekly status reports.",
                requirement_type="shall",
                category="management",
            ),
        ],
        modified_items=[
            {
                "existing_id": "REQ-001",
                "new_text": "Submit a 30-page technical narrative.",
                "change_summary": "Page limit raised from 25 to 30.",
            },
        ],
        removed_items=[
            {
                "existing_id": "REQ-002",
                "reason": "Cost narrative absorbed into Section M.",
            },
        ],
    )

    with session_scope() as db:
        report = apply_amendment_delta(
            proposal_id=proposal_id,
            amendment_document_id=doc_id,
            delta=delta,
            db=db,
        )

    # ── verify report counts ─────────────────────────────────────────
    assert report.n_new == 1
    assert report.n_modified == 1
    assert report.n_removed == 1
    assert "S1" in report.sections_marked_stale
    assert "S2" not in report.sections_marked_stale
    assert report.due_date_changed is False
    assert report.page_limit_changes == []

    # ── verify row mutations ─────────────────────────────────────────
    with session_scope() as db:
        items = (
            db.query(ComplianceMatrixItem)
            .filter(ComplianceMatrixItem.proposal_id == proposal_id)
            .order_by(ComplianceMatrixItem.id)
            .all()
        )
        # 3 originals + 1 superseded-new + 1 brand-new = 4 active + 1 superseded
        # Total rows = 5 (3 original kept, 1 of which marked superseded; +1 new active
        # for the modified item; +1 net-new from new_items).
        # Specifically: REQ-001 (original→superseded) + REQ-002 (removed)
        # + REQ-003 (untouched) + REQ-001 (new active) + REQ-004 (net-new) = 5 rows
        assert len(items) == 5

        active = [i for i in items if i.status == "active"]
        superseded = [i for i in items if i.status == "superseded"]
        removed = [i for i in items if i.status == "removed"]

        # 1 untouched (REQ-003) + 1 new (REQ-001 post-amendment) + 1 net-new = 3 active
        assert len(active) == 3
        assert len(superseded) == 1
        assert len(removed) == 1

        # REQ-001 has TWO rows now: the old one (superseded) and the new one (active)
        req001_rows = [i for i in items if i.requirement_id == "REQ-001"]
        assert len(req001_rows) == 2

        req001_old = next(i for i in req001_rows if i.status == "superseded")
        assert req001_old.superseded_by_id is not None

        req001_new = next(i for i in req001_rows if i.status == "active")
        assert "30-page" in req001_new.requirement_text
        assert req001_new.amendment_origin == "Amendment_0001.pdf"
        assert req001_new.linked_response_section_id == section_1_id
        # Sanity: the superseded row points forward to the new row
        assert req001_old.superseded_by_id == req001_new.id

        # REQ-002 is the one removed item
        req002 = next(i for i in items if i.requirement_id == "REQ-002")
        assert req002.status == "removed"
        assert req002.amendment_origin == "Amendment_0001.pdf"

        # The new row is REQ-004 (next sequential after REQ-001..REQ-003)
        net_new = next(i for i in items if i.requirement_id not in {"REQ-001", "REQ-002", "REQ-003"})
        assert net_new.requirement_id == "REQ-004"
        assert net_new.status == "active"
        assert net_new.amendment_origin == "Amendment_0001.pdf"
        assert "weekly" in net_new.requirement_text.lower()

        # ── verify drift flags on sections ───────────────────────────
        sections = db.query(ProposalSection).filter(ProposalSection.proposal_id == proposal_id).all()
        sec_by_id = {s.section_id: s for s in sections}
        # S1 addresses REQ-001 (modified) → drift_pending True
        assert sec_by_id["S1"].compliance_drift_pending is True
        # S2 addresses REQ-003 (unchanged) → drift_pending False
        assert sec_by_id["S2"].compliance_drift_pending is False
