"""polish_edits table — audit log of Final Polish auto-applied changes

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-29

One row per cross-section consistency edit the Final Polish Applier
auto-applied. Lets the Final Polish tab surface a human-readable
"what changed" list grouped by polish run, instead of the user having
to diff section revisions manually to see what got polished.

Schema:
  proposal_id           — FK proposals (CASCADE on proposal delete)
  proposal_section_id   — FK proposal_sections (CASCADE on section
                          delete; the edit is meaningless without
                          its section context)
  section_id_label      — cached SEC-### label so the row remains
                          readable in joins/exports if section_id
                          ever changes
  issue_type            — enum-style string from the detector
                          (numerical_drift, terminology_drift,
                           voice_drift, commitment_conflict,
                           redundant_repetition, naming_inconsistency)
  severity              — CRITICAL / MAJOR / MINOR
  edit_summary          — one-sentence human-readable change description
                          from the applier ('Aligned FTE count from
                          4 to 3.5 to match SEC-007')
  rationale             — detector's rationale (why it mattered)
  problematic_text      — verbatim snippet that was replaced (may be
                          long; clamped via column type=Text)
  suggested_fix         — verbatim text it was replaced with
  applied_at            — when this specific edit landed
  applied_in_run_at     — start-of-run timestamp shared by every edit
                          in the same polish wave (for UI grouping)
  cost_usd              — applier-call cost for this single edit, so
                          the UI can roll up "cost of this run" cleanly
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "polish_edits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proposal_id", sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column(
            "proposal_section_id", sa.Integer(),
            sa.ForeignKey("proposal_sections.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("section_id_label", sa.String(length=64), nullable=False),
        sa.Column("issue_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("edit_summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("problematic_text", sa.Text(), nullable=True),
        sa.Column("suggested_fix", sa.Text(), nullable=True),
        sa.Column(
            "applied_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "applied_in_run_at",
            sa.DateTime(timezone=True), nullable=False,
            index=True,
        ),
        sa.Column(
            "cost_usd", sa.Float(), nullable=False,
            server_default=sa.text("0.0"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("polish_edits")
