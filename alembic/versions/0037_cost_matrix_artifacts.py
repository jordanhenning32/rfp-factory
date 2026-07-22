"""buyer-authored cost matrix artifacts

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cost_matrix_artifacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="detected", nullable=False),
        sa.Column("template_sha256", sa.String(length=64), nullable=False),
        sa.Column("analysis_version", sa.String(length=24), nullable=False),
        sa.Column("analysis_json", sa.JSON(), nullable=False),
        sa.Column("mapping_json", sa.JSON(), nullable=False),
        sa.Column("human_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["rfp_package_documents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_document_id",
            name="uq_cost_matrix_artifacts_source_document_id",
        ),
    )
    op.create_index(
        op.f("ix_cost_matrix_artifacts_proposal_id"),
        "cost_matrix_artifacts",
        ["proposal_id"],
        unique=False,
    )
    op.create_table(
        "cost_matrix_outputs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("pricing_scenario", sa.String(length=32), nullable=True),
        sa.Column("pricing_basis_sha256", sa.String(length=64), nullable=False),
        sa.Column("generation_provenance_json", sa.JSON(), nullable=False),
        sa.Column("mapping_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("output_filename", sa.String(length=500), nullable=False),
        sa.Column("output_storage_path", sa.String(length=1000), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=False),
        sa.Column("validation_json", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["cost_matrix_artifacts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_id",
            "version",
            name="uq_cost_matrix_outputs_artifact_version",
        ),
    )
    op.create_index(
        op.f("ix_cost_matrix_outputs_artifact_id"),
        "cost_matrix_outputs",
        ["artifact_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_cost_matrix_outputs_artifact_id"),
        table_name="cost_matrix_outputs",
    )
    op.drop_table("cost_matrix_outputs")
    op.drop_index(
        op.f("ix_cost_matrix_artifacts_proposal_id"),
        table_name="cost_matrix_artifacts",
    )
    op.drop_table("cost_matrix_artifacts")
