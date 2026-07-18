"""learned_rules table for accept/dismiss-driven guidance memory

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-26

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "learned_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("rule_text", sa.Text(), nullable=False),
        sa.Column(
            "source_finding_id",
            sa.Integer(),
            sa.ForeignKey(
                "reviewer_findings.id",
                name="fk_learned_rules_source_finding_id_reviewer_findings",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column("source_action", sa.String(length=16), nullable=True),
        sa.Column("source_category", sa.String(length=40), nullable=True),
        sa.Column("source_severity", sa.String(length=16), nullable=True),
        sa.Column("source_reviewer", sa.String(length=8), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "hits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
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
        "ix_learned_rules_kind_status",
        "learned_rules",
        ["kind", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_learned_rules_kind_status", table_name="learned_rules")
    op.drop_table("learned_rules")
