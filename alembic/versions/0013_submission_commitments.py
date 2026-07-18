"""submission_commitments — user-tracked deliverable artifacts

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-27

When a writer commits to delivering an artifact (a diagram, a labeled
exhibit, a plan), the user can flag that commitment for tracking on
the Submission Checklist. The existing checklist pulls from
compliance_matrix_items where requirement_type=mandatory_form or
category=certification — those are RFP-level required submissions.
This new table captures DRAFT-level commitments the proposal volunteers
during writing that the user wants to gather + attach by submission day.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "submission_commitments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proposal_id", sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "source", sa.String(length=40),
            nullable=False, server_default="manual",
        ),
        sa.Column(
            "source_section_id", sa.Integer(),
            sa.ForeignKey("proposal_sections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "obtained", sa.Boolean(),
            nullable=False, server_default=sa.text("0"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("submission_commitments")
