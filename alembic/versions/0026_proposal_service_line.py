"""proposals.service_line — service-line tag (it_services / payment_systems / future)

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-29

Adds a nullable `service_line` column to the proposals table so the
system can branch its behavior per proposal: IT services bids run the
labor-catalog cost flow (Cost Analyst → Cost Reviewer → labor totals),
payment-systems bids run a fee-schedule flow (Cost Writer pulls from
data/pricing/payment_systems.json + _payment_systems_context.json).
NULL means "no explicit choice" — read sites treat NULL as
'it_services' so legacy proposals see no behavior change. The column
is intentionally a free-form string (not an enum) so adding new
service lines later is a registry update, not a migration.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column("service_line", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("proposals", "service_line")
