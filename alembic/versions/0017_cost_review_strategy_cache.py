"""proposals — cache last cost-review strategy

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-28

Caches the last "Generate Strategy" Sonnet output so users can
re-open it without re-paying ~$0.02-0.08 per call. Three additive
columns on proposals:

  cost_review_strategy_markdown — Sonnet's last synthesized plan
                                   (markdown). Nullable until the
                                   user clicks Generate Strategy.

  cost_review_strategy_generated_at — when the cached markdown was
                                       produced. Surfaces "Generated
                                       X minutes ago" in the dialog.

  cost_review_strategy_findings_count — count of active findings at
                                         gen time. Lets the UI warn
                                         "based on N findings, current
                                         count is M" so the user knows
                                         when to regenerate.

All three are additive nullable — no destructive changes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cost_review_strategy_markdown",
                sa.Text(), nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "cost_review_strategy_generated_at",
                sa.DateTime(timezone=True), nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "cost_review_strategy_findings_count",
                sa.Integer(), nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.drop_column("cost_review_strategy_findings_count")
        batch_op.drop_column("cost_review_strategy_generated_at")
        batch_op.drop_column("cost_review_strategy_markdown")
