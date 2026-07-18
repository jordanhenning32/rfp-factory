"""Phase 2B — pre-draft pipeline reorder

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-28

Adds three new ProposalStatus values used as gates between outline
approval and the Writer Team:

  awaiting_team_approval   set by approve-outline; user proceeds to
                            Team tab and assigns named personnel
  awaiting_cost_build       set by approve-team; user proceeds to
                            Cost tab and runs the Cost Analyst
  awaiting_draft            set by Cost Analyst on successful
                            completion; user clicks "Begin Drafting"
                            to spawn the Writer Team

The status column is a VARCHAR(40) — no DDL change needed for new
enum values to be storable. This migration is a no-op on the DB
schema but bumps the alembic revision so the new enum members are
deployed in lockstep with the code that produces them.

Existing proposals are NOT migrated:
- Anything in AWAITING_OUTLINE_APPROVAL stays — when the user
  approves the outline, the new code routes them through the
  team → cost gates from there.
- DRAFT_IN_PROGRESS / DRAFT_READY / REVIEWING / etc. are past
  this phase; their drafts are already written under the old
  order. Re-runs are still possible via the existing per-section
  Regenerate / Refine-with-AI / Apply Strategy paths, which now
  pick up the team roster + cost build automatically when present.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op  # noqa: F401  (imported for hook discovery)

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No DDL changes — proposals.status is a VARCHAR(40) and
    # accepts any string. The new ProposalStatus enum values are
    # introduced at the application layer.
    pass


def downgrade() -> None:
    pass
