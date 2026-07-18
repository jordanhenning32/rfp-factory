"""Synthetic scenarios for deterministic citation pre-flight checks."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

from app.core.enums import (
    KbDocumentClass,
    KbDocumentStatus,
    ProposalRole,
    ProposalStatus,
)
from app.models import (
    KnowledgeBaseDocument,
    Proposal,
    ProposalSection,
    RfpPackage,
)


def _check_section_citations():
    import app.services.citation_check as citation_check

    return importlib.reload(citation_check).check_section_citations


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


def _seed_section(db, citations: list[dict]) -> int:
    proposal = _seed_proposal(db)
    section = ProposalSection(
        proposal_id=proposal.id,
        section_id="1.0",
        section_title="Technical Approach",
        section_order=1,
        draft_text_markdown="Synthetic drafted section.",
        citations_json=citations,
        compliance_items_addressed_json=[],
        needs_human_placeholders_json=[],
        shortfall_mitigations_applied_json=[],
    )
    db.add(section)
    db.flush()
    return section.id


def _seed_kb_doc(
    db,
    *,
    filename: str,
    document_class: KbDocumentClass,
    extracted_text_md: str = "Synthetic source text.",
) -> KnowledgeBaseDocument:
    doc = KnowledgeBaseDocument(
        filename=filename,
        storage_path=f"memory://{filename}",
        document_class=document_class,
        tags_json=[],
        status=KbDocumentStatus.ACTIVE,
        extracted_text_md=extracted_text_md,
        metadata_json={},
    )
    db.add(doc)
    db.flush()
    return doc


def test_missing_kb_doc_creates_critical_hallucination(inmemory_db) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        section_id = _seed_section(
            db,
            [
                {
                    "marker": "1",
                    "claim": "Quadratic can support secure delivery.",
                    "source_kb_doc": "KB DOC #999 - missing",
                    "source_section": "Evidence",
                    "confidence": "HIGH",
                }
            ],
        )
        db.commit()

    findings = _check_section_citations()(section_id)

    assert len(findings) == 1
    assert findings[0].severity == "CRITICAL"
    assert findings[0].category == "hallucination"
    assert "KB DOC #999" in findings[0].finding_text
    assert "does not exist" in findings[0].finding_text


def test_prior_proposal_won_source_creates_critical_uncited_claim(
    inmemory_db,
) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        doc = _seed_kb_doc(
            db,
            filename="prior-won.md",
            document_class=KbDocumentClass.PRIOR_PROPOSAL_WON,
        )
        section_id = _seed_section(
            db,
            [
                {
                    "marker": "2",
                    "claim": "Quadratic has experience operating secure platforms.",
                    "source_kb_doc": f"KB DOC #{doc.id} - prior proposal",
                    "source_section": "Past Performance",
                    "confidence": "HIGH",
                }
            ],
        )
        db.commit()

    findings = _check_section_citations()(section_id)

    assert len(findings) == 1
    assert findings[0].severity == "CRITICAL"
    assert findings[0].category == "uncited_claim"
    assert "prior_proposal_won" in findings[0].finding_text
    assert "NEVER citable as completed work" in findings[0].finding_text


def test_active_past_performance_claim_with_corporate_source_is_uncited(
    inmemory_db,
) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        doc = _seed_kb_doc(
            db,
            filename="corporate.md",
            document_class=KbDocumentClass.CORPORATE,
        )
        section_id = _seed_section(
            db,
            [
                {
                    "marker": "3",
                    "claim": "Quadratic delivered integrated secure analytics operations for Treasury.",
                    "source_kb_doc": f"KB DOC #{doc.id} - corporate profile",
                    "source_section": "Capabilities",
                    "confidence": "HIGH",
                }
            ],
        )
        db.commit()

    findings = _check_section_citations()(section_id)

    assert len(findings) == 1
    assert findings[0].severity == "CRITICAL"
    assert findings[0].category == "uncited_claim"
    assert "corporate" in findings[0].finding_text
    assert "not past_performance_won/subbed" in findings[0].finding_text


def test_company_profile_source_without_kb_doc_marker_is_skipped(
    inmemory_db,
) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        section_id = _seed_section(
            db,
            [
                {
                    "marker": "4",
                    "claim": "Quadratic delivered secure operations for a customer.",
                    "source_kb_doc": "company_profile.past_performance[0]",
                    "source_section": "past_performance",
                    "confidence": "HIGH",
                }
            ],
        )
        db.commit()

    findings = _check_section_citations()(section_id)

    assert findings == []
