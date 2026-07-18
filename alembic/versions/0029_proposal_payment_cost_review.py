"""proposals.payment_cost_review_findings_json — persisted output of the payment-systems Cost Reviewer

Revision ID: 0029
Revises: 0028
Create Date: 2026-04-29

The labor-flow Cost Reviewer persists findings to cost_review_findings
(FK to PricingPackage). Payment_systems proposals have no
PricingPackage rows — there's no labor build to scenario-review. The
payment-systems Cost Reviewer adversarially fact-checks the drafted
fee narrative (SEC-005 or equivalent) against the persisted Payment
Market Scan, the company's PCI / compliance posture, the brand
framing rules, and the fit-risk talking points; persists findings as
a single JSON blob on the proposal row (mirrors the
payment_market_scan_json pattern).

NULL = the payment Cost Reviewer hasn't run for this proposal yet.
The Cost Review tab branches on service_line and renders findings
from this column for payment_systems proposals.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column(
            "payment_cost_review_findings_json", sa.Text(), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("proposals", "payment_cost_review_findings_json")
