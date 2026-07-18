from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.proposal import Proposal
    from app.models.reviewer import ReviewerFinding


class ProposalSection(Base, TimestampMixin):
    """A drafted section of a proposal.

    Citations and [NEEDS_HUMAN] placeholders are stored as JSON to preserve
    the structured-output schema the Writer Team produces.
    """

    __tablename__ = "proposal_sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
    )

    section_id: Mapped[str] = mapped_column(String(64), nullable=False)
    section_title: Mapped[str] = mapped_column(String(500), nullable=False)
    section_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Outline Agent's brief: 3-6 sentences telling the Writer what this section
    # needs to do, who's reading it, and which evaluation criteria it targets.
    section_brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    word_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # When True, the Writer Team skips this section — it's primarily a cost /
    # pricing / labor-rate / fee section that the Cost Analysis Agent
    # (Weeks 12-13) will draft after it produces the actual numbers.
    requires_cost_analysis: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # User-driven flag set on the Outline tab. When True, the Writer
    # Team skips this section entirely — no draft is generated, no
    # cost-analysis fallback applies. Use case: outline produced a
    # wrapper section for a form / attachment / instructions block
    # that doesn't need narrative response, and the auto-filter in
    # outline.py didn't catch it. Reset to False on outline regenerate
    # because all sections are replaced.
    excluded_from_draft: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )

    draft_text_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_revision_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # [{requirement_id}, ...]
    compliance_items_addressed_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # [{claim, source_kb_doc, source_section, confidence}, ...]
    citations_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # [{location, description}, ...]
    needs_human_placeholders_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # [gap_id, ...]
    shortfall_mitigations_applied_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Set to True by the amendment-ingestion delta apply when this section's
    # `compliance_items_addressed_json` overlaps a modified or removed
    # requirement_id. The Draft tab shows a "Stale — compliance changed
    # since draft" chip + a "Re-draft this section" button when True. The
    # writer clears the flag on a successful regenerate.
    compliance_drift_pending: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default="0",
    )

    proposal: Mapped[Proposal] = relationship(back_populates="sections")
    findings: Mapped[list[ReviewerFinding]] = relationship(
        back_populates="section",
        cascade="all, delete-orphan",
    )
