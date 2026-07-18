"""Stage-message logging for background jobs.

Each long-running job writes synthetic AgentRun rows with
`agent_name='_stage'` as it progresses, so the Run Progress page can
read recent stages without a separate stages table. We repurpose the
`error_text` column to carry the human-readable message.

The DB write is BEST-EFFORT — if `proposal_id` no longer exists (test
data wipe, race with delete, stale background thread), the FK insert
raises `IntegrityError`. We catch it, log a warning, and never re-raise.
Stage logging must not be able to bring down a job, and especially not
shadow a job's *real* failure when its except handler tries to log.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.core.enums import AgentRunStatus
from app.db.session import session_scope
from app.models import AgentRun

log = logging.getLogger(__name__)


def record_stage(proposal_id: int, message: str) -> None:
    """Append a synthetic `_stage` AgentRun row for `proposal_id` with
    `message` as the body. Always logs to stdout. Swallows any DB error
    (most commonly an FK violation when the proposal was deleted) so
    the calling job's error path can't be derailed by a logging
    failure."""
    log.info("[proposal %d] %s", proposal_id, message)
    try:
        with session_scope() as db:
            db.add(
                AgentRun(
                    proposal_id=proposal_id,
                    agent_name="_stage",
                    model_used=None,
                    started_at=datetime.utcnow(),
                    completed_at=datetime.utcnow(),
                    status=AgentRunStatus.COMPLETED,
                    error_text=message,
                )
            )
    except Exception as exc:
        log.warning(
            "stage write failed for proposal %d (%s — proposal may have "
            "been deleted; message=%r). Stage logged to stdout only.",
            proposal_id,
            type(exc).__name__,
            message[:80],
        )
