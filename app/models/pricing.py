"""PricingPackage + per-FTE lines + CostReviewFinding.

PricingPackage is now keyed (proposal_id, scenario): three rows per
proposal — LOW (Competitive), MEDIUM (Target), HIGH (Protective). Same
labor estimate flows through all three; what varies is coverage,
margin, and contingency assumption per scenario_definitions in
data/internal_pricing_rules.json.

The Cost Analyst (GPT-5.5) writes labor judgment (which categories,
how many hours, which salary per scenario); deterministic Python
code applies the wrap_rate_formula to compute every dollar value. The
LLM never returns a computed total.

CostReviewFinding is the deferred adversarial reviewer's hook — already
defined here so the schema's stable when we later wire that agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import FindingSeverity
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.market_scan import MarketScan
    from app.models.proposal import Proposal


class PricingPackage(Base, TimestampMixin):
    """One row per (proposal, scenario). Output of the Cost Analyst."""

    __tablename__ = "pricing_packages"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "scenario",
            name="uq_pricing_packages_proposal_scenario",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        index=True,
    )

    # PricingScenario enum: LOW / MEDIUM / HIGH. Together with proposal_id
    # this is a unique key — re-running the analyst replaces the existing
    # row for the same scenario.
    scenario: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="MEDIUM",
    )

    # Back-reference to the market scan that informed this scenario.
    # Nullable because the analyst can run without market data (the
    # Market Researcher may have failed or been skipped), in which case
    # the scenario produces a cost-only view.
    market_scan_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_scans.id", ondelete="SET NULL"),
        nullable=True,
    )

    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- Aggregate cost values (computed by Python, not LLM) ----
    loaded_labor_cost: Mapped[float | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )

    # [{item, amount, justification}, ...] — Other Direct Costs (travel,
    # equipment, training). Stays as JSON for now; ODCs are bid-specific
    # and rarely uniform enough to warrant a relational table.
    odcs_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    subcontractor_costs: Mapped[float | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )

    # {ga_hourly_addon_usd, ga_total_usd, contingency_hours_usd,
    #  contingency_cost_usd, profit_pct, profit_usd, total_subtotal_cost_usd}
    indirect_costs_json: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
        nullable=False,
    )

    total_proposed_price: Mapped[float | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )

    # {revenue, cogs, gross_margin, gross_margin_pct, blended_hourly_rate,
    #  break_even_hours, sensitivity[]} — the formatted P&L summary the
    # UI renders on the Cost tab.
    pnl_projection_json: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
        nullable=False,
    )

    # Lifecycle phase breakdown — list of phase dicts with computed
    # per-phase costs for THIS scenario. Each phase: {name,
    # description, start_month, duration_months, labor_allocations[],
    # phase_loaded_cost_usd, phase_ga_usd, phase_contingency_usd,
    # phase_subtotal_cost_usd, phase_profit_usd, phase_price_usd}.
    # Nullable so packages produced before this column existed stay
    # readable; UI surfaces a "re-run analyst to populate" notice.
    phase_breakdown_json: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )

    # MarketPosition enum: below / in_band / above. The analyst's
    # commentary on where this scenario's price sits relative to the
    # market_scan band. Drives bid_recommendation and informs the
    # Cost Volume Writer's narrative pricing rationale.
    vs_market_position: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    # BidRecommendation enum: bid / walk_away / flag_for_review. Set per
    # scenario — typically LOW = walk_away if margin floor violated,
    # MEDIUM = bid (default target posture), HIGH = bid for high-risk
    # pursuits.
    bid_recommendation: Mapped[str | None] = mapped_column(
        String(24),
        nullable=True,
    )
    recommendation_rationale: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # ---- relationships ----
    proposal: Mapped[Proposal] = relationship(back_populates="pricing_packages")
    market_scan: Mapped[MarketScan | None] = relationship(
        back_populates="pricing_packages",
    )
    lines: Mapped[list[PricingPackageLine]] = relationship(
        back_populates="pricing_package",
        cascade="all, delete-orphan",
    )
    review_findings: Mapped[list[CostReviewFinding]] = relationship(
        back_populates="pricing_package",
        cascade="all, delete-orphan",
    )


class PricingPackageLine(Base, TimestampMixin):
    """One per-FTE row within a PricingPackage. Three sets per proposal
    (one per scenario) — same labor structure, different computed
    values because coverage and margin differ.
    """

    __tablename__ = "pricing_package_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    pricing_package_id: Mapped[int] = mapped_column(
        ForeignKey("pricing_packages.id", ondelete="CASCADE"),
        index=True,
    )

    # GSA OLM labor catalog category — must match a category in
    # data/internal_pricing_rules.json labor_catalog[].category.
    # The Cost Analyst picks this; downstream pricing math uses the
    # corresponding ceiling_hourly_rate as the cap on
    # proposed_billing_rate_usd.
    labor_category: Mapped[str] = mapped_column(String(80), nullable=False)

    # One of the documented wage_bands keys: "85k", "95k", "105k", ...,
    # "230k". The Cost Analyst chooses the band; Python looks up the
    # loaded_annual_cost_*_usd from the JSON rules file.
    wage_band: Mapped[str] = mapped_column(String(16), nullable=False)

    # "high" or "low" — drives which loaded_annual_cost variant is used.
    # Per scenario_definitions: LOW scenario → low coverage; MEDIUM and
    # HIGH scenarios → high coverage.
    coverage_level: Mapped[str] = mapped_column(String(8), nullable=False)

    hours: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    # Computed values — populated by deterministic Python from the
    # wrap_rate_formula and scenario_definitions. The LLM never sets
    # these directly.
    loaded_hourly_rate_usd: Mapped[float] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )
    loaded_cost_usd: Mapped[float] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    ga_allocation_usd: Mapped[float] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    proposed_billing_rate_usd: Mapped[float] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )
    billed_total_usd: Mapped[float] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    profit_per_hour_usd: Mapped[float] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )

    # Free-form rationale from the Cost Analyst (e.g., "Single PM
    # for 12-month PoP; mid-band hire fits a 3-yr-experience CO/CTR
    # interface role; Senior PM unnecessary for routine status
    # reporting"). Renders on the Cost tab next to the line.
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    pricing_package: Mapped[PricingPackage] = relationship(
        back_populates="lines",
    )


class CostReviewFinding(Base, TimestampMixin):
    """Independent Cost Reviewer's flags and alternative scenarios.

    DEFERRED — the adversarial Cost Reviewer agent is not built yet.
    Schema preserved here so future wiring doesn't require another
    migration. Findings poke holes in a PricingPackage: missed scope,
    unrealistic hours, wage-band misalignment, margin pressure vs
    market band, etc.
    """

    __tablename__ = "cost_review_findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    pricing_package_id: Mapped[int] = mapped_column(
        ForeignKey("pricing_packages.id", ondelete="CASCADE"),
    )

    finding_text: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[FindingSeverity] = mapped_column(
        String(16),
        nullable=False,
    )
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # [{label, total_price, rationale, margin_delta}, ...]
    alternative_scenarios_json: Mapped[list] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )

    # Agent's primary actionable fix for this finding. E.g.,
    # "Increase Security Consultant hours from 650 to 900 to cover
    # NIST SSP development effort." Nullable for back-compat with
    # rows persisted before the Cost Review v2 schema.
    recommended_change: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # User's response to the finding: "pending" (default — awaiting
    # review), "accepted" (user agrees), or "rejected" (user
    # dismisses). Cleared back to "pending" on a fresh reviewer run.
    user_action: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        server_default="pending",
        nullable=False,
    )

    # When the user edits the recommended_change, the edited text is
    # stored here (the original agent recommendation stays in
    # recommended_change for audit). When the user rejects with a
    # reason, the reason is stored here. Nullable.
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # True when user_action was set by the system (auto-accept of
    # CRITICAL/MAJOR consensus findings post-cost-review-run); False
    # when the user clicked the action themselves. The Cost Review
    # tab renders an "AUTO" chip on auto-actioned findings so the
    # user knows what to audit before drafting picks them up. Reset
    # to False whenever the user re-actions the row.
    auto_actioned: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
    )

    pricing_package: Mapped[PricingPackage] = relationship(
        back_populates="review_findings",
    )
