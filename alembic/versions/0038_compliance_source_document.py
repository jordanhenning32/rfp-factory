"""stable source document provenance for compliance items

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.add_column(
            sa.Column("source_document_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_compliance_matrix_items_source_document_id",
            "rfp_package_documents",
            ["source_document_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_compliance_matrix_items_source_document_id",
            ["source_document_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("compliance_matrix_items") as batch_op:
        batch_op.drop_index("ix_compliance_matrix_items_source_document_id")
        batch_op.drop_constraint(
            "fk_compliance_matrix_items_source_document_id",
            type_="foreignkey",
        )
        batch_op.drop_column("source_document_id")
