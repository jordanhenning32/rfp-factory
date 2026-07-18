"""proposals.cots_orientation flag for COTS-leaning RFPs

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-27

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite doesn't support adding NOT NULL columns without a default,
    # so we provide a server_default of 0 (False). Existing rows pick
    # this up; the intake job sets it correctly on the next run.
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cots_orientation",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.drop_column("cots_orientation")
