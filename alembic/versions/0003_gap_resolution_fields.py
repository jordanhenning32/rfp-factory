"""gap resolution fields

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("gap_analyses") as batch:
        batch.add_column(sa.Column("selected_mitigation_index", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("resolution_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("gap_analyses") as batch:
        batch.drop_column("resolution_notes")
        batch.drop_column("resolved")
        batch.drop_column("selected_mitigation_index")
