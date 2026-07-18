"""proposals — strategic framing columns

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-28

Adds two nullable string columns capturing the user's "framing"
answers — the strategic posture that shapes every gap response and
section draft:

  teaming_framing — NULL | "open" | "self_perform_only"
  build_framing   — NULL | "custom_build_first" | "self_perform_first"

NULL on either column means "decide per gap" — the writer behaves as
today (no framing block injected into its cached prefix). Both are
additive nullable; existing proposals are unaffected.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.add_column(
            sa.Column("teaming_framing", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("build_framing", sa.String(length=32), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.drop_column("build_framing")
        batch_op.drop_column("teaming_framing")
