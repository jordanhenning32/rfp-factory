"""Index gap_analyses.proposal_id

Revision ID: 0030
Revises: 0029
Create Date: 2026-04-30

GapAnalysis.proposal_id was the only proposal-scoped FK in the
schema without `index=True`. Compared to the seven indexed siblings
(ComplianceMatrixItem, AgentRun, MarketScan, PolishEdit, PricingPackage,
ProposalSection, SubmissionCommitment, TeamMember), this column gets
hit by hot-path queries on every page navigation and 5s timer tick:

  - _render_next_step_banner counts gap rows for the deal-breaker chip
  - _compute_tab_badges counts unaddressed gaps for the Gaps tab badge
  - proposal_review's gap snapshot JOIN at pages.py:1183

Without the index every one of those is a full-table scan over
gap_analyses. With it, they're a single index probe. SQLite's
single-user scale makes the absolute win small today (sub-ms per
query), but the symmetry is the right call regardless and the win
grows linearly as gap_analyses accumulates rows across proposals.

Naming convention `ix_<table>_<column>` mirrors the index names
created by 0001_initial_schema.py.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_gap_analyses_proposal_id",
        "gap_analyses",
        ["proposal_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_gap_analyses_proposal_id", table_name="gap_analyses")
