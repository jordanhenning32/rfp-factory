from __future__ import annotations

from datetime import UTC, datetime


def test_placeholder_resolutions_bump_revision_once_and_preserve_audit_metadata(
    inmemory_db, monkeypatch,
) -> None:
    import app.db.session as db_session
    import app.services.needs_human as needs_human
    from app.models import Proposal, ProposalSection, RfpPackage

    monkeypatch.setattr(needs_human, "session_scope", db_session.session_scope)
    markers = ("provide value", "apply signature", "remove optional note")
    with db_session.session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://needs-human-package",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="NEEDS_HUMAN revision contract",
        )
        db.add(proposal)
        db.flush()
        section = ProposalSection(
            proposal_id=proposal.id,
            section_id="SEC-001",
            section_title="Human inputs",
            draft_text_markdown=(
                f"A [NEEDS_HUMAN: {markers[0]}] B "
                f"[NEEDS_HUMAN: {markers[1]}] C "
                f"[NEEDS_HUMAN: {markers[2]}]"
            ),
            current_revision_number=7,
            needs_human_placeholders_json=[
                {
                    "marker_text": marker,
                    "description": marker,
                    "category": "other",
                }
                for marker in markers
            ],
        )
        db.add(section)
        db.flush()
        section_id = section.id

    assert needs_human.resolve_placeholder(
        proposal_section_pk=section_id,
        marker_text=markers[0],
        kind="edit",
        value="supplied text",
    )
    assert needs_human.resolve_placeholder(
        proposal_section_pk=section_id,
        marker_text=markers[1],
        kind="signature",
        value="/s/ Test Signer — July 21, 2032",
    )
    assert needs_human.resolve_placeholder(
        proposal_section_pk=section_id,
        marker_text=markers[2],
        kind="reject",
        value="",
    )

    with db_session.session_scope() as db:
        section = db.get(ProposalSection, section_id)
        assert section.current_revision_number == 10
        assert section.draft_text_markdown == (
            "A supplied text B /s/ Test Signer — July 21, 2032 C "
        )
        placeholders = {
            item["marker_text"]: item
            for item in section.needs_human_placeholders_json
        }
        assert placeholders[markers[0]]["resolution_kind"] == "edit"
        assert placeholders[markers[0]]["resolution_value"] == "supplied text"
        assert placeholders[markers[1]]["resolution_kind"] == "signature"
        assert placeholders[markers[2]]["resolution_kind"] == "reject"
        assert placeholders[markers[2]]["resolution_value"] == ""
        assert all(item["resolved"] for item in placeholders.values())

    # A stale double-click is an idempotent success, not another revision.
    assert needs_human.resolve_placeholder(
        proposal_section_pk=section_id,
        marker_text=markers[0],
        kind="edit",
        value="different stale value",
    )
    with db_session.session_scope() as db:
        section = db.get(ProposalSection, section_id)
        assert section.current_revision_number == 10
        assert "different stale value" not in section.draft_text_markdown
