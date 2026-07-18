"""Post-submission outcome ledger.

One ProposalOutcome row per proposal (unique on `proposal_id`, CASCADE on
proposal delete). Captures what happened AFTER submission:

  - lifecycle (submitted_at / outcome / decided_at)
  - financial outcome (our proposed price, awarded price, awarded firm)
  - debrief metadata (received flag, narrative notes, per-factor scoring
    breakdown via `factor_scores_json`)

This table is the load-bearing input to
`app.services.lessons.format_reviewer_guidance`: when proposal_outcomes
has data, the reviewer-guidance hook joins ReviewerFinding ->
ProposalSection -> Proposal -> ProposalOutcome to compute per-category
WON-vs-LOST correlation patterns. Reviewer agents read the resulting
guidance block on every run and bias their findings accordingly.

`factor_scores_json` (optional, nullable) is a list with shape:
    [
      {"factor_id": str,
       "factor_name": str,
       "our_score": num | None,
       "winning_score": num | None,
       "max_score": num | None,
       "notes": str | None},
      ...
    ]
Mirrors the `proposals.evaluation_criteria_json` factors schema from
pipeline 1 so the UI can pre-populate scoring rows from the extracted
Section M factors. No validation is enforced at the ORM level; the
service in `app/services/proposal_outcomes.py` is the contract surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ProposalOutcomeStatus
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.proposal import Proposal


class ProposalOutcome(Base, TimestampMixin):
    """One outcome row per proposal. See module docstring."""

    __tablename__ = "proposal_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    # Lifecycle
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    outcome: Mapped[ProposalOutcomeStatus] = mapped_column(
        String(20),
        nullable=False,
        default=ProposalOutcomeStatus.PENDING,
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Financial outcome (when known)
    our_proposed_price_usd: Mapped[float | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    awarded_price_usd: Mapped[float | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    awarded_to: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # Scoring / debrief
    debrief_received: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    our_total_score: Mapped[float | None] = mapped_column(
        Numeric(6, 2),
        nullable=True,
    )
    winning_total_score: Mapped[float | None] = mapped_column(
        Numeric(6, 2),
        nullable=True,
    )
    debrief_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-factor scoring breakdown — JSON list mirroring evaluation_criteria
    # factors. See module docstring for the row shape.
    factor_scores_json: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    # Relationships
    proposal: Mapped[Proposal] = relationship(back_populates="outcome")
