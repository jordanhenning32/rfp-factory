"""drop compliance.excluded_from_outline; add proposal_sections.excluded_from_draft

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-27

Two coordinated changes:
  1. Drop compliance_matrix_items.excluded_from_outline (added in 0011).
     The compliance-level toggle was reverted; user wanted the toggle
     on the OUTLINE tab instead, per-section.
  2. Add proposal_sections.excluded_from_draft. When True, the Writer
     Team skips this section entirely (no draft generated). Mirrors the
     existing requires_cost_analysis skip but for non-cost reasons —
     primarily wrapper sections the Outline Agent created for forms /
     attachments / instructions that don't need narrative.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.drop_column("excluded_from_outline")
    with op.batch_alter_table("proposal_sections") as batch_op:
        batch_op.add_column(
            sa.Column(
                "excluded_from_draft",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("proposal_sections") as batch_op:
        batch_op.drop_column("excluded_from_draft")
    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.add_column(
            sa.Column(
                "excluded_from_outline",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
