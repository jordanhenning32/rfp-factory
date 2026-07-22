from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ComplianceStatus, GapSeverity, RequirementCategory, RequirementType
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.proposal import Proposal


class ComplianceMatrixItem(Base, TimestampMixin):
    """A single requirement extracted from the RFP package.

    Every entry MUST have a source citation (source_doc/section/page).
    """

    __tablename__ = "compliance_matrix_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
    )

    requirement_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_doc: Mapped[str] = mapped_column(String(500), nullable=False)
    # Stable provenance for source-aware review. ``source_doc`` remains the
    # human-readable filename, while this FK distinguishes documents that
    # happen to share a filename and lets review state key to the canonical
    # package row. Nullable for pre-migration/amendment history.
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("rfp_package_documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_page: Mapped[int | None] = mapped_column(nullable=True)

    requirement_type: Mapped[RequirementType] = mapped_column(String(32), nullable=False)
    category: Mapped[RequirementCategory] = mapped_column(String(32), nullable=False)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)

    compliance_status: Mapped[ComplianceStatus] = mapped_column(
        String(32),
        default=ComplianceStatus.TO_BE_DRAFTED,
        nullable=False,
    )
    linked_response_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposal_sections.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Submission checklist state (UI-driven, set on the Submission Checklist tab).
    # Useful for items where the user needs to gather and attach a document before
    # submission (W-9, COI, references form, etc.).
    submission_obtained: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    submission_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # User-set "this isn't really a narrative item" flag. Set from the
    # Outline tab's unassigned-items recovery UI via the "Mark N/A"
    # option. Excluded items are filtered out of the unassigned-items
    # warning so the user isn't pestered about them again.
    excluded_from_outline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Amendment / Q&A provenance — when this requirement was added or
    # modified by an amendment document, holds the amendment's filename
    # (e.g. "Amendment_0001.pdf"). NULL for items that came from the
    # original RFP. Surfaced in the Compliance tab as a blue chip and
    # in Reviewer A's user prompt as the AMENDED ITEMS block.
    amendment_origin: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Status of this row in the requirement lifecycle:
    #   'active'     — currently in force; surfaces in the Compliance tab
    #                  by default and is the only status the Writer Team
    #                  considers.
    #   'superseded' — replaced by a newer row carrying the same
    #                  requirement_id; `superseded_by_id` points to the
    #                  active row. Hidden by default; users can toggle
    #                  "All statuses" on the Compliance tab to see them.
    #   'removed'    — cancelled by an amendment; kept in the DB for
    #                  audit but not surfaced in writer / reviewer flows.
    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        nullable=False,
        server_default="active",
    )

    # Self-FK to the new row when this row was superseded by an amendment.
    # NULL for active + removed rows. ondelete=SET NULL so deleting the
    # parent proposal cascades cleanly through proposal_id without orphan
    # forward-pointer races.
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("compliance_matrix_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    proposal: Mapped[Proposal] = relationship(back_populates="compliance_items")
    gaps: Mapped[list[GapAnalysis]] = relationship(
        back_populates="requirement",
        cascade="all, delete-orphan",
    )


class GapAnalysis(Base, TimestampMixin):
    """Shortfall Strategist output for a single compliance item."""

    __tablename__ = "gap_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
    )
    requirement_id_fk: Mapped[int] = mapped_column(
        ForeignKey("compliance_matrix_items.id", ondelete="CASCADE")
    )

    gap_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gap_severity: Mapped[GapSeverity] = mapped_column(String(32), nullable=False)
    gap_description: Mapped[str] = mapped_column(Text, nullable=False)
    current_state: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Each option: {approach, proposal_language_draft, honesty_check, additional_action_required, partner_suggestions?}
    mitigation_options_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    recommended_mitigation_index: Mapped[int | None] = mapped_column(nullable=True)

    # User decisions on this gap. selected_mitigation_index is what the user
    # chose to use (may differ from recommended). resolved=True means the user
    # has confirmed mitigation is in place or accepted the gap as a known issue.
    # selected_partner_name applies only to teaming-style mitigations: it
    # records which specific partner from the suggestion list the user picked.
    selected_mitigation_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_partner_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    proposal: Mapped[Proposal] = relationship(back_populates="gap_analyses")
    requirement: Mapped[ComplianceMatrixItem] = relationship(back_populates="gaps")
