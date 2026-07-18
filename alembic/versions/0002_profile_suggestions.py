"""profile_suggestions

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-25

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "profile_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "kb_document_id",
            sa.Integer(),
            sa.ForeignKey("knowledge_base_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("section", sa.String(80), nullable=False),
        sa.Column("match_key", sa.String(300), nullable=True),
        sa.Column("proposed_value_json", sa.JSON(), nullable=False),
        sa.Column("current_value_json", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_profile_suggestions_status", "profile_suggestions", ["status"])
    op.create_index("ix_profile_suggestions_kb_document_id", "profile_suggestions", ["kb_document_id"])


def downgrade() -> None:
    op.drop_index("ix_profile_suggestions_kb_document_id", table_name="profile_suggestions")
    op.drop_index("ix_profile_suggestions_status", table_name="profile_suggestions")
    op.drop_table("profile_suggestions")
