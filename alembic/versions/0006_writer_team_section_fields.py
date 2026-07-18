"""writer team section fields

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("proposal_sections") as batch:
        batch.add_column(sa.Column("section_brief", sa.Text(), nullable=True))
        batch.add_column(sa.Column("page_limit", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("word_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("proposal_sections") as batch:
        batch.drop_column("word_limit")
        batch.drop_column("page_limit")
        batch.drop_column("section_brief")
