"""Synthetic scenarios for deterministic credential-allowlist checks."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

from app.core.enums import ProposalRole, ProposalStatus
from app.models import Proposal, ProposalSection, RfpPackage


def _preflight_checks():
    import app.services.preflight_checks as preflight_checks

    return importlib.reload(preflight_checks)


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


def _seed_section(db, *, draft_text_markdown: str) -> int:
    proposal = _seed_proposal(db)
    section = ProposalSection(
        proposal_id=proposal.id,
        section_id="1.0",
        section_title="Technical Approach",
        section_order=1,
        draft_text_markdown=draft_text_markdown,
        compliance_items_addressed_json=[],
        citations_json=[],
        needs_human_placeholders_json=[],
        shortfall_mitigations_applied_json=[],
    )
    db.add(section)
    db.flush()
    return section.id


def test_non_allowlisted_credential_creates_critical_hallucination(
    inmemory_db,
    monkeypatch,
) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        section_id = _seed_section(
            db,
            draft_text_markdown="Quadratic maintains SOC 2 controls.",
        )
        db.commit()

    preflight_checks = _preflight_checks()
    monkeypatch.setattr(
        preflight_checks,
        "get_company_profile",
        lambda: {"certifications": ["ISO 27001"]},
    )

    findings = preflight_checks.check_section_credentials_allowlisted(section_id)

    assert len(findings) >= 1
    assert findings[0].severity == "CRITICAL"
    assert findings[0].category == "hallucination"
    assert "soc 2" in findings[0].finding_text.lower()
    assert "ISO 27001" in findings[0].finding_text


def test_allowlisted_credential_returns_no_findings(
    inmemory_db,
    monkeypatch,
) -> None:
    from app.db.session import SessionLocal as InMemorySession

    with InMemorySession() as db:
        section_id = _seed_section(
            db,
            draft_text_markdown="Quadratic maintains ISO 27001 practices.",
        )
        db.commit()

    preflight_checks = _preflight_checks()
    monkeypatch.setattr(
        preflight_checks,
        "get_company_profile",
        lambda: {"certifications": ["ISO 27001"]},
    )

    assert preflight_checks.check_section_credentials_allowlisted(section_id) == []
