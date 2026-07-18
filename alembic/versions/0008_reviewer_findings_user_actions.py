"""reviewer findings user actions

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("reviewer_findings") as batch:
        batch.add_column(
            sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("dismissed_reason", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("reviewer_findings") as batch:
        batch.drop_column("accepted_at")
        batch.drop_column("dismissed_reason")
        batch.drop_column("dismissed_at")
