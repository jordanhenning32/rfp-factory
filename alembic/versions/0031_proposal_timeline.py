"""proposals.timeline_json — per-proposal implementation timeline (Gantt phases)

Revision ID: 0031
Revises: 0030
Create Date: 2026-04-30

Stores the user-curated implementation timeline rendered on the
Timeline tab. One JSON document per proposal — same pattern as
`payment_market_scan_json` and `payment_cost_review_findings_json`.

Document shape:

    {
        "anchor_date": "YYYY-MM-DD" | null,
        "phases": [
            {
                "id": "uuid4",
                "phase_name": str,
                "start_offset": int,    # days from project start
                "duration": int,        # days
                "deliverable": str,
                "owner": str,
                "color": str,           # "#1F3A5F" hex
                "order": int            # stable sort within same start_offset
            },
            ...
        ]
    }

`anchor_date` is optional. When set, the UI displays absolute calendar
dates alongside the relative offsets (start_offset + duration translate
to "Jun 1 – Jun 30" instead of "d0–d30"). Government RFPs typically
quote schedule in offsets ("60 days post-award") so offsets are the
primary axis; the anchor is purely a display affordance the user opts
into once an actual award date is known.

NULL = the proposal has no timeline yet. The Timeline tab shows an
empty state with an "Add Phase" button + (when available) an
"Import from cost build" button that seeds phases from the active
PricingPackage's phase_breakdown_json.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column("timeline_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("proposals", "timeline_json")
