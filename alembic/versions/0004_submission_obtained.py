"""submission_obtained on compliance_matrix_items

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("compliance_matrix_items") as batch:
        batch.add_column(
            sa.Column(
                "submission_obtained",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("submission_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("compliance_matrix_items") as batch:
        batch.drop_column("submission_notes")
        batch.drop_column("submission_obtained")
