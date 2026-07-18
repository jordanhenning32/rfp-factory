"""writer team cost-deferral flag

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("proposal_sections") as batch:
        batch.add_column(
            sa.Column(
                "requires_cost_analysis",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("proposal_sections") as batch:
        batch.drop_column("requires_cost_analysis")
