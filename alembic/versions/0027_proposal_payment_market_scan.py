"""proposals.payment_market_scan_json — persisted output of the payment-systems Market Researcher

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-29

When service_line=payment_systems, the Cost Market Researcher (Gemini
grounded + Haiku structuring) produces a JSON blob covering: typical
pricing model for the procurement, comparable processor rate
disclosures, estimated annual transaction volume, our recommended rate
posture (match/beat median), and a profit-math projection using the
recommended rate × volume minus our cost basis. Stored as a single
JSON blob on the proposal row rather than a relational table — the
schema is denormalized and the read pattern is "fetch everything for
one proposal" (the Cost Writer's cached prefix), so a relational shape
would just add joins. New table can come later if a UI tab needs to
filter individual fields.

NULL means the payment market scan has not yet been run for this
proposal (or the proposal is service_line=it_services and uses
the labor-flow MarketScan tables instead).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column("payment_market_scan_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("proposals", "payment_market_scan_json")
