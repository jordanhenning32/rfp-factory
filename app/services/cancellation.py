"""Cancellation registry for long-running background jobs.

Each long-running job (auto review-revise loop, writer team, etc.) registers
a threading.Event keyed by (job_kind, proposal_id) when it starts. UI
buttons call `request_cancel(...)` to set the event. The job polls
`is_cancelled(...)` at safe checkpoints (between sections, passes,
LLM calls) and exits cleanly when the flag is set.

LIMITATION: this does NOT interrupt in-progress LLM calls. The SDKs we use
(anthropic, google-genai) don't expose an abort hook on synchronous
`messages.create` / `generate_content`. So a cancel sits idle until the
current LLM call returns (~30-60s for Reviewer A on Opus, ~5-10s for
Reviewer B on Gemini Flash, ~30-60s for Writer Team on Sonnet). Then the
loop sees the cancel at the next checkpoint and exits.

If we ever need true mid-call abort, switch to the SDKs' streaming APIs
and `cancel()` the response — much heavier change for marginal benefit.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

# Module-level registry. Keyed by (job_kind, proposal_id) so the same
# proposal can run different kinds of jobs concurrently if needed.
_events: dict[tuple[str, int], threading.Event] = {}
_lock = threading.Lock()


def register(job_kind: str, proposal_id: int) -> threading.Event | None:
    """Allocate a cancel event for this job. Returns None (and logs a
    warning) if another job with the same key is already registered —
    callers should treat that as 'refuse to start a concurrent job'.

    Concurrent jobs for the same proposal corrupt the cancel registry
    (the first to finish unregisters using the shared key, removing
    the second job's event), AND they double-write findings/draft rows.
    So we strictly serialize.
    """
    key = (job_kind, proposal_id)
    event = threading.Event()
    with _lock:
        if key in _events:
            log.warning(
                "cancellation: REFUSING to register %s/%d — another job "
                "with the same key is already registered. Existing keys: %r",
                job_kind,
                proposal_id,
                list(_events.keys()),
            )
            return None
        _events[key] = event
        keys = list(_events.keys())
    log.info(
        "cancellation: registered %s/%d (registry now: %r)",
        job_kind,
        proposal_id,
        keys,
    )
    return event


def request_cancel(job_kind: str, proposal_id: int) -> bool:
    """UI calls this to ask the job to stop. Returns True if a job was
    registered (and thus the cancel was delivered), False if no such
    job is running.

    When False, also logs the current registry + a thread-enumeration
    snapshot so we can diagnose why a 'visibly running' loop has no
    registered event (e.g., started under older code, or the registry
    was cleared by another job's finally)."""
    key = (job_kind, proposal_id)
    with _lock:
        event = _events.get(key)
        keys = list(_events.keys())
    if event is None:
        # Diagnostic — enumerate live threads so we can see if a job is
        # actually running despite the missing registry entry.
        thread_names = [
            t.name
            for t in threading.enumerate()
            if t.is_alive()
            and t.name
            and t.name.startswith(
                (
                    "auto-review-",
                    "writer-",
                    "reviewer-",
                    "outline-",
                    "intake-",
                    "shortfall-",
                )
            )
        ]
        log.warning(
            "cancellation: request_cancel %s/%d found NO event. "
            "Registry: %r. Live job threads: %r. (If a thread exists "
            "but isn't in the registry, it was likely started under "
            "older code — restart Python to recover.)",
            job_kind,
            proposal_id,
            keys,
            thread_names,
        )
        return False
    event.set()
    log.info(
        "cancellation: request_cancel %s/%d delivered (registry: %r)",
        job_kind,
        proposal_id,
        keys,
    )
    return True


def is_cancelled(job_kind: str, proposal_id: int) -> bool:
    """Job calls this at checkpoints to decide whether to exit early."""
    with _lock:
        event = _events.get((job_kind, proposal_id))
    return event is not None and event.is_set()


def is_running(job_kind: str, proposal_id: int) -> bool:
    """UI calls this to decide whether to show the Cancel button. True
    when an event is registered AND the cancel has not yet been requested
    (i.e., the job is still actively running, not winding down)."""
    with _lock:
        event = _events.get((job_kind, proposal_id))
    return event is not None and not event.is_set()


def unregister(job_kind: str, proposal_id: int) -> None:
    """Job calls this from its finally block to clean up the registry."""
    key = (job_kind, proposal_id)
    with _lock:
        existed = _events.pop(key, None) is not None
        keys = list(_events.keys())
    log.info(
        "cancellation: unregister %s/%d (existed=%s, registry now: %r)",
        job_kind,
        proposal_id,
        existed,
        keys,
    )


# Job-kind constants so callers don't have to remember magic strings.
JOB_AUTO_REVIEW = "auto_review_revise"


# ---- Active-section tracking --------------------------------------------
# Lets the UI know which sections a long-running per-section loop is
# CURRENTLY processing, so we can mark them "in flight" and discourage
# concurrent user actions (manual regenerate, finding-apply) on them.
# Other sections — already past or not yet reached — are safe to act on.
#
# Set-valued (per proposal_id) so a parallelized auto-loop with N workers
# can mark all N in-flight sections at once. The serial loop just adds
# and removes one at a time.

_active_sections: dict[int, set[int]] = {}


def add_active_section(proposal_id: int, section_pk: int) -> None:
    """Worker calls this when it picks up a section to process."""
    with _lock:
        _active_sections.setdefault(proposal_id, set()).add(section_pk)


def remove_active_section(proposal_id: int, section_pk: int) -> None:
    """Worker calls this when it finishes (or aborts) a section. Cleans
    up the proposal entry entirely once the set drains."""
    with _lock:
        active = _active_sections.get(proposal_id)
        if active is None:
            return
        active.discard(section_pk)
        if not active:
            _active_sections.pop(proposal_id, None)


def clear_active_sections(proposal_id: int) -> None:
    """End-of-loop cleanup. Drops every in-flight marker for this proposal
    so a delayed worker exit can't leave stale entries in the registry."""
    with _lock:
        _active_sections.pop(proposal_id, None)


def get_active_sections(proposal_id: int) -> set[int]:
    """UI calls this to find which sections are currently being processed
    by an active loop. Returns an empty set if no loop is active. Always
    returns a copy so callers can iterate without holding the lock."""
    with _lock:
        active = _active_sections.get(proposal_id)
        return set(active) if active else set()
