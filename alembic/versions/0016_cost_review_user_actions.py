"""cost_review_findings — recommended_change + user action tracking

Revision ID: 0016
Revises: 0015
Create Date: 2026-04-28

Three additive columns on cost_review_findings so the Cost Review
tab can surface actionable fixes and let the user accept / reject /
edit each finding:

  recommended_change — the agent's primary actionable fix
                       (e.g., "Increase Security Consultant hours
                       from 650 to 900"). Nullable for back-compat
                       with rows persisted before this column.

  user_action — "pending" / "accepted" / "rejected". Default pending
                so existing rows show as awaiting review until the
                user clicks Accept or Reject. Cleared back to
                pending on a fresh reviewer run.

  user_note — user's edited recommendation (when they edit the
              agent's recommended_change) OR rejection reason
              (when they reject with an explanation). Nullable.

All three are additive — no destructive changes — so existing data
stays readable.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("cost_review_findings") as batch_op:
        batch_op.add_column(
            sa.Column("recommended_change", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "user_action", sa.String(length=16),
                nullable=False, server_default="pending",
            )
        )
        batch_op.add_column(
            sa.Column("user_note", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("cost_review_findings") as batch_op:
        batch_op.drop_column("user_note")
        batch_op.drop_column("user_action")
        batch_op.drop_column("recommended_change")
