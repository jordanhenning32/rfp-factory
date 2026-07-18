"""market_scan detail rows — dual-pipeline provenance

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-28

Adds provenance fields to market_scan_comparable_awards and
market_scan_competitors so the dual-pipeline Cost Market Researcher
(Gemini Pass A + Claude+web Pass B + consolidator) can persist
which provider(s) surfaced each row.

  confirmed_by — JSON list[str], subset of {"gemini", "claude"}.
                 Empty list on legacy single-provider rows; the UI
                 treats empty as "no provenance to show" and renders
                 nothing.
  needs_review — bool, default False. Set by the consolidator when a
                 single-provider row is sub-HIGH confidence (or for
                 competitors, when only one provider had the firm).
                 UI surfaces an amber Verify chip on these rows.

Both columns are additive; existing rows get sensible defaults.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in (
        "market_scan_comparable_awards",
        "market_scan_competitors",
    ):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "confirmed_by",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[]'"),
                )
            )
            batch_op.add_column(
                sa.Column(
                    "needs_review",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )


def downgrade() -> None:
    for table in (
        "market_scan_comparable_awards",
        "market_scan_competitors",
    ):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("needs_review")
            batch_op.drop_column("confirmed_by")
