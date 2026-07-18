"""proposals.selected_pricing_model — user override of the agent-recommended model

Revision ID: 0028
Revises: 0027
Create Date: 2026-04-29

For service_line=payment_systems proposals only. The Payment Market
Researcher recommends ONE pricing model based on the procurement type
(interchange_plus / flat_rate / tiered / percentage_of_collected). This
column lets the user override that recommendation — picking, e.g.,
flat_rate even though the agent recommended interchange_plus.

NULL = no override (use the agent's recommendation, read from
proposals.payment_market_scan_json[`pricing_structure.pricing_model`]).
The Cost Volume Writer reads this column to know which model to
narrate. The UI surfaces a mismatch banner + "Re-run scan with
<model> focus" button when user override ≠ agent recommendation, so
the user can fetch rates aligned with their chosen model.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column("selected_pricing_model", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("proposals", "selected_pricing_model")
