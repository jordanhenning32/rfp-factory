"""proposal_outcomes ledger — win/loss outcome rows feed the reviewer hook

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-19

Adds the `proposal_outcomes` table that captures post-submission outcome
data per proposal. One row per proposal, enforced by unique=True on
`proposal_id`; the FK `fk_proposal_outcomes_proposal_id_proposals` cascades
on proposal delete so the parent-row lifecycle drives the ledger.

Columns (13 data columns + 2 TimestampMixin columns):

  id                      Integer PK
  proposal_id             Integer FK -> proposals.id ON DELETE CASCADE,
                          UNIQUE NOT NULL
  submitted_at            DateTime(tz=True) NULL
  outcome                 String(20) NOT NULL DEFAULT 'pending'
                          (ProposalOutcomeStatus stored as string —
                          matches the existing enum-as-String convention)
  decided_at              DateTime(tz=True) NULL
  our_proposed_price_usd  Numeric(14, 2) NULL
  awarded_price_usd       Numeric(14, 2) NULL
  awarded_to              String(255) NULL
  debrief_received        Boolean NOT NULL DEFAULT 0
  our_total_score         Numeric(6, 2) NULL
  winning_total_score     Numeric(6, 2) NULL
  debrief_notes           Text NULL
  factor_scores_json      JSON NULL  -- per-factor score breakdown, list
                          of {factor_id, factor_name, our_score,
                          winning_score, max_score, notes}
  created_at / updated_at DateTime(tz=True) NOT NULL DEFAULT now()

The UNIQUE on proposal_id auto-creates the unique constraint + index,
so no separate `create_index` call is needed; the FK's name matches the
project's `NAMING_CONVENTION` from `app/db/base.py` so future
batch_alter_table reflections find it cleanly.

Downgrade drops the table; the FK + UNIQUE constraint drop with it.
Round-trips cleanly: 0033 -> 0034 -> 0033 -> 0034.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "proposal_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey(
                "proposals.id",
                ondelete="CASCADE",
                name="fk_proposal_outcomes_proposal_id_proposals",
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outcome",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("our_proposed_price_usd", sa.Numeric(14, 2), nullable=True),
        sa.Column("awarded_price_usd", sa.Numeric(14, 2), nullable=True),
        sa.Column("awarded_to", sa.String(length=255), nullable=True),
        sa.Column(
            "debrief_received",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("our_total_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("winning_total_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("debrief_notes", sa.Text(), nullable=True),
        sa.Column("factor_scores_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("proposal_outcomes")
