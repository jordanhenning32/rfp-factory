"""compliance_matrix_items — excluded_from_outline flag

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-28

Adds a boolean `excluded_from_outline` column to compliance_matrix_items,
default False. Set from the Outline tab's unassigned-items recovery UI
when the user picks "Mark N/A — not a narrative item" — excludes the
item from the outline-unassigned warning so they aren't pestered again.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.add_column(
            sa.Column(
                "excluded_from_outline",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.drop_column("excluded_from_outline")
