"""selected_partner_name on gap_analyses

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("gap_analyses") as batch:
        batch.add_column(sa.Column("selected_partner_name", sa.String(300), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("gap_analyses") as batch:
        batch.drop_column("selected_partner_name")
