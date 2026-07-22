"""pricing line rate overrides

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pricing_package_lines") as batch:
        batch.add_column(
            sa.Column("loaded_hourly_override_usd", sa.Numeric(10, 2), nullable=True)
        )
        batch.add_column(
            sa.Column("billed_hourly_override_usd", sa.Numeric(10, 2), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("pricing_package_lines") as batch:
        batch.drop_column("billed_hourly_override_usd")
        batch.drop_column("loaded_hourly_override_usd")
