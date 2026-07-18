from __future__ import annotations

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class CompanyProfileVersion(Base, TimestampMixin):
    """Versioned snapshots of company_profile.json.

    Source of truth is data/company_profile.json on disk; this table preserves
    historical snapshots so a proposal can be re-rendered against the profile
    that was active when it was drafted (e.g., capabilities or rate card had
    different values at the time).
    """

    __tablename__ = "company_profile_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_from: Mapped[str | None] = mapped_column(String(40), nullable=True)
    profile_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class InternalPricingRules(Base, TimestampMixin):
    """Quadratic's internal pricing rules — overhead, G&A, fee, subcontractor markup.

    Distinct from the published OLM rate card (which lives in company_profile.json)
    because internal pricing is not for public consumption and may change more
    frequently. The Cost Analysis Agent reads the active record.
    """

    __tablename__ = "internal_pricing_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_from: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rules_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
