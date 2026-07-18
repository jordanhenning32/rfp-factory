"""PolishEdit — audit-log row for one Final Polish auto-applied edit.

Surfaces on the Final Polish tab so the user can see what changed
without diffing section revisions manually. Persisted in
`polish_edits` (migration 0024). One row per applier success;
detector-only runs (zero issues found) leave no rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.proposal import Proposal
    from app.models.section import ProposalSection


class PolishEdit(Base, TimestampMixin):
    """One Final Polish edit applied to one section in one polish run."""

    __tablename__ = "polish_edits"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proposal_section_id: Mapped[int] = mapped_column(
        ForeignKey("proposal_sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Cached SEC-### label so the row stays human-readable in lists
    # / exports even if the section table changes shape.
    section_id_label: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)

    edit_summary: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    problematic_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    suggested_fix: Mapped[str | None] = mapped_column(Text, nullable=True)

    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # Identical for every edit in the same polish-run wave so the UI
    # can `GROUP BY applied_in_run_at` to render "Run @ 15:21 — 6
    # edits" sections cleanly.
    applied_in_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    cost_usd: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
    )

    proposal: Mapped[Proposal] = relationship()
    section: Mapped[ProposalSection] = relationship()
