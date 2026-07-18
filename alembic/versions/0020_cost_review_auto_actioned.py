"""cost_review_findings.auto_actioned — system-vs-user provenance

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-28

Adds a single boolean column to cost_review_findings tracking
whether the user_action was set by the system (auto-accept of
CRITICAL/MAJOR consensus findings) vs. by the user clicking
Accept/Reject/Edit. Lets the Cost Review tab render an "AUTO"
chip on auto-actioned findings so the user knows what to audit
before drafting picks them up.

When the user re-actions an auto-accepted finding (clicking any
of Accept/Reject/Edit), the value flips to False — they've
reviewed the recommendation and confirmed (or overridden) it.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("cost_review_findings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_actioned", sa.Boolean(),
                nullable=False, server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("cost_review_findings") as batch_op:
        batch_op.drop_column("auto_actioned")
