from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.proposal import Proposal


class ProposalTeamMember(Base, TimestampMixin):
    """One role on the proposed delivery team.

    User-curated (Phase 1A) — typed manually on the Team tab. Future
    phases add a Team Composer agent that pre-seeds and a Cost
    Analyst inversion that consumes this as input.

    person_kind drives display + downstream cost handling:
      "named" — a specific person, typically from
                company_profile.key_personnel
      "tbh"   — to-be-hired; allowed but flagged in narrative
      "sub"   — delivered by a teaming partner; assigned_person
                carries "Sub: <partner name>"

    phases_active_json holds the lifecycle phases this role works
    in (e.g. ["Phase 1", "Phase 2-3"] or numeric ids — kept loose
    for now; Cost Analyst inversion will tighten the format).
    """

    __tablename__ = "proposal_team_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
    )

    role_name: Mapped[str] = mapped_column(String(120), nullable=False)
    person_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="named",
        server_default="named",
    )
    assigned_person: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    labor_category: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )
    wage_band: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    time_allocation_pct: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    experience_years: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    bio_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    phases_active_json: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    display_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    proposal: Mapped[Proposal] = relationship(
        back_populates="team_members",
    )
