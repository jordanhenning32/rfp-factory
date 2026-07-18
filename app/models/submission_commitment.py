"""SubmissionCommitment — user-tracked deliverable artifacts.

Created when the user resolves a Provide-Value placeholder dialog with
the "Also track on Submission Checklist" box ticked, OR (future) auto-
extracted by an agent that reads finished drafts for "Quadratic will
deliver X" commitments. Surfaces on the Submission Checklist tab
alongside the form-fill / certification items already extracted from
the compliance matrix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    pass


class SubmissionCommitment(Base, TimestampMixin):
    __tablename__ = "submission_commitments"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
    )

    # Free-form description of the commitment ("Network Architecture
    # diagram as labeled exhibit, including AWS GovCloud topology").
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Where this came from. Current values:
    #   "manual"            — user added directly from Submission Checklist
    #   "needs_human_apply" — auto-added when user resolved a placeholder
    #                         and ticked the "track this" checkbox
    # Future:
    #   "draft_extraction"  — agent-detected from a section's prose
    source: Mapped[str] = mapped_column(
        String(40),
        default="manual",
        nullable=False,
    )

    # Optional link back to the section that committed to it. SET NULL
    # on delete so a section regenerate doesn't cascade-delete the
    # commitment — the user's checklist tracking outlives any single
    # draft revision.
    source_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposal_sections.id", ondelete="SET NULL"),
        nullable=True,
    )

    # User toggle: have we gathered the artifact yet?
    obtained: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default="0",
    )
    # Free-form note (e.g., "diagram is in Lucidchart account, export
    # at submission time").
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
