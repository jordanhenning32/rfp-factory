"""proposals.evaluation_criteria_json — per-proposal Section M evaluation criteria

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-19

Stores the structured output of the Section M extractor agent on the
proposal row. Same pattern as `timeline_json` and
`payment_market_scan_json` — one JSON document per proposal.

Document shape (mirrors the agent's report_evaluation_criteria tool schema):

    {
        "evaluation_method": "best_value" | "lpta" | "trade_off" | "unknown",
        "factors": [
            {
                "factor_id": "F1",
                "factor_name": str,
                "weight_pct": number | null,
                "weight_descriptive": str | null,
                "scoring_scale": str | null,
                "evidence_required": str | null,
                "subfactors": [
                    {"name": str, "weight_pct": number | null, "notes": str | null}
                ]
            },
            ...
        ],
        "section_l_to_m_map": {"REQ-001": ["F1", "F1.1"], ...},
        "trade_off_language": str | null,
        "lowest_price_clause": str | null,
        "extraction_notes": str | null
    }

`evaluation_method` encodes how the buyer will select the awardee:
  - "best_value": buyer may trade price for non-price factors
  - "lpta": Lowest Price Technically Acceptable
  - "trade_off": explicit trade-off process described in the RFP
  - "unknown": RFP is silent or ambiguous

`section_l_to_m_map` cross-references compliance item IDs (REQ-NNN) to
one or more factor IDs (F1, F1.1, …). Empty object when the RFP does not
explicitly cross-reference Section L items to evaluation factors.

NULL = Section M extraction has not yet run for this proposal (older
proposals predating this migration, or extraction failed). The Evaluation
Criteria tab shows an empty state with a "Re-extract evaluation criteria"
button when this column is NULL.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column("evaluation_criteria_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("proposals", "evaluation_criteria_json")
