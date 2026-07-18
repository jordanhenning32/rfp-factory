"""proposals.proposed_scenario — persisted user-selected pricing scenario

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-29

Adds a nullable `proposed_scenario` column to the proposals table so the
Cost tab's scenario-card click persists across runs and feeds the Cost
Writer / Cost Reviewer / cached-prefix Cost Build block. NULL means "no
explicit choice yet" — read sites treat NULL as "MEDIUM" (the legacy
default that was previously hardcoded). Allowed values: LOW / MEDIUM /
HIGH (CUSTOM stays in-memory only — no PricingPackage row exists for
CUSTOM, so persisting it would break the downstream lookup).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column("proposed_scenario", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("proposals", "proposed_scenario")
