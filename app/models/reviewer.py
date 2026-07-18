from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import FindingCategory, FindingSeverity, ReviewerAgent
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.section import ProposalSection


class ReviewerFinding(Base, TimestampMixin):
    """One finding from Reviewer A or Reviewer B against a section.

    Findings drive the revision loop. The user reviews findings and either
    accepts them (which queues them as a directive for the Writer Team to
    apply on the next regenerate) or dismisses them. Resolved-in-pass-N
    is set when a re-review confirms the issue was addressed.
    """

    __tablename__ = "reviewer_findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_section_id: Mapped[int] = mapped_column(ForeignKey("proposal_sections.id", ondelete="CASCADE"))

    reviewer_agent: Mapped[ReviewerAgent] = mapped_column(String(8), nullable=False)
    pass_number: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[FindingSeverity] = mapped_column(String(16), nullable=False)
    category: Mapped[FindingCategory] = mapped_column(String(40), nullable=False)
    finding_text: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_in_pass_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # User-driven actions: dismissed = won't fix; accepted = queue for the
    # Writer's next directive-driven regenerate. Mutually exclusive in the UI.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    section: Mapped[ProposalSection] = relationship(back_populates="findings")
