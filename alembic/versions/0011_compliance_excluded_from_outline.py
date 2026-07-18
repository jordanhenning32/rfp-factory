"""compliance_matrix_items.excluded_from_outline — manual user opt-out

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-27

User-driven flag on compliance items: when True, the Outline Agent
will not see this item and the Writer Team will not draft narrative
for it. Stacks on top of the automatic filter (mandatory_form /
submission_format / certification).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.add_column(
            sa.Column(
                "excluded_from_outline",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.drop_column("excluded_from_outline")
