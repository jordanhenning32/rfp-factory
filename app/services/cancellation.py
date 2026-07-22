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
from collections.abc import Iterator
from contextlib import contextmanager

from app.services.proposal_access import proposal_write_lock

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
        if event is not None:
            # Set while still holding the registry lock so unregister/new
            # register cannot slip between lookup and signal delivery.
            event.set()
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


def unregister(
    job_kind: str,
    proposal_id: int,
    event: threading.Event | None = None,
) -> bool:
    """Remove a job's cancel event and return whether it was removed.

    Jobs should pass the event returned by :func:`register`. That ownership
    check prevents a delayed/stale cleanup from deleting a newer job that has
    since registered under the same key. ``event=None`` remains available for
    administrative cleanup and backwards-compatible callers.
    """
    key = (job_kind, proposal_id)
    with _lock:
        current = _events.get(key)
        owned = current is not None and (event is None or current is event)
        if owned:
            _events.pop(key, None)
        keys = list(_events.keys())
    log.info(
        "cancellation: unregister %s/%d (removed=%s, registry now: %r)",
        job_kind, proposal_id, owned, keys,
    )
    return owned


# Job-kind constants so callers don't have to remember magic strings.
JOB_AUTO_REVIEW = "auto_review_revise"


# ---- Active-section ownership -------------------------------------------
# This registry is both the UI's "in flight" signal and the write exclusion
# primitive for section-level jobs. A visibility-only refcount is unsafe: two
# workers can both snapshot a draft, call a provider, and then persist in
# completion order (last writer wins). Each section is therefore owned by
# exactly one thread at a time. Acquisition by that same thread is re-entrant
# because the auto-review worker deliberately nests Writer regeneration while
# retaining ownership of the section.


class _SectionOwnership:
    __slots__ = ("owner", "depth")

    def __init__(self, owner: threading.Thread) -> None:
        self.owner = owner
        self.depth = 1


_active_sections: dict[int, dict[int, _SectionOwnership]] = {}


class SectionOwnershipError(RuntimeError):
    """Raised when a section write bypasses its required ownership lease."""


def add_active_section(proposal_id: int, section_pk: int) -> bool:
    """Try to acquire exclusive ownership of one proposal section.

    Returns ``True`` when ownership was acquired. Re-entry by the same thread
    increments a depth counter and also returns ``True``. A different thread
    receives ``False`` and must skip/fail closed without writing the section.
    """
    owner = threading.current_thread()
    # Coordinate acquisition with approval/submission's readiness snapshot.
    # If this wins first, readiness sees the active section and blocks. If the
    # lifecycle transition wins first, this work linearizes after it.
    with proposal_write_lock(proposal_id):
        with _lock:
            active = _active_sections.setdefault(proposal_id, {})
            current = active.get(section_pk)
            if current is None:
                active[section_pk] = _SectionOwnership(owner)
                return True
            if current.owner is owner:
                current.depth += 1
                return True

            log.info(
                "section ownership: refusing proposal=%d section=%d to %s; "
                "already owned by %s",
                proposal_id,
                section_pk,
                owner.name,
                current.owner.name,
            )
            return False


def remove_active_section(proposal_id: int, section_pk: int) -> bool:
    """Release one re-entrant ownership level held by the current thread.

    A non-owner cannot clear another worker's marker. Returning a bool makes
    stale cleanup observable while preserving callers that ignore the result.
    """
    owner = threading.current_thread()
    with _lock:
        active = _active_sections.get(proposal_id)
        if active is None:
            return False
        current = active.get(section_pk)
        if current is None or current.owner is not owner:
            return False
        if current.depth > 1:
            current.depth -= 1
        else:
            active.pop(section_pk, None)
        if not active:
            _active_sections.pop(proposal_id, None)
        return True


def owns_active_section(proposal_id: int, section_pk: int) -> bool:
    """Return whether the current thread owns this section's write lease."""
    owner = threading.current_thread()
    with _lock:
        current = (_active_sections.get(proposal_id) or {}).get(section_pk)
        return current is not None and current.owner is owner


def require_active_section_owner(proposal_id: int, section_pk: int) -> None:
    """Fail closed when a low-level draft persist bypasses orchestration."""
    if not owns_active_section(proposal_id, section_pk):
        raise SectionOwnershipError(
            f"Section {section_pk} on proposal {proposal_id} cannot be "
            "written without an active ownership lease."
        )


@contextmanager
def section_write_lease(section_pk: int) -> Iterator[bool]:
    """Resolve a section's proposal and acquire its re-entrant write lease.

    The session module is resolved at call time so isolated test harnesses and
    alternate data roots remain authoritative. ``False`` means not found or
    already owned by a different thread; callers must not mutate in either
    case.
    """
    from app.db import session as db_session
    from app.models import ProposalSection

    with db_session.session_scope() as db:
        section = db.get(ProposalSection, section_pk)
        proposal_id = section.proposal_id if section is not None else None
    if proposal_id is None:
        yield False
        return

    acquired = add_active_section(int(proposal_id), section_pk)
    try:
        yield acquired
    finally:
        if acquired:
            remove_active_section(int(proposal_id), section_pk)


def clear_active_sections(proposal_id: int) -> None:
    """Administrative/test cleanup for every marker on one proposal.

    Normal workers must pair ``add_active_section`` with
    ``remove_active_section`` instead. Call this only when no real worker for
    the proposal can still be active.
    """
    with _lock:
        _active_sections.pop(proposal_id, None)


def get_active_sections(proposal_id: int) -> set[int]:
    """UI calls this to find which sections are currently being processed
    by an active loop. Returns an empty set if no loop is active. Always
    returns a copy so callers can iterate without holding the lock."""
    with _lock:
        active = _active_sections.get(proposal_id)
        return set(active.keys()) if active else set()
