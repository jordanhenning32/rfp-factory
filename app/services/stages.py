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
from datetime import UTC, datetime

from app.core.enums import AgentRunStatus
from app.db.session import session_scope
from app.models import AgentRun
from app.services.proposal_access import ensure_proposal_mutable

log = logging.getLogger(__name__)

# Synthetic stage rows do not have a dedicated metadata column.  Reuse the
# otherwise-unused prompt-version field to distinguish a job's terminal row
# from recoverable sub-step failures.  The distinction matters while the app
# is running: a FAILED sub-step can be followed by more intake work, whereas a
# terminal failure makes an immediate retry safe.
TERMINAL_STAGE_MARKER = "terminal-stage-v1"


def record_stage(
    proposal_id: int,
    message: str,
    *,
    status: AgentRunStatus | str = AgentRunStatus.COMPLETED,
    terminal: bool = False,
) -> None:
    """Append a synthetic `_stage` AgentRun row for `proposal_id` with
    `message` as the body and an explicit status. The default remains
    COMPLETED for normal progress messages; callers must pass FAILED for
    actual failure paths. ``terminal=True`` marks that the owning job has
    stopped; ordinary FAILED stage rows remain recoverable sub-step notices.
    Always logs to stdout. Swallows any DB error (most commonly an FK
    violation when the proposal was deleted) so the calling job's error path
    can't be derailed by a logging failure."""
    log.info("[proposal %d] %s", proposal_id, message)
    now = datetime.now(UTC)
    try:
        stage_status = (
            status
            if isinstance(status, AgentRunStatus)
            else AgentRunStatus(status)
        )
        with session_scope() as db:
            ensure_proposal_mutable(
                db, proposal_id, operation="record workflow stage",
            )
            db.add(
                AgentRun(
                    proposal_id=proposal_id,
                    agent_name="_stage",
                    model_used=None,
                    prompt_version=(TERMINAL_STAGE_MARKER if terminal else None),
                    started_at=now,
                    completed_at=now,
                    status=stage_status,
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
