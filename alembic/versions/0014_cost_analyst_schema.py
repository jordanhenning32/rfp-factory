"""cost analyst schema — H/M/L pricing scenarios + market scan tables

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-27

Three coordinated changes for the Cost Analyst pipeline (Market
Researcher + Cost Analyst + Cost Volume Writer):

1. Modify pricing_packages — repurpose from one-row-per-proposal to
   one-row-per-scenario. Adds `scenario` (LOW/MEDIUM/HIGH), unique on
   (proposal_id, scenario). Drops fte_breakdown_json and market_comps_json
   which are replaced by relational tables. Adds market_scan_id and
   agent_run_id back-references.

2. Create market_scans + market_scan_comparable_awards +
   market_scan_competitors. The Market Researcher (Gemini 3.5 Pro
   grounded) writes ONE market_scans row per proposal that drives all
   three pricing scenarios. Comparable awards and competitors are
   relational detail tables with source URLs for citation.

3. Create pricing_package_lines — per-FTE detail per scenario. Same
   labor structure (category + hours + wage_band) duplicates across
   the three scenarios because COMPUTED values (loaded_hourly_rate,
   billed_rate, profit) differ per scenario based on coverage and
   margin assumptions.

Empty-table assumption: pricing_packages has never been written to
(agents don't exist yet), so dropping the JSON columns is safe.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- Step 1: market_scans (must exist before pricing_packages FK to it) ----
    op.create_table(
        "market_scans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proposal_id", sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("market_band_low_usd", sa.Numeric(14, 2), nullable=True),
        sa.Column("market_band_mid_usd", sa.Numeric(14, 2), nullable=True),
        sa.Column("market_band_high_usd", sa.Numeric(14, 2), nullable=True),
        sa.Column("methodology", sa.Text(), nullable=True),
        sa.Column(
            "agent_run_id", sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("proposal_id", name="uq_market_scans_proposal"),
    )
    op.create_index(
        "ix_market_scans_proposal_id", "market_scans", ["proposal_id"],
    )

    # ---- Step 2: comparable awards detail ----
    op.create_table(
        "market_scan_comparable_awards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "market_scan_id", sa.Integer(),
            sa.ForeignKey("market_scans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("award_title", sa.Text(), nullable=False),
        sa.Column("award_value_usd", sa.Numeric(14, 2), nullable=True),
        sa.Column("period_of_performance_months", sa.Integer(), nullable=True),
        sa.Column("awardee_name", sa.String(length=160), nullable=True),
        sa.Column("customer_agency", sa.String(length=160), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_market_scan_comparable_awards_market_scan_id",
        "market_scan_comparable_awards", ["market_scan_id"],
    )

    # ---- Step 3: competitors detail ----
    op.create_table(
        "market_scan_competitors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "market_scan_id", sa.Integer(),
            sa.ForeignKey("market_scans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("competitor_name", sa.String(length=160), nullable=False),
        # high / medium / low — see CompetitorBidLikelihood enum
        sa.Column("likelihood_to_bid", sa.String(length=16), nullable=False),
        sa.Column("estimated_rate_low_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("estimated_rate_high_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("rate_estimation_basis", sa.Text(), nullable=True),
        sa.Column(
            "source_urls", sa.JSON(),
            nullable=False, server_default=sa.text("'[]'"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_market_scan_competitors_market_scan_id",
        "market_scan_competitors", ["market_scan_id"],
    )

    # ---- Step 4: modify pricing_packages — repurpose for per-scenario rows ----
    with op.batch_alter_table("pricing_packages") as batch_op:
        batch_op.drop_column("fte_breakdown_json")
        batch_op.drop_column("market_comps_json")
        batch_op.add_column(
            sa.Column(
                # PricingScenario enum: LOW / MEDIUM / HIGH
                "scenario", sa.String(length=16),
                nullable=False, server_default="MEDIUM",
            )
        )
        batch_op.add_column(
            sa.Column(
                "market_scan_id", sa.Integer(),
                sa.ForeignKey("market_scans.id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "agent_run_id", sa.Integer(),
                sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        # MarketPosition enum: below / in_band / above
        batch_op.add_column(
            sa.Column("vs_market_position", sa.String(length=16), nullable=True)
        )
        # BidRecommendation enum: bid / walk_away / flag_for_review
        batch_op.add_column(
            sa.Column("bid_recommendation", sa.String(length=24), nullable=True)
        )
        batch_op.add_column(
            sa.Column("recommendation_rationale", sa.Text(), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_pricing_packages_proposal_scenario",
            ["proposal_id", "scenario"],
        )
        batch_op.create_index(
            "ix_pricing_packages_proposal_id", ["proposal_id"],
        )

    # ---- Step 5: pricing_package_lines — per-FTE detail per scenario ----
    op.create_table(
        "pricing_package_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pricing_package_id", sa.Integer(),
            sa.ForeignKey("pricing_packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # GSA OLM labor catalog category (e.g., "Project Manager II",
        # "Software Engineer III"). Maps to data/internal_pricing_rules.json
        # labor_catalog[].category.
        sa.Column("labor_category", sa.String(length=80), nullable=False),
        # One of the wage_bands keys: "85k", "95k", ..., "230k".
        sa.Column("wage_band", sa.String(length=16), nullable=False),
        # "high" or "low" — drives which loaded_annual_cost_*_usd is used.
        sa.Column("coverage_level", sa.String(length=8), nullable=False),
        sa.Column("hours", sa.Numeric(10, 2), nullable=False),
        # Computed by Python from the wrap_rate_formula, NOT from the LLM.
        sa.Column("loaded_hourly_rate_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("loaded_cost_usd", sa.Numeric(14, 2), nullable=False),
        sa.Column("ga_allocation_usd", sa.Numeric(14, 2), nullable=False),
        sa.Column("proposed_billing_rate_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("billed_total_usd", sa.Numeric(14, 2), nullable=False),
        sa.Column("profit_per_hour_usd", sa.Numeric(10, 2), nullable=False),
        # LLM-provided commentary on why this category / hours / band was chosen.
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_pricing_package_lines_pricing_package_id",
        "pricing_package_lines", ["pricing_package_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pricing_package_lines_pricing_package_id",
        table_name="pricing_package_lines",
    )
    op.drop_table("pricing_package_lines")

    with op.batch_alter_table("pricing_packages") as batch_op:
        batch_op.drop_index("ix_pricing_packages_proposal_id")
        batch_op.drop_constraint(
            "uq_pricing_packages_proposal_scenario", type_="unique",
        )
        batch_op.drop_column("recommendation_rationale")
        batch_op.drop_column("bid_recommendation")
        batch_op.drop_column("vs_market_position")
        batch_op.drop_column("agent_run_id")
        batch_op.drop_column("market_scan_id")
        batch_op.drop_column("scenario")
        batch_op.add_column(
            sa.Column(
                "market_comps_json", sa.JSON(),
                nullable=False, server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "fte_breakdown_json", sa.JSON(),
                nullable=False, server_default=sa.text("'[]'"),
            )
        )

    op.drop_index(
        "ix_market_scan_competitors_market_scan_id",
        table_name="market_scan_competitors",
    )
    op.drop_table("market_scan_competitors")

    op.drop_index(
        "ix_market_scan_comparable_awards_market_scan_id",
        table_name="market_scan_comparable_awards",
    )
    op.drop_table("market_scan_comparable_awards")

    op.drop_index("ix_market_scans_proposal_id", table_name="market_scans")
    op.drop_table("market_scans")
