"""pricing_packages.phase_breakdown_json — lifecycle phase cost allocation

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-27

Adds a single JSON column to pricing_packages so each scenario can
hold its own per-phase cost breakdown. Phase definitions (name,
description, start_month, duration_months, labor_allocations) are
shared across scenarios — the LLM produces them once — but the
COMPUTED phase costs differ per scenario because coverage / margin /
contingency vary. So one column per scenario row, populated by the
math layer.

Empty/null is acceptable — pricing packages produced before this
column was added stay readable and the UI handles missing data.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("pricing_packages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "phase_breakdown_json", sa.JSON(),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("pricing_packages") as batch_op:
        batch_op.drop_column("phase_breakdown_json")
