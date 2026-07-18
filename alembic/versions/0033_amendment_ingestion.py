"""amendment + Q&A ingestion — six new columns + amendment_runs table

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-19

Adds the schema underpinnings for the Amendment + Q&A ingestion track:

  rfp_package_documents
    + document_role: String(16) NULL — 'original' | 'amendment' | 'qa_response';
      NULL on pre-existing rows is treated as 'original' by absence.
    + sequence_number: Integer NULL — buyer-assigned amendment number;
      always NULL on original + qa_response rows.

  compliance_matrix_items
    + amendment_origin: String(64) NULL — filename of the amendment doc
      that added or modified this row; NULL for original-RFP items.
    + status: String(16) NOT NULL DEFAULT 'active' — 'active' | 'superseded'
      | 'removed'. Existing rows pick up 'active' via server_default.
    + superseded_by_id: Integer NULL → compliance_matrix_items.id
      ON DELETE SET NULL. Self-FK pointing forward to the row that
      replaced this one.

  proposal_sections
    + compliance_drift_pending: Boolean NOT NULL DEFAULT 0 — flipped to
      True by the amendment apply when this section's
      `compliance_items_addressed_json` overlaps the modified/removed
      requirement set. Cleared by a successful writer regenerate.

  amendment_runs (new table)
    - id: Integer PK
    - proposal_id: Integer FK -> proposals.id ON DELETE CASCADE, indexed
    - document_id: Integer FK -> rfp_package_documents.id ON DELETE CASCADE
    - started_at / completed_at: DateTime(tz=True) NULL
    - status: String(16) NOT NULL DEFAULT 'running'
        ('running' | 'completed' | 'failed')
    - report_json: Text NULL — JSON-serialized AmendmentApplyReport
    - error_text: Text NULL — first 2000 chars of the exception on failure
    - created_at / updated_at: DateTime(tz=True) NOT NULL, server_default=now()

The audit table mirrors `agent_runs`' shape so the amendment pipeline has
its own first-class history. `AmendmentRun.proposal_id` is indexed because
the Amendments & Q&A tab queries the latest completed run per proposal on
every render.

Downgrade drops everything in reverse order (table first then columns) so
0032 → 0033 → 0032 round-trips cleanly on SQLite.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── rfp_package_documents: document_role + sequence_number ──────────
    with op.batch_alter_table("rfp_package_documents") as batch_op:
        batch_op.add_column(
            sa.Column("document_role", sa.String(length=16), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sequence_number", sa.Integer(), nullable=True)
        )

    # ── compliance_matrix_items: amendment_origin + status + self-FK ────
    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.add_column(
            sa.Column("amendment_origin", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.add_column(
            sa.Column(
                "superseded_by_id",
                sa.Integer(),
                sa.ForeignKey(
                    "compliance_matrix_items.id",
                    ondelete="SET NULL",
                    name="fk_compliance_matrix_items_superseded_by_id_compliance_matrix_items",
                ),
                nullable=True,
            )
        )

    # ── proposal_sections: compliance_drift_pending ─────────────────────
    with op.batch_alter_table("proposal_sections") as batch_op:
        batch_op.add_column(
            sa.Column(
                "compliance_drift_pending",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )

    # ── amendment_runs (new) ────────────────────────────────────────────
    op.create_table(
        "amendment_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("rfp_package_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="running",
        ),
        sa.Column("report_json", sa.Text(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_amendment_runs_proposal_id",
        "amendment_runs",
        ["proposal_id"],
    )


def downgrade() -> None:
    # Reverse order: drop table first, then columns.
    op.drop_index("ix_amendment_runs_proposal_id", table_name="amendment_runs")
    op.drop_table("amendment_runs")

    with op.batch_alter_table("proposal_sections") as batch_op:
        batch_op.drop_column("compliance_drift_pending")

    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.drop_column("superseded_by_id")
        batch_op.drop_column("status")
        batch_op.drop_column("amendment_origin")

    with op.batch_alter_table("rfp_package_documents") as batch_op:
        batch_op.drop_column("sequence_number")
        batch_op.drop_column("document_role")
