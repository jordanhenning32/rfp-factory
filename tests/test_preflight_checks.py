"""Synthetic scenarios for deterministic compliance pre-flight checks."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

from app.core.enums import (
    ComplianceStatus,
    ProposalRole,
    ProposalStatus,
    RequirementCategory,
    RequirementType,
)
from app.models import (
    ComplianceMatrixItem,
    Proposal,
    ProposalSection,
    RfpPackage,
)


def _check_compliance_coverage():
    import app.services.preflight_checks as preflight_checks

    return importlib.reload(preflight_checks).check_compliance_coverage


def _seed_proposal(db) -> Proposal:
    pkg = RfpPackage(
        uploaded_by="pytest",
        uploaded_at=datetime.now(UTC),
        storage_dir="memory://rfp-package",
    )
    db.add(pkg)
    db.flush()
    proposal = Proposal(
        rfp_package_id=pkg.id,
        title="Synthetic Proposal",
        role=ProposalRole.PRIME,
        status=ProposalStatus.DRAFT_READY,
    )
    db.add(proposal)
    db.flush()
    return proposal


def _seed_section_with_requirement(
    db,
    *,
    requirement_id: str,
    requirement_text: str,
    draft_text_markdown: str,
) -> int:
    proposal = _seed_proposal(db)
    section = ProposalSection(
        proposal_id=proposal.id,
        section_id="1.0",
        section_title="Technical Approach",
        section_order=1,
        draft_text_markdown=draft_text_markdown,
        compliance_items_addressed_json=[requirement_id],
        citations_json=[],
        needs_human_placeholders_json=[],
        shortfall_mitigations_applied_json=[],
    )
    db.add(section)
    db.flush()
    item = ComplianceMatrixItem(
        proposal_id=proposal.id,
        requirement_id=requirement_id,
        requirement_text=requirement_text,
        source_doc="rfp.pdf",
        source_section="Section L",
        source_page=1,
        requirement_type=RequirementType.SHALL,
        category=RequirementCategory.TECHNICAL,
        compliance_status=ComplianceStatus.TO_BE_DRAFTED,
    )
    db.add(item)
    db.flush()
    return section.id


def test_below_min_salient_terms_threshold_returns_empty(inmemory_db) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        section_id = _seed_section_with_requirement(
            db,
            requirement_id="REQ-LOW",
            requirement_text="Comply with FAR.",
            draft_text_markdown="This draft does not address the short requirement.",
        )
        db.commit()

    findings = _check_compliance_coverage()(section_id)

    assert findings == []


def test_majority_salient_terms_missing_creates_major_compliance_gap(
    inmemory_db,
) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        section_id = _seed_section_with_requirement(
            db,
            requirement_id="REQ-MISSING",
            requirement_text=(
                "The contractor shall provide cybersecurity monitoring "
                "vulnerability remediation incident response reporting."
            ),
            draft_text_markdown="Our approach addresses cybersecurity.",
        )
        db.commit()

    findings = _check_compliance_coverage()(section_id)

    assert len(findings) == 1
    assert findings[0].severity == "MAJOR"
    assert findings[0].category == "compliance_gap"
    assert "REQ-MISSING" in findings[0].finding_text


def test_majority_salient_terms_present_returns_empty(inmemory_db) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        section_id = _seed_section_with_requirement(
            db,
            requirement_id="REQ-COVERED",
            requirement_text=(
                "The contractor shall provide cybersecurity monitoring "
                "vulnerability remediation incident response reporting."
            ),
            draft_text_markdown=(
                "The contractor will provide cybersecurity monitoring and "
                "vulnerability remediation with incident response reporting."
            ),
        )
        db.commit()

    findings = _check_compliance_coverage()(section_id)

    assert findings == []


def test_all_short_acronym_tokens_returns_empty(inmemory_db) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        section_id = _seed_section_with_requirement(
            db,
            requirement_id="REQ-ACRONYMS",
            requirement_text="API MFA AI ML RFP FAR SLA",
            draft_text_markdown="This draft is intentionally unrelated.",
        )
        db.commit()

    findings = _check_compliance_coverage()(section_id)

    assert findings == []
