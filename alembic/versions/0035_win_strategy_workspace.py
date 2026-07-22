"""Win strategy workspace JSON columns.

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.add_column(sa.Column("evaluator_scorecard_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("win_themes_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("past_performance_matches_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("price_to_win_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("red_team_findings_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("graphics_tables_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("proposals") as batch:
        batch.drop_column("graphics_tables_json")
        batch.drop_column("red_team_findings_json")
        batch.drop_column("price_to_win_json")
        batch.drop_column("past_performance_matches_json")
        batch.drop_column("win_themes_json")
        batch.drop_column("evaluator_scorecard_json")
