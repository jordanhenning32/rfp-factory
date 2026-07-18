"""MarketScan + comparable awards + competitors.

Output of the Cost Market Researcher agent (Gemini 3.5 Pro grounded).
ONE market_scans row per proposal — re-running the researcher replaces
the existing scan. Comparable awards and competitors are persisted as
relational detail so the UI can render them with citations and the
Cost Analyst can reason over individual rows rather than parsing JSON.

Citations are required: every comparable_award has a source_url, every
competitor has source_urls (list, JSON column). Reviewer-style fact-
checking can verify these against SAM.gov / USAspending.gov / FPDS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.pricing import PricingPackage


class MarketScan(Base, TimestampMixin):
    """Per-proposal market scan. Drives all three pricing scenarios."""

    __tablename__ = "market_scans"
    __table_args__ = (UniqueConstraint("proposal_id", name="uq_market_scans_proposal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
    )

    # Aggregate market band the agent inferred from comparable awards.
    # Nullable when the agent couldn't establish a band (too few comps).
    market_band_low_usd: Mapped[float | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    market_band_mid_usd: Mapped[float | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    market_band_high_usd: Mapped[float | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )

    # Free-text description of how the band was derived (e.g., "Median +/-
    # 1 SD across 7 comparable CMS MMIS awards 2023-2025"). Used in the
    # Cost Volume narrative to defend the agent's market position claim.
    methodology: Mapped[str | None] = mapped_column(Text, nullable=True)

    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- relationships ----
    comparable_awards: Mapped[list[MarketScanComparableAward]] = relationship(
        back_populates="market_scan",
        cascade="all, delete-orphan",
    )
    competitors: Mapped[list[MarketScanCompetitor]] = relationship(
        back_populates="market_scan",
        cascade="all, delete-orphan",
    )
    pricing_packages: Mapped[list[PricingPackage]] = relationship(
        back_populates="market_scan",
    )


class MarketScanComparableAward(Base, TimestampMixin):
    """One comparable federal award informing the market band."""

    __tablename__ = "market_scan_comparable_awards"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_scan_id: Mapped[int] = mapped_column(
        ForeignKey("market_scans.id", ondelete="CASCADE"),
        index=True,
    )

    award_title: Mapped[str] = mapped_column(Text, nullable=False)
    award_value_usd: Mapped[float | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    period_of_performance_months: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    awardee_name: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
    )
    customer_agency: Mapped[str | None] = mapped_column(
        String(160),
        nullable=True,
    )

    # Required — every comparable award must trace to a public source
    # (SAM.gov, USAspending.gov, FPDS, agency press release, etc.).
    # Citation-check pre-flight should verify the URL resolves.
    source_url: Mapped[str] = mapped_column(Text, nullable=False)

    # Agent's confidence (0.0-1.0) that this award is comparable to the
    # current bid. Used for ranking and for filtering low-confidence
    # rows out of the band calculation.
    relevance_score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Dual-pipeline provenance — JSON list[str] subset of
    # {"gemini", "claude"}. Empty on legacy single-provider rows. The
    # consolidator populates both fields; UI surfaces chips when
    # confirmed_by is non-empty.
    confirmed_by: Mapped[list] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    needs_review: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    market_scan: Mapped[MarketScan] = relationship(
        back_populates="comparable_awards",
    )


class MarketScanCompetitor(Base, TimestampMixin):
    """One firm the researcher believes will likely bid this RFP."""

    __tablename__ = "market_scan_competitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_scan_id: Mapped[int] = mapped_column(
        ForeignKey("market_scans.id", ondelete="CASCADE"),
        index=True,
    )

    competitor_name: Mapped[str] = mapped_column(String(160), nullable=False)

    # CompetitorBidLikelihood enum: high / medium / low.
    likelihood_to_bid: Mapped[str] = mapped_column(String(16), nullable=False)

    # Estimated competitor billing rate range for this work, derived
    # from public award data (award value ÷ PoP ÷ FTE estimate × 1950).
    # Often the most useful single number in the scan because it
    # bounds what we can charge.
    estimated_rate_low_usd: Mapped[float | None] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )
    estimated_rate_high_usd: Mapped[float | None] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )

    # How the agent computed the rate range — required for transparency.
    # E.g., "$1.3M award ÷ 12mo PoP ÷ 5 FTE × 1950 hrs = $111/hr blended".
    rate_estimation_basis: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # JSON list of source URLs (SAM.gov, USAspending, agency PR, etc.).
    # Supports multiple sources per competitor since rate inference
    # often combines award data with company financial reporting.
    source_urls: Mapped[list] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Dual-pipeline provenance (same shape as comparable_awards above).
    confirmed_by: Mapped[list] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    needs_review: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    market_scan: Mapped[MarketScan] = relationship(
        back_populates="competitors",
    )
