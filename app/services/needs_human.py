"""Resolve [NEEDS_HUMAN] placeholders in a drafted ProposalSection.

When the Writer Team drafts a section it leaves placeholders inline:
    "...with a [NEEDS_HUMAN: insert final submission date] of..."
plus a structured entry in needs_human_placeholders_json:
    {marker_text, description, category}

The user resolves each placeholder via the Draft tab UI:
- "edit"      — replace marker with user-supplied free text
- "signature" — replace marker with a typed electronic signature
- "reject"    — remove the marker (and its surrounding bracket text) entirely
- "manual_edit" — placeholder marker was removed by an inline edit; auto-set
                  by reconcile_placeholders

This module performs the atomic update: rewrite draft_text_markdown AND
mark the placeholder resolved in the JSON, both in one transaction.

The placeholder dict gains three optional fields after resolution:
    resolved: bool          — True once the user has acted
    resolution_kind: str    — "edit" | "signature" | "reject" | "manual_edit"
    resolution_value: str   — what was written into the markdown (empty for reject)

A regenerate of the section snapshots resolved placeholders before clearing
the draft, passes them to the Writer Team as "previously resolved human
inputs" so the writer can bake the values directly into the prose instead
of re-emitting the marker, then runs carry_forward_resolved_placeholders()
as a safety net to re-apply any prior resolutions to matching markers the
writer still emits. Net effect: the user's typed values, signatures, and
explicit rejections survive a regenerate.

reconcile_placeholders() keeps the JSON list in sync with the inline markers:
- If a marker appears inline but isn't in JSON, add a synthetic entry so the
  user always has an action card to act on it (the writer occasionally drops
  one).
- If a JSON entry is unresolved but its marker is no longer inline (user
  edited it out), auto-resolve as 'manual_edit'.
"""

from __future__ import annotations

import logging
import re

from app.db.session import session_scope
from app.models import ProposalSection
from app.services.cancellation import section_write_lease
from app.services.proposal_access import ensure_proposal_mutable

log = logging.getLogger(__name__)


# Match `[NEEDS_HUMAN: ...]` non-greedy. re.DOTALL handles markers that wrap
# across lines (rare). Square brackets are literal here — the writer should
# never put unescaped brackets inside the marker_text.
_INLINE_MARKER_RE = re.compile(r"\[NEEDS_HUMAN:\s*(.+?)\]", re.DOTALL)

# Collapses internal whitespace runs (including newlines/tabs) to a single
# space, so reconciliation matches markers across reasonable text-formatting
# differences. Used for the "is this marker still inline?" check only —
# every other place compares marker_text verbatim.
_WS_RE = re.compile(r"\s+")


def _normalize_marker(text: str) -> str:
    """Whitespace-tolerant key for matching JSON marker_text against the
    inline marker. Lowercase NOT applied — case differences may be
    intentional (signature vs Signature, etc.)."""
    return _WS_RE.sub(" ", (text or "").strip())


def _extract_inline_markers(md: str) -> list[str]:
    """Return marker_text values found in `[NEEDS_HUMAN: …]` patterns,
    deduped while preserving first-seen order."""
    if not md:
        return []
    seen: dict[str, None] = {}
    for m in _INLINE_MARKER_RE.finditer(md):
        seen.setdefault(m.group(1).strip(), None)
    return list(seen.keys())


# Pattern for Cost-Writer "TODO marker" identifiers — `[ALL_CAPS_NAME]`
# style. These are structural reminders the user must address, not
# inline placeholders the writer dropped into prose. They were never
# meant to be replaced by string substitution, so reconcile must NOT
# auto-resolve them just because they're absent from the draft.
_TODO_MARKER_PATTERN = re.compile(r"^\[[A-Z][A-Z0-9_]*\]$")


def _is_todo_marker(marker_text: str) -> bool:
    """True when marker_text looks like a Cost-Writer-style TODO
    identifier (`[OPTION_YEAR_PRICING]`, `[ATTACHMENT_D_FORM_LINE_ITEMS]`)
    rather than an inline-replacement placeholder. Used by reconcile
    to skip the orphan-from-text auto-resolve for these — the user
    needs to act on them via the Needs Human Input tab."""
    if not marker_text:
        return False
    return bool(_TODO_MARKER_PATTERN.match(marker_text.strip()))


def _normalize_placeholder_schema(ph: dict) -> dict:
    """Heal placeholder dicts from agents that emitted the wrong field
    name shape. The Cost Writer historically used `marker` instead of
    `marker_text` and omitted `category` entirely; this pass copies
    `marker` → `marker_text` and defaults missing `category` so the UI
    + resolve_placeholder pipeline can act on those rows uniformly.
    Idempotent: rows already in the canonical shape pass through
    unchanged."""
    if ph.get("marker_text") and ph.get("category"):
        return ph
    out = dict(ph)
    if not out.get("marker_text"):
        # Cost Writer's pre-fix schema used `marker`; pull it across.
        legacy = (out.get("marker") or "").strip()
        if legacy:
            out["marker_text"] = legacy
    if not out.get("category"):
        out["category"] = "other"
    return out


def reconcile_placeholders(proposal_section_pk: int) -> bool:
    """Make the JSON placeholder list match the inline markers in the draft.

    Two directions:
    1. Inline marker has no JSON entry → append a synthetic unresolved entry
       (category='other', generic description). Ensures the action panel
       always has a card for every inline marker.
    2. Unresolved JSON entry whose marker is no longer inline → mark resolved
       with kind='manual_edit'. Happens when the user removes a marker via
       inline edit OR when a writer regenerate dropped the marker but kept
       a stale JSON entry.

    Also runs a schema-healing pass over the existing list so legacy
    Cost-Writer placeholders (which used `marker` instead of
    `marker_text` and omitted `category`) become actionable without
    requiring a re-run.

    Idempotent. Returns True if anything changed.
    """
    with section_write_lease(proposal_section_pk) as acquired:
        if not acquired:
            return False
        return _reconcile_placeholders_owned(proposal_section_pk)


def _reconcile_placeholders_owned(proposal_section_pk: int) -> bool:
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None or not sec.draft_text_markdown:
            return False
        ensure_proposal_mutable(
            db, sec.proposal_id, operation="reconcile draft placeholders",
        )

        # Heal field-name shape before any of the directional passes
        # so downstream logic (still_inline check, marker comparison,
        # UI rendering) all see the canonical schema.
        existing = [_normalize_placeholder_schema(ph) for ph in (sec.needs_human_placeholders_json or [])]
        inline_markers = _extract_inline_markers(sec.draft_text_markdown)
        inline_set = set(inline_markers)
        # Whitespace-tolerant lookup — the writer's JSON marker_text and
        # the inline marker text occasionally differ by trailing whitespace
        # or newlines folded into the inline form. Strict-set match would
        # mis-classify those as "no longer inline" and either auto-resolve
        # them (deleting unaddressed work) or fail to clean them up. The
        # normalized check is only used to DECIDE whether the JSON marker
        # is still represented inline — the verbatim marker_text is always
        # what we store and what resolve_placeholder uses.
        inline_normalized = {_normalize_marker(m) for m in inline_markers}

        # Direction A: orphan-from-text — auto-resolve.
        updated: list[dict] = []
        for ph in existing:
            if ph.get("resolved"):
                updated.append(ph)
                continue
            mt = ph.get("marker_text", "")
            mt_norm = _normalize_marker(mt)
            still_inline = (mt and mt in inline_set) or (mt_norm and mt_norm in inline_normalized)
            if mt and not still_inline and not _is_todo_marker(mt):
                # Inline-style placeholder dropped from the draft —
                # treat as user-resolved-via-edit.
                new_ph = dict(ph)
                new_ph["resolved"] = True
                new_ph["resolution_kind"] = "manual_edit"
                new_ph["resolution_value"] = ""
                updated.append(new_ph)
            else:
                # Either (a) still inline, (b) no marker_text yet, or
                # (c) Cost-Writer-style TODO identifier that was never
                # inline. Leave the row unresolved so the user can act
                # on it from the Needs Human Input tab.
                updated.append(ph)

        # Direction B: orphan-from-JSON — synthesize entry. Track unresolved
        # marker_texts already in updated so we don't double-add.
        unresolved_in_updated = {ph.get("marker_text") for ph in updated if not ph.get("resolved")}
        for marker in inline_markers:
            if marker not in unresolved_in_updated:
                updated.append(
                    {
                        "marker_text": marker,
                        "description": (
                            "(detected in draft; the writer didn't include a "
                            "description for this placeholder)"
                        ),
                        "category": "other",
                    }
                )
                unresolved_in_updated.add(marker)

        if updated == existing:
            return False
        sec.needs_human_placeholders_json = updated
        log.info(
            "reconcile_placeholders: section pk=%d, %d -> %d entries",
            proposal_section_pk,
            len(existing),
            len(updated),
        )
        return True


def resolve_placeholder(
    *,
    proposal_section_pk: int,
    marker_text: str,
    kind: str,
    value: str,
) -> bool:
    """Mark one placeholder resolved and rewrite the draft markdown.

    A successful resolution is a user-visible draft edit, so it increments
    ``current_revision_number`` exactly once. Idempotent retries of an
    already-resolved marker remain no-op successes and do not create a
    second revision.

    Returns True if the placeholder was found and updated, OR if a matching
    placeholder is already resolved (idempotent — the user's intent is
    already satisfied, so the action is a no-op success rather than an
    error). Returns False only when no matching placeholder exists at all,
    which signals a real mismatch worth reporting.
    """
    with section_write_lease(proposal_section_pk) as acquired:
        if not acquired:
            return False
        return _resolve_placeholder_owned(
            proposal_section_pk=proposal_section_pk,
            marker_text=marker_text,
            kind=kind,
            value=value,
        )


def _resolve_placeholder_owned(
    *,
    proposal_section_pk: int,
    marker_text: str,
    kind: str,
    value: str,
) -> bool:
    if kind not in ("edit", "signature", "reject"):
        log.warning("resolve_placeholder: unknown kind %r", kind)
        return False

    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None or not sec.draft_text_markdown:
            return False
        ensure_proposal_mutable(
            db, sec.proposal_id, operation="resolve draft placeholder",
        )

        placeholders = list(sec.needs_human_placeholders_json or [])
        match_idx: int | None = None
        any_match: bool = False  # marker_text exists at all (resolved or not)
        for i, ph in enumerate(placeholders):
            if ph.get("marker_text") == marker_text:
                any_match = True
                if not ph.get("resolved"):
                    match_idx = i
                    break

        if match_idx is None:
            # Two cases:
            #  (a) any_match=True → there's already a resolved entry with
            #      this marker_text. The user's intent is satisfied (probably
            #      a stale-UI double-click after a successful first action).
            #      Return True silently.
            #  (b) any_match=False → marker_text doesn't exist in the JSON
            #      at all. Real mismatch — caller surfaces an error.
            if any_match:
                log.info(
                    "resolve_placeholder: marker %r already resolved on "
                    "section pk=%d — treating as no-op success.",
                    marker_text[:60],
                    proposal_section_pk,
                )
                return True
            log.warning(
                "resolve_placeholder: marker %r not found on section pk=%d",
                marker_text,
                proposal_section_pk,
            )
            return False

        # Update the JSON entry.
        updated = dict(placeholders[match_idx])
        updated["resolved"] = True
        updated["resolution_kind"] = kind
        updated["resolution_value"] = value
        placeholders[match_idx] = updated
        sec.needs_human_placeholders_json = placeholders

        # Rewrite the markdown — replace ALL occurrences of the marker (writers
        # sometimes repeat the same marker_text). re.escape handles brackets,
        # parentheses, etc. inside the marker text.
        pattern = re.escape(f"[NEEDS_HUMAN: {marker_text}]")
        new_md = re.sub(pattern, value, sec.draft_text_markdown)
        # If kind=reject and removing the marker leaves a stray double space or
        # an empty parenthetical, leave it — the user can edit later. Aggressive
        # cleanup risks munging legitimate text.
        sec.draft_text_markdown = new_md
        sec.current_revision_number = (sec.current_revision_number or 0) + 1
        log.info(
            "resolved placeholder on section pk=%d: kind=%s len=%d "
            "rev=%d marker=%r",
            proposal_section_pk, kind, len(value),
            sec.current_revision_number, marker_text[:60],
        )

    # Run reconcile in a follow-up transaction (the section is committed
    # by now). Catches the case where the same marker_text had duplicate
    # JSON entries: re.sub above replaced ALL inline occurrences, so any
    # remaining unresolved JSON duplicates are now orphaned-from-text and
    # reconcile auto-resolves them as 'manual_edit'.
    try:
        reconcile_placeholders(proposal_section_pk)
    except Exception:
        log.exception(
            "reconcile after resolve_placeholder failed (section pk=%d)",
            proposal_section_pk,
        )
    return True


_DATE_PATTERNS: tuple[str, ...] = (
    # Substring matches on lowered marker_text. Picks up the
    # "this is the document's submission/transmittal date" cases —
    # NOT kickoff / milestone / deadline dates which are project
    # schedule and not today's date.
    "submission date",
    "submittal date",
    "submitted on",
    "date of submission",
    "transmittal date",
    "transmittal letter date",
    "letter date",
    "cover letter date",
    "date of letter",
    "date of this letter",
    "date of the letter",
    "today's date",
    "today",
    "current date",
    "proposal date",
    "proposal submission date",
)
def _get_signing_authority_name() -> str | None:
    """Return the signing authority only when the active profile names one.

    Reads
    from company_profile.key_personnel where role contains 'CEO'
    (case-insensitive). A missing profile or unnamed authority remains a human
    task; guessing a person's name in submission-ready prose is unsafe.
    """
    try:
        from app.core.company_profile import get_company_profile

        profile = get_company_profile()
        for entry in profile.get("key_personnel") or []:
            role = (entry.get("role") or "").lower()
            if "ceo" in role or "chief executive" in role:
                name = (entry.get("name") or "").strip()
                if name:
                    return name
    except Exception:
        log.warning("_get_signing_authority_name: profile lookup failed")
    return None


def auto_resolve_obvious_placeholders(proposal_section_pk: int) -> int:
    """Auto-resolve [NEEDS_HUMAN] placeholders that have safe
    defaults so the user isn't asked for the obvious stuff.

    Two rules currently apply:
      1. category=='signature'  → kind='signature', value=<profile CEO name>
         only when the active profile explicitly names that authority.
      2. marker_text matches a doc-date pattern (submission /
         transmittal / cover letter date / "today") → kind='edit',
         value=today formatted as 'Month D, YYYY' (e.g.
         'April 28, 2026'). Pattern list is conservative — it
         skips kickoff / milestone / deadline dates which depend
         on the contract award, not today.

    Called from the writer-team paths in app.jobs.writer right
    after persist_section_draft, so the user never sees these
    placeholders surface on the Needs Human Input tab.

    Returns the count of placeholders resolved by this pass."""
    with section_write_lease(proposal_section_pk) as acquired:
        if not acquired:
            return 0
        return _auto_resolve_obvious_placeholders_owned(proposal_section_pk)


def _auto_resolve_obvious_placeholders_owned(
    proposal_section_pk: int,
) -> int:
    from datetime import date as _date

    today_str = _date.today().strftime("%B %d, %Y")
    signing_name = _get_signing_authority_name()

    # Snapshot first, then call resolve_placeholder per match.
    # resolve_placeholder takes its own session and rewrites the
    # draft markdown, so we can't hold the read session through
    # the loop.
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None:
            return 0
        ensure_proposal_mutable(
            db, sec.proposal_id, operation="auto-resolve draft placeholders",
        )
        placeholders = list(sec.needs_human_placeholders_json or [])

    actions: list[tuple[str, str, str]] = []
    for ph in placeholders:
        if ph.get("resolved"):
            continue
        marker_text = (ph.get("marker_text") or "").strip()
        if not marker_text:
            continue
        category = (ph.get("category") or "").lower()

        if category == "signature" and signing_name:
            actions.append((marker_text, "signature", signing_name))
            continue

        mt_lower = marker_text.lower()
        if any(pat in mt_lower for pat in _DATE_PATTERNS):
            actions.append((marker_text, "edit", today_str))

    n_resolved = 0
    for marker_text, kind, value in actions:
        try:
            ok = resolve_placeholder(
                proposal_section_pk=proposal_section_pk,
                marker_text=marker_text,
                kind=kind,
                value=value,
            )
            if ok:
                n_resolved += 1
        except Exception:
            log.exception(
                "auto_resolve_obvious_placeholders: failed on marker %r (section pk=%d)",
                marker_text[:80],
                proposal_section_pk,
            )

    if n_resolved:
        log.info(
            "auto_resolve_obvious_placeholders: section pk=%d "
            "auto-resolved %d placeholder(s) "
            "(signatures + doc dates)",
            proposal_section_pk,
            n_resolved,
        )
    return n_resolved


def auto_resolve_via_llm(proposal_section_pk: int) -> int:
    """Phase B post-pass. Runs the Needs Human Resolver agent
    against whatever [NEEDS_HUMAN] placeholders remain on a section
    after the deterministic auto-resolver and the carry-forward
    pass have done their work. The agent decides per marker:
      edit   — answer is in the cached context; fill it in
      reject — submission-checklist item; remove from narrative
      skip   — needs human judgment; leave for the user

    Returns the count of placeholders the LLM resolved (edit +
    reject; skips don't count). Silent no-op when the section has
    no unresolved placeholders, when the section has no
    proposal_id link, or when the agent call fails (we log and
    swallow — the user still has the placeholders surfaced and
    can resolve manually).
    """
    with section_write_lease(proposal_section_pk) as acquired:
        if not acquired:
            return 0
        return _auto_resolve_via_llm_owned(proposal_section_pk)


def _auto_resolve_via_llm_owned(proposal_section_pk: int) -> int:
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None:
            return 0
        ensure_proposal_mutable(
            db, sec.proposal_id, operation="auto-resolve draft placeholders",
        )
        proposal_id = sec.proposal_id
        section_id = sec.section_id or ""
        section_title = sec.section_title or ""
        placeholders = list(sec.needs_human_placeholders_json or [])

    unresolved = [
        ph for ph in placeholders if not ph.get("resolved") and (ph.get("marker_text") or "").strip()
    ]
    if not unresolved:
        return 0

    # Build the cached context the agent reads. Each block is best-
    # effort — empty when the upstream gate hasn't been hit (e.g.,
    # cost build absent on a fresh proposal).
    try:
        from app.core.company_profile import get_company_profile
        from app.core.decisions import format_decisions_for_prompt
        from app.services.pricing import format_cost_build_block_for_writer
        from app.services.team import format_team_block_for_writer

        profile = get_company_profile()
        # Compact profile snapshot — just the fields the resolver
        # actually mines for answers. The writer's cached prefix
        # has the full profile JSON; we don't need to repeat it.
        profile_bits: list[str] = []
        if cert := profile.get("certifications"):
            profile_bits.append("Certifications:\n  " + "\n  ".join(f"- {c}" for c in cert))
        if vehicles := profile.get("contract_vehicles"):
            profile_bits.append("Contract vehicles:\n  " + "\n  ".join(f"- {v}" for v in vehicles))
        if caps := profile.get("capability_areas"):
            profile_bits.append("Capability areas:\n  " + "\n  ".join(f"- {c}" for c in caps))
        if kp := profile.get("key_personnel") or []:
            kp_lines: list[str] = ["Key personnel:"]
            for entry in kp[:30]:
                nm = entry.get("name") or "?"
                role = entry.get("role") or ""
                yrs = entry.get("years_experience")
                focus = entry.get("focus") or ""
                bits = [f"  - {nm}"]
                if role:
                    bits.append(f"({role}")
                    bits.append(f", {yrs} yrs)" if yrs is not None else ")")
                kp_lines.append(" ".join(bits))
                if focus:
                    kp_lines.append(f"      focus: {focus}")
            profile_bits.append("\n".join(kp_lines))
        if pp := profile.get("past_performance") or []:
            profile_bits.append(f"Past performance count: {len(pp)} (canonical entries available)")
        profile_summary = "\n\n".join(profile_bits) if profile_bits else "(empty)"

        decisions_text = format_decisions_for_prompt() or "(none)"
        team_roster_block = format_team_block_for_writer(proposal_id) or "(no approved team)"
        cost_build_block = format_cost_build_block_for_writer(proposal_id) or "(no cost build)"
    except Exception:
        log.exception(
            "auto_resolve_via_llm: failed to build context for section pk=%d — skipping LLM pass",
            proposal_section_pk,
        )
        return 0

    try:
        from app.agents.needs_human_resolver import resolve_placeholders

        result = resolve_placeholders(
            proposal_id=proposal_id,
            section_id=section_id,
            section_title=section_title,
            placeholders=unresolved,
            profile_summary=profile_summary,
            decisions_text=decisions_text,
            team_roster_block=team_roster_block,
            cost_build_block=cost_build_block,
        )
    except Exception:
        log.exception(
            "auto_resolve_via_llm: agent call failed for section "
            "pk=%d — leaving placeholders for the user to action",
            proposal_section_pk,
        )
        return 0

    n_applied = 0
    for d in result.decisions:
        if d.action == "skip":
            continue
        kind = "edit" if d.action == "edit" else "reject"
        value = d.value if kind == "edit" else ""
        if kind == "edit" and not value.strip():
            log.info(
                "auto_resolve_via_llm: skipping 'edit' with empty value (marker=%r, reason=%r)",
                d.marker_text[:60],
                d.reason[:80],
            )
            continue
        try:
            ok = resolve_placeholder(
                proposal_section_pk=proposal_section_pk,
                marker_text=d.marker_text,
                kind=kind,
                value=value,
            )
            if ok:
                n_applied += 1
                log.info(
                    "auto_resolve_via_llm: section pk=%d marker=%r -> %s (%s)",
                    proposal_section_pk,
                    d.marker_text[:60],
                    d.action,
                    d.reason[:100],
                )
        except Exception:
            log.exception(
                "auto_resolve_via_llm: resolve_placeholder failed for marker %r",
                d.marker_text[:60],
            )

    if n_applied:
        log.info(
            "auto_resolve_via_llm: section pk=%d applied %d LLM-driven resolutions",
            proposal_section_pk,
            n_applied,
        )
    return n_applied


def snapshot_resolved_placeholders(proposal_section_pk: int) -> list[dict]:
    """Read the section's resolved placeholders so they can survive a
    regenerate. Returns only the resolutions with explicit values:
    'edit' (typed text), 'signature' (typed name), 'reject' (declined).
    'manual_edit' is excluded — we don't have the user's inline replacement
    text, so we can't carry it forward; the user will see any new marker
    in that spot and can act again.
    """
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None:
            return []
        out: list[dict] = []
        for ph in sec.needs_human_placeholders_json or []:
            if not ph.get("resolved"):
                continue
            if ph.get("resolution_kind") not in ("edit", "signature", "reject"):
                continue
            if not ph.get("marker_text"):
                continue
            out.append(dict(ph))
        return out


def carry_forward_resolved_placeholders(
    proposal_section_pk: int,
    prior_resolved: list[dict],
) -> int:
    """Re-apply previously-resolved placeholders to a freshly regenerated
    section. Safety net for when the Writer Team re-emits a marker the
    user already answered (despite the prompt instruction not to).

    Match strategy: whitespace-tolerant compare on marker_text. A prior
    'edit'/'signature' is replayed by substituting the marker with the
    stored resolution_value; a prior 'reject' is replayed by removing
    the marker (empty replacement). Each prior consumes at most one
    new marker so duplicate marker_text in the new draft don't all
    collapse into one resolution.

    Returns the count of priors carried forward.
    """
    with section_write_lease(proposal_section_pk) as acquired:
        if not acquired:
            return 0
        return _carry_forward_resolved_placeholders_owned(
            proposal_section_pk,
            prior_resolved,
        )


def _carry_forward_resolved_placeholders_owned(
    proposal_section_pk: int,
    prior_resolved: list[dict],
) -> int:
    if not prior_resolved:
        return 0
    actionable = [
        ph
        for ph in prior_resolved
        if ph.get("resolved")
        and ph.get("resolution_kind") in ("edit", "signature", "reject")
        and ph.get("marker_text")
    ]
    if not actionable:
        return 0

    carried = 0
    with session_scope() as db:
        sec = db.get(ProposalSection, proposal_section_pk)
        if sec is None or not sec.draft_text_markdown:
            return 0
        ensure_proposal_mutable(
            db, sec.proposal_id, operation="carry forward draft placeholders",
        )
        new_placeholders = list(sec.needs_human_placeholders_json or [])
        if not new_placeholders:
            return 0

        # Index unresolved new placeholders by normalized marker_text.
        new_by_norm: dict[str, list[int]] = {}
        for i, ph in enumerate(new_placeholders):
            if ph.get("resolved"):
                continue
            mt = ph.get("marker_text") or ""
            if not mt:
                continue
            new_by_norm.setdefault(_normalize_marker(mt), []).append(i)

        new_md = sec.draft_text_markdown
        used: set[int] = set()

        for prior in actionable:
            prior_norm = _normalize_marker(prior.get("marker_text") or "")
            kind = prior["resolution_kind"]
            value = prior.get("resolution_value") or ""
            target_idx: int | None = None
            for idx in new_by_norm.get(prior_norm, []):
                if idx in used:
                    continue
                target_idx = idx
                break
            if target_idx is None:
                continue

            new_marker_text = new_placeholders[target_idx].get("marker_text") or ""
            pattern = re.escape(f"[NEEDS_HUMAN: {new_marker_text}]")
            # Single replacement keeps duplicate marker_text safe — the
            # next prior with the same normalized text matches a different
            # JSON index, but we still only sub once per prior here. If
            # the inline marker repeats verbatim, the whitespace-tolerant
            # JSON match means each duplicate prior peels off one inline
            # occurrence in turn.
            new_md, n_subs = re.subn(pattern, value, new_md, count=1)
            if n_subs == 0:
                # JSON entry exists but no inline marker found — uncommon
                # (writer JSON / draft mismatch). Still mark JSON resolved
                # so the user doesn't see a phantom action card.
                pass

            updated = dict(new_placeholders[target_idx])
            updated["resolved"] = True
            updated["resolution_kind"] = kind
            updated["resolution_value"] = value
            new_placeholders[target_idx] = updated
            used.add(target_idx)
            carried += 1

        if carried:
            sec.draft_text_markdown = new_md
            sec.needs_human_placeholders_json = new_placeholders
            log.info(
                "carry_forward_resolved_placeholders: section pk=%d carried %d/%d (edit/signature/reject)",
                proposal_section_pk,
                carried,
                len(actionable),
            )

    if carried:
        try:
            reconcile_placeholders(proposal_section_pk)
        except Exception:
            log.exception(
                "reconcile after carry_forward failed (section pk=%d)",
                proposal_section_pk,
            )
    return carried
