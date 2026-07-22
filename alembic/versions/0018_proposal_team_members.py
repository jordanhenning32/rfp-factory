"""proposal_team_members table — user-approved team roster

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-28

Front-loads team composition so the Writer Team has named personnel
+ time allocations + labor categories baked into its cached prefix
when drafting. Eliminates the "% time PM/CTO" and personnel-
detail [NEEDS_HUMAN] placeholder buckets the writer was emitting
for lack of upstream data.

One row per role on the proposed delivery team:
  role_name           — what they do (PM, Solution Architect, BA II, ...)
  person_kind         — named / tbh / sub  (drives display + cost-analyst input)
  assigned_person     — "Alex Rivera" / "TBH" / "Sub: Example Partner"
  labor_category      — GSA OLM mapping (used by Cost Analyst later)
  wage_band           — "$170K" / "$95K" — same vocab as internal_pricing_rules
  time_allocation_pct — 0-100, share of full-time over PoP
  experience_years    — for evaluator-facing prose
  bio_summary         — 1-2 sentences of qualifications, written by user/agent
  phases_active_json  — list of phase identifiers this role participates in
  display_order       — render order on Team tab

User flow: outline approval → Team tab → manual entry (Phase 1A) →
"Approve Team" → drafting. Future Phase 1B adds a Team Composer
agent that pre-seeds rows; Phase 1C inverts the Cost Analyst to
consume this roster as input.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proposal_team_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proposal_id", sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("role_name", sa.String(length=120), nullable=False),
        sa.Column(
            "person_kind", sa.String(length=16),
            nullable=False, server_default="named",
        ),
        sa.Column("assigned_person", sa.String(length=200), nullable=True),
        sa.Column("labor_category", sa.String(length=120), nullable=True),
        sa.Column("wage_band", sa.String(length=40), nullable=True),
        sa.Column(
            "time_allocation_pct", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column("experience_years", sa.Integer(), nullable=True),
        sa.Column("bio_summary", sa.Text(), nullable=True),
        sa.Column(
            "phases_active_json", sa.JSON(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "display_order", sa.Integer(),
            nullable=False, server_default="0",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )
    # Track whether the user has reviewed/approved the team. Lives on
    # proposals so it can drive UI gates (e.g. "Approve Team before
    # drafting"). Nullable timestamp; null = not yet approved.
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "team_approved_at", sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.drop_column("team_approved_at")
    op.drop_table("proposal_team_members")
