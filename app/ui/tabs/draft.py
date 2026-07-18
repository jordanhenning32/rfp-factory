"""Proposal Review > Draft tab.

Renders each ProposalSection.draft_text_markdown plus citations and
inline [NEEDS_HUMAN] placeholders. Per-section toolbar: Edit (inline
markdown editor) · Refine with AI (directive dialog) · Regenerate
(full re-run of the Writer for that section).

Includes every helper used by the tab itself:
  - placeholder dialogs (Provide value / Signature / Remove / Save decision)
  - placeholder rendering (_render_placeholder_action_card +
    _render_section_action_panel + _highlight_and_link_placeholders +
    _strip_cite_markers_for_display)
  - section-refine dialog (_open_refine_dialog) + suggestion chips
  - the _NEEDS_HUMAN_CATEGORY_LABELS / _CITE_MARKER_RE constants
"""

from __future__ import annotations

import asyncio
import logging
import re as _re_for_display
from datetime import datetime

from nicegui import ui
from sqlalchemy import select

from app.db.session import SessionLocal
from app.jobs.writer import spawn_writer_for_section
from app.models import Proposal, ProposalSection
from app.services.needs_human import reconcile_placeholders, resolve_placeholder
from app.services.sections import save_manual_edit
from app.ui._shared import _empty_state

log = logging.getLogger(__name__)


# Helpers that still live in pages.py — resolved lazily on first call
# so we don't hit a circular import at module-load time.
def _pages_helper(name: str):
    from app.ui import pages

    return getattr(pages, name)


def _refine_section_with_ai(*args, **kwargs):
    return _pages_helper("_refine_section_with_ai")(*args, **kwargs)


def _regenerate_section(*args, **kwargs):
    return _pages_helper("_regenerate_section")(*args, **kwargs)


def _force_restart_writer_team(*args, **kwargs):
    return _pages_helper("_force_restart_writer_team")(*args, **kwargs)


def _begin_drafting(*args, **kwargs):
    return _pages_helper("_begin_drafting")(*args, **kwargs)


# Used by the per-section placeholder action card on the Draft tab.
# (The aggregated Needs Human Input tab was removed — its content was
# redundant with the per-section view + the system_verified readiness
# checks on the Submission Checklist tab.)
_NEEDS_HUMAN_CATEGORY_LABELS = {
    "pricing": "Pricing",
    "schedule_commitment": "Schedule",
    "teaming_confirmation": "Teaming",
    "specific_personnel": "Personnel",
    "specific_numbers": "Numbers",
    "policy_decision": "Policy",
    "signature": "Signature",
    "other": "Other",
}


def _is_signature_placeholder(ph: dict) -> bool:
    """Heuristic — we trust the writer's category, but also catch cases where
    older drafts (pre-signature-category) flagged a signature need under
    'other' by sniffing the description for 'sign' / 'signature'."""
    if ph.get("category") == "signature":
        return True
    txt = f"{ph.get('description', '')} {ph.get('marker_text', '')}".lower()
    return "signature" in txt or "sign here" in txt or "/s/" in txt


# Strip cite markers from the rendered display. The raw markdown still has
# them — Reviewer A (Weeks 9-10) needs them for cite-back-to-source checks,
# and DOCX export (Week 11) will also strip at export time. This is a
# DISPLAY-ONLY transform.
_CITE_MARKER_RE = _re_for_display.compile(r"\[\^cite-\d+\]")


def _strip_cite_markers_for_display(md: str) -> str:
    if not md:
        return md
    return _CITE_MARKER_RE.sub("", md)


def _highlight_and_link_placeholders(md: str, placeholders: list[dict], section_pk: int) -> str:
    """Replace each unresolved `[NEEDS_HUMAN: marker]` with a markdown link
    that anchors to the action card above the section. Click in the rendered
    prose → browser scrolls to the matching card (`id="nh-{section_pk}-{idx}"`
    set in `_render_placeholder_action_card`), and the user picks Sign /
    Provide value / Remove from there.

    Resolved placeholders aren't here — their markers were already
    substituted with the user's resolution_value when they resolved them.
    """
    if not md:
        return md
    out = md
    unresolved = [p for p in placeholders if not p.get("resolved")]
    for idx, ph in enumerate(unresolved):
        marker_text = ph.get("marker_text", "")
        if not marker_text:
            continue
        old = f"[NEEDS_HUMAN: {marker_text}]"
        anchor = f"#nh-{section_pk}-{idx}"
        new = f"[**🟡 NEEDS HUMAN — {marker_text}**]({anchor})"
        out = out.replace(old, new)
    return out


def _open_provide_value_dialog(
    proposal_id: int,
    section_pk: int,
    marker_text: str,
    description: str,
    category: str,
    on_change,
) -> None:
    """Free-text dialog with an AI-suggested replacement and a chat box for
    iterating with the AI.

    Layout:
      - Marker text (read-only context).
      - AI suggestion card (latest suggestion, with Use-this / Append).
      - Chat panel: transcript of refinement turns + input + send. Each
        send refines the suggestion (history is passed back to the AI).
      - Replacement textarea (the value that actually applies).
      - Cancel / Apply.

    Conversation state lives in `chat_state["history"]` (alternating
    user/assistant turns AFTER the seed). Regenerate resets history.
    """
    # Mutable state for the dialog. `history` mirrors what we send to the
    # advisor each turn — a list of {role, content} starting with the
    # initial assistant suggestion (the seed user message is reconstructed
    # internally by the advisor on every call, so it's never in history).
    chat_state: dict = {"history": [], "latest_suggestion": ""}

    with ui.dialog() as dlg, ui.card().classes("min-w-[32rem] max-w-[52rem]"):
        ui.label("Provide value").classes("text-base font-semibold")
        ui.label(description).classes("text-sm opacity-80 pt-1")
        with ui.card().classes("w-full bg-slate-50 border-l-4 border-amber-300 shadow-none"):
            ui.label("WHAT NEEDS YOUR INPUT").classes(
                "text-[10px] font-semibold tracking-wider text-amber-800 uppercase"
            )
            ui.label(marker_text).classes(
                "text-sm text-slate-800 leading-relaxed break-words whitespace-pre-wrap pt-1"
            )

        # AI suggestion card — shows the LATEST suggestion (initial single-
        # shot or any chat-driven refinement). "Use this" copies it into
        # the replacement textarea below.
        with ui.card().classes("w-full bg-blue-50 border-l-4 border-blue-400"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("auto_awesome").classes("text-blue-700")
                    ui.label("AI-suggested replacement").classes("text-xs font-medium text-blue-900")
                regen_btn = (
                    ui.button(
                        "Regenerate",
                        icon="refresh",
                    )
                    .props("flat dense size=sm color=primary")
                    .tooltip("Reset the conversation and get a fresh single-shot suggestion.")
                )
            sugg_container = ui.column().classes("w-full pt-2 gap-1")

        # Chat panel — refine the suggestion conversationally.
        with ui.card().classes("w-full bg-slate-50 border-l-4 border-slate-400 mt-2"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("chat").classes("text-slate-700")
                ui.label("Refine with AI").classes("text-xs font-medium text-slate-900")
            ui.label(
                "Ask the AI to adjust: 'make it shorter', 'use 6 months instead', "
                "'why did you pick that range?', 'now write it more formal'."
            ).classes("text-xs opacity-70")
            transcript_column = ui.column().classes("w-full pt-2 gap-1 max-h-48 overflow-y-auto")
            with ui.row().classes("w-full pt-2 gap-2 items-end"):
                chat_input = (
                    ui.input(
                        placeholder="Type a refinement or question…",
                    )
                    .classes("flex-1")
                    .props("dense outlined")
                )
                send_btn = ui.button(
                    "Send",
                    icon="send",
                ).props("color=primary dense")

        value_input = (
            ui.textarea(
                "Replacement text",
                placeholder="What should appear here in the proposal?",
            )
            .classes("w-full pt-2")
            .props("autogrow rounded outlined")
        )
        ui.label("This text replaces the marker exactly. Markdown formatting is preserved.").classes(
            "text-xs opacity-60 pt-1"
        )

        # Optional: track this commitment on the Submission Checklist.
        # When the proposal commits to delivering an artifact (a diagram,
        # a labeled exhibit, a plan, etc.), the user shouldn't have to
        # remember it on submission day — toggle this on and the
        # commitment gets a row on the Submission Checklist tab next to
        # the form-fill / cert items extracted from the matrix.
        with ui.card().classes("w-full bg-emerald-50 border-l-4 border-emerald-400 shadow-none mt-2"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("checklist").classes("text-emerald-700")
                ui.label("Track this as a deliverable commitment").classes(
                    "text-xs font-medium text-emerald-900"
                )
            track_checkbox = ui.checkbox(
                "Add this commitment to the Submission Checklist",
                value=False,
            ).classes("pt-1")
            ui.label(
                "Toggle ON when the replacement says the proposal will "
                "deliver / attach / produce something specific (a "
                "diagram, a plan, a labeled exhibit). The Submission "
                "Checklist will track it so it's not forgotten on "
                "submission day."
            ).classes("text-xs opacity-70")
            commitment_desc_input = (
                ui.input(
                    "Commitment description (editable)",
                    value=description,  # default to the placeholder's description
                )
                .classes("w-full pt-1")
                .props("dense outlined")
            )

        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Cancel", on_click=dlg.close).props("flat")

            def apply() -> None:
                v = (value_input.value or "").strip()
                if not v:
                    ui.notify(
                        "Empty replacement — use Remove if you want to delete.",
                        type="warning",
                    )
                    return
                ok = resolve_placeholder(
                    proposal_section_pk=section_pk,
                    marker_text=marker_text,
                    kind="edit",
                    value=v,
                )
                # If user opted in, also persist the commitment so it
                # shows on the Submission Checklist. Done AFTER the
                # placeholder resolution so a failure here doesn't
                # roll back the draft edit.
                tracked = False
                if ok and track_checkbox.value:
                    desc = (commitment_desc_input.value or "").strip()
                    if desc:
                        try:
                            from app.services.submission_commitments import (
                                add_submission_commitment,
                            )

                            add_submission_commitment(
                                proposal_id=proposal_id,
                                description=desc,
                                source="needs_human_apply",
                                source_section_id=section_pk,
                            )
                            tracked = True
                        except Exception:
                            log.exception(
                                "failed to add submission commitment (placeholder still resolved)",
                            )
                dlg.close()
                if ok:
                    msg = "Value applied to the draft."
                    if tracked:
                        msg += " Commitment added to the Submission Checklist."
                    ui.notify(msg, type="positive", multi_line=tracked, timeout=5000)
                    on_change()
                    # Decision-ledger auto-capture: if the user has
                    # supplied this same value 2+ times across all
                    # placeholders (and no existing decision covers
                    # it), surface a follow-up dialog asking whether
                    # to save it as a cross-RFP decision so the
                    # writer auto-applies it on future proposals.
                    try:
                        from app.services.decision_capture import (
                            detect_decision_candidate,
                        )

                        candidate = detect_decision_candidate(
                            v,
                            kind="edit",
                        )
                        if candidate:
                            _open_save_decision_dialog(
                                proposal_id,
                                candidate,
                            )
                    except Exception:
                        log.exception(
                            "decision-capture detection failed; ignoring (placeholder still resolved)"
                        )
                else:
                    ui.notify(
                        "Could not apply — this marker no longer exists "
                        "in the draft. The section may have been "
                        "regenerated; refresh to see the latest "
                        "placeholders.",
                        type="negative",
                        timeout=6000,
                    )
                    on_change()

            ui.button("Apply", on_click=apply).props("color=primary")

        # ---- Helpers --------------------------------------------------

        def _render_suggestion(suggestion: str, rationale: str) -> None:
            """Replace the suggestion-card body with a new suggestion +
            Use-this / Append buttons."""
            sugg_container.clear()
            with sugg_container:
                ui.label(suggestion).classes("text-sm text-blue-900 italic whitespace-pre-wrap")
                if rationale:
                    ui.label(f"Why: {rationale}").classes("text-xs opacity-70 pt-1")
                with ui.row().classes("gap-2 pt-2"):
                    ui.button(
                        "Use this",
                        icon="check",
                        on_click=lambda s=suggestion: value_input.set_value(s),
                    ).props("color=primary outline dense size=sm")
                    ui.button(
                        "Append to my text",
                        icon="add",
                        on_click=lambda s=suggestion: value_input.set_value(
                            (value_input.value or "") + ("\n\n" if value_input.value else "") + s
                        ),
                    ).props("flat dense size=sm")

        def _show_thinking(label: str = "Thinking…") -> None:
            sugg_container.clear()
            with sugg_container:
                with ui.row().classes("items-center gap-2"):
                    ui.spinner("dots", color="primary")
                    ui.label(label).classes("text-sm opacity-70")

        def _add_user_to_transcript(text: str) -> None:
            with transcript_column:
                with ui.row().classes("items-start gap-2 w-full"):
                    ui.icon("person").classes("text-slate-600 text-sm pt-0.5")
                    ui.label(text).classes("text-sm flex-1")

        def _add_assistant_to_transcript(suggestion: str) -> None:
            with transcript_column:
                with ui.row().classes("items-start gap-2 w-full"):
                    ui.icon("auto_awesome").classes("text-blue-600 text-sm pt-0.5")
                    preview = suggestion[:160] + ("…" if len(suggestion) > 160 else "")
                    ui.label(preview).classes("text-sm italic flex-1 text-blue-900")

        def _assistant_history_payload(suggestion: str, rationale: str) -> str:
            """Compact assistant turn we record in conversation history.
            Anthropic uses this for context on the next call. Keep it
            text — the actual tool call format isn't needed here."""
            text = f"Suggestion: {suggestion}"
            if rationale:
                text += f"\nRationale: {rationale}"
            return text

        # ---- Initial fetch ---------------------------------------------

        async def fetch_initial_suggestion() -> None:
            """Single-shot initial suggestion. Resets chat history."""
            chat_state["history"] = []
            chat_state["latest_suggestion"] = ""
            transcript_column.clear()
            _show_thinking("Generating suggestion (Haiku, ~$0.005)…")
            try:
                from app.agents.needs_human_advisor import chat_about_placeholder

                result = await asyncio.to_thread(
                    chat_about_placeholder,
                    proposal_id=proposal_id,
                    section_pk=section_pk,
                    marker_text=marker_text,
                    description=description or "",
                    category=category or "other",
                )
            except Exception as exc:
                log.exception("needs_human_advisor failed")
                sugg_container.clear()
                with sugg_container:
                    ui.label(f"Could not generate suggestion: {exc}").classes("text-sm text-red-700")
                return

            suggestion = (result.get("suggestion") or "(empty)").strip()
            rationale = (result.get("rationale") or "").strip()
            chat_state["latest_suggestion"] = suggestion
            chat_state["history"] = [
                {
                    "role": "assistant",
                    "content": _assistant_history_payload(suggestion, rationale),
                }
            ]
            _render_suggestion(suggestion, rationale)

        # ---- Chat send -------------------------------------------------

        async def send_chat() -> None:
            user_msg = (chat_input.value or "").strip()
            if not user_msg:
                return
            chat_input.set_value("")

            _add_user_to_transcript(user_msg)
            _show_thinking("Refining…")

            try:
                from app.agents.needs_human_advisor import chat_about_placeholder

                result = await asyncio.to_thread(
                    chat_about_placeholder,
                    proposal_id=proposal_id,
                    section_pk=section_pk,
                    marker_text=marker_text,
                    description=description or "",
                    category=category or "other",
                    history=chat_state["history"],
                    user_message=user_msg,
                )
            except Exception as exc:
                log.exception("needs_human_advisor chat failed")
                sugg_container.clear()
                with sugg_container:
                    if chat_state["latest_suggestion"]:
                        # Restore the prior suggestion so the user doesn't
                        # lose it; show error inline below.
                        ui.label(chat_state["latest_suggestion"]).classes(
                            "text-sm text-blue-900 italic whitespace-pre-wrap"
                        )
                    ui.label(f"Refine failed: {exc}").classes("text-xs text-red-700 pt-1")
                return

            suggestion = (result.get("suggestion") or "(empty)").strip()
            rationale = (result.get("rationale") or "").strip()

            # Update conversation state.
            chat_state["history"].append({"role": "user", "content": user_msg})
            chat_state["history"].append(
                {
                    "role": "assistant",
                    "content": _assistant_history_payload(suggestion, rationale),
                }
            )
            chat_state["latest_suggestion"] = suggestion

            _render_suggestion(suggestion, rationale)
            _add_assistant_to_transcript(suggestion)

        # ---- Wire events -----------------------------------------------

        regen_btn.on_click(fetch_initial_suggestion)
        send_btn.on_click(send_chat)
        # Enter in the chat input also sends.
        chat_input.on("keydown.enter", send_chat)

        # Auto-fetch the first suggestion when the dialog opens.
        ui.timer(0.05, fetch_initial_suggestion, once=True)
    dlg.open()


def _open_signature_dialog(section_pk: int, marker_text: str, description: str, on_change) -> None:
    """Typed e-signature dialog — produces '/s/ Name — Date' inline.

    Defaults: extracts a likely name from the description (proper-noun match);
    today's date pre-filled."""
    import re as _re

    name_match = _re.search(
        r"\b([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,2})\b",
        description or "",
    )
    default_name = name_match.group(1) if name_match else ""
    today = datetime.now().strftime("%B %d, %Y")

    with ui.dialog() as dlg, ui.card().classes("min-w-[28rem]"):
        ui.label("Apply Electronic Signature").classes("text-base font-semibold")
        ui.label(description).classes("text-sm opacity-80 pt-1")
        with ui.card().classes("w-full bg-slate-50 border-l-4 border-amber-300 shadow-none"):
            ui.label("WHERE YOU'LL SIGN").classes(
                "text-[10px] font-semibold tracking-wider text-amber-800 uppercase"
            )
            ui.label(marker_text).classes(
                "text-sm text-slate-800 leading-relaxed break-words whitespace-pre-wrap pt-1"
            )
        name_input = ui.input("Signed by", value=default_name).classes("w-full")
        date_input = ui.input("Date", value=today).classes("w-full")
        preview_card = ui.card().classes("w-full bg-slate-50 border border-slate-300")
        with preview_card:
            preview = ui.label(f"/s/ {default_name} — {today}").classes("text-base font-mono")

        def update_preview() -> None:
            n = (name_input.value or "(name)").strip() or "(name)"
            d = (date_input.value or "(date)").strip() or "(date)"
            preview.set_text(f"/s/ {n} — {d}")

        name_input.on_value_change(lambda _: update_preview())
        date_input.on_value_change(lambda _: update_preview())

        ui.label(
            "By clicking Apply, the typed name above is inserted at the marker as your electronic signature."
        ).classes("text-xs opacity-70 pt-2")

        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Cancel", on_click=dlg.close).props("flat")

            def apply() -> None:
                n = (name_input.value or "").strip()
                d = (date_input.value or "").strip()
                if not n:
                    ui.notify("Please type a name to sign.", type="warning")
                    return
                value = f"/s/ {n} — {d}" if d else f"/s/ {n}"
                ok = resolve_placeholder(
                    proposal_section_pk=section_pk,
                    marker_text=marker_text,
                    kind="signature",
                    value=value,
                )
                dlg.close()
                if ok:
                    ui.notify("Signature applied.", type="positive")
                    on_change()
                else:
                    ui.notify(
                        "Could not apply — this marker no longer exists "
                        "in the draft. The section may have been "
                        "regenerated; refresh to see the latest "
                        "placeholders.",
                        type="negative",
                        timeout=6000,
                    )
                    on_change()

            ui.button("Apply Signature", icon="draw", on_click=apply).props("color=primary")
    dlg.open()


def _open_remove_placeholder_dialog(section_pk: int, marker_text: str, description: str, on_change) -> None:
    """Confirm-and-remove dialog — deletes the marker from the draft."""
    with ui.dialog() as dlg, ui.card().classes("min-w-[28rem]"):
        ui.label("Remove placeholder?").classes("text-base font-semibold")
        ui.label(description).classes("text-sm opacity-80 pt-1")
        with ui.card().classes("w-full bg-slate-50 border-l-4 border-amber-300 shadow-none"):
            ui.label("PLACEHOLDER TO REMOVE").classes(
                "text-[10px] font-semibold tracking-wider text-amber-800 uppercase"
            )
            ui.label(marker_text).classes(
                "text-sm text-slate-800 leading-relaxed break-words whitespace-pre-wrap pt-1"
            )
        ui.label(
            "The marker will be deleted from the draft. To restore it, regenerate this section."
        ).classes("text-sm pt-2")
        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Cancel", on_click=dlg.close).props("flat")

            def apply() -> None:
                ok = resolve_placeholder(
                    proposal_section_pk=section_pk,
                    marker_text=marker_text,
                    kind="reject",
                    value="",
                )
                dlg.close()
                if ok:
                    ui.notify("Placeholder removed.", type="positive")
                    on_change()
                else:
                    ui.notify(
                        "Could not remove — this marker no longer exists "
                        "in the draft. Refresh to see the latest "
                        "placeholders.",
                        type="negative",
                        timeout=6000,
                    )
                    on_change()

            ui.button("Remove from draft", icon="delete_outline", on_click=apply).props("color=red-7")
    dlg.open()


def _open_save_decision_dialog(
    proposal_id: int,
    candidate: dict,
) -> None:
    """Follow-up dialog after the user resolves a placeholder with
    a value they've used N times before. Lets the user review and
    edit a suggested topic / applies-to / decision text, then save
    to data/decisions.json so the writer auto-applies the decision
    on future proposals.

    `candidate` comes from
    app.services.decision_capture.detect_decision_candidate.
    Cancel is a clean no-op — the placeholder resolution already
    landed; this dialog is purely opt-in capture.
    """
    n = int(candidate.get("n_matches") or 0)
    suggested_topic = candidate.get("suggested_topic") or ""
    suggested_applies = candidate.get("suggested_applies_to") or ""
    decision_value = candidate.get("value") or ""
    sample_markers = list(
        dict.fromkeys(m.get("marker_text") or "" for m in (candidate.get("matches") or []))
    )[:5]

    with ui.dialog() as dlg, ui.card().classes("w-[640px]"):
        ui.label("Save as cross-RFP decision?").classes("text-base font-medium")
        ui.label(
            f"You've used this answer for {n} placeholders. Saving "
            f"it as a decision in the cross-RFP ledger means the "
            f"writer will auto-apply it on this and future proposals "
            f"— fewer placeholders to action next time."
        ).classes("text-xs opacity-70 pb-2")

        # Show the matching markers so the user knows what would be
        # captured. Read-only.
        with ui.card().classes("w-full bg-slate-50 border border-slate-200"):
            ui.label(f"Matched in {n} placeholder(s):").classes("text-xs font-medium opacity-70")
            for mt in sample_markers:
                ui.label(f"  • {mt}").classes("text-xs opacity-80 font-mono")

        ui.label("Topic").classes("text-xs uppercase opacity-70 pt-3")
        topic_input = (
            ui.input(
                value=suggested_topic,
                placeholder="Project kickoff timing",
            )
            .props("outlined dense")
            .classes("w-full")
        )

        ui.label("Applies to placeholders like").classes("text-xs uppercase opacity-70 pt-2")
        applies_input = (
            ui.input(
                value=suggested_applies,
                placeholder=("kickoff date | project start | commencement"),
            )
            .props("outlined dense")
            .classes("w-full")
        )
        ui.label(
            "Pipe-separated patterns the writer agent matches against future placeholder marker_texts."
        ).classes("text-xs opacity-60")

        ui.label("Decision").classes("text-xs uppercase opacity-70 pt-2")
        decision_input = (
            ui.textarea(
                value=decision_value,
                placeholder="Within 30 days of contract award.",
            )
            .props("outlined dense autogrow")
            .classes("w-full")
        )
        ui.label(
            "The exact value the writer will auto-apply. Edit if "
            "you want a more general phrasing for future use."
        ).classes("text-xs opacity-60")

        with ui.row().classes("justify-end gap-2 pt-3 w-full"):
            ui.button("Skip", on_click=dlg.close).props("flat")

            def _save() -> None:
                topic = (topic_input.value or "").strip()
                applies = (applies_input.value or "").strip()
                decision_text = (decision_input.value or "").strip()
                if not topic:
                    ui.notify(
                        "Topic is required.",
                        type="warning",
                        timeout=3000,
                    )
                    return
                if not decision_text:
                    ui.notify(
                        "Decision text is required.",
                        type="warning",
                        timeout=3000,
                    )
                    return
                try:
                    from app.core.decisions import add_decision

                    result = add_decision(
                        topic=topic,
                        decision=decision_text,
                        applies_to_gaps_like=applies,
                        source_proposal_id=proposal_id,
                    )
                except Exception as exc:
                    ui.notify(
                        f"Save failed: {type(exc).__name__}: {exc}",
                        type="negative",
                        timeout=5000,
                    )
                    return
                dlg.close()
                if result.get("added"):
                    new_id = (result.get("decision") or {}).get("id", "?")
                    ui.notify(
                        f"Saved as {new_id}. The writer will "
                        f"auto-apply this decision on future "
                        f"matching placeholders.",
                        type="positive",
                        multi_line=True,
                        timeout=5000,
                    )
                else:
                    reason = result.get("reason") or "unknown"
                    ui.notify(
                        f"Not saved: {reason}.",
                        type="warning",
                        timeout=4000,
                    )

            ui.button(
                "Save Decision",
                icon="bookmark_added",
                on_click=_save,
            ).props("color=primary")
    dlg.open()


def _render_placeholder_action_card(
    proposal_id: int,
    section_pk: int,
    ph: dict,
    on_change,
    *,
    anchor_id: str | None = None,
) -> None:
    """One actionable card per placeholder. Resolved cards render compactly
    with the resolution; unresolved cards have Sign / Provide value / Remove
    buttons.

    `anchor_id` (when provided) is set as the card's HTML id so the
    clickable marker in the rendered markdown can scroll directly to it."""
    resolved = bool(ph.get("resolved"))
    marker_text = ph.get("marker_text", "")
    description = ph.get("description", "")
    category = ph.get("category", "other")
    cat_label = _NEEDS_HUMAN_CATEGORY_LABELS.get(category, category)

    if resolved:
        card_cls = "w-full bg-green-50 border-l-4 border-green-400"
    else:
        card_cls = "w-full bg-white border-l-4 border-amber-400"

    card = ui.card().classes(card_cls)
    if anchor_id:
        # `:target` styling makes the card flash when the user lands on it
        # via the marker link — handy on long sections.
        card.props(f'id="{anchor_id}"')
    with card:
        with ui.row().classes("items-start gap-3 w-full"):
            if resolved:
                ui.icon("check_circle").classes("text-green-700 text-lg pt-0.5")
            else:
                ui.icon("pending_actions").classes("text-amber-700 text-lg pt-0.5")

            with ui.column().classes("gap-0 flex-1"):
                # Marker text = the question itself. Render in normal prose
                # font (it's English, not code) with a strong but not loud
                # weight. The card's left-border accent already conveys
                # status — the text doesn't need to scream.
                ui.label(marker_text).classes(
                    "text-sm font-medium leading-relaxed break-words "
                    "whitespace-pre-wrap " + ("text-slate-700" if resolved else "text-slate-900")
                )
                with ui.row().classes("items-center gap-2 flex-wrap pt-1"):
                    ui.chip(cat_label).props(
                        f"dense color={'green-2' if resolved else 'amber-2'} "
                        f"text-color={'green-9' if resolved else 'amber-9'}"
                    ).classes("text-xs")
                ui.label(description).classes("text-xs text-slate-600 pt-1 leading-relaxed")
                if resolved:
                    res_kind = ph.get("resolution_kind", "")
                    res_val = ph.get("resolution_value", "")
                    label_for_kind = {
                        "edit": "Resolved (edit)",
                        "signature": "Signed",
                        "reject": "Removed",
                    }.get(res_kind, "Resolved")
                    if res_kind == "reject" or not res_val:
                        ui.label(f"{label_for_kind} — marker deleted from draft.").classes(
                            "text-xs text-green-800 pt-2 italic"
                        )
                    else:
                        ui.label(label_for_kind).classes(
                            "text-[10px] font-semibold tracking-wider text-green-800 uppercase pt-2"
                        )
                        with ui.card().classes(
                            "w-full bg-white border-l-4 border-green-300 shadow-none mt-1"
                        ):
                            ui.label(res_val).classes(
                                "text-sm text-slate-800 leading-relaxed break-words whitespace-pre-wrap"
                            )

            if not resolved:
                with ui.column().classes("gap-1 items-stretch"):
                    if _is_signature_placeholder(ph):
                        ui.button(
                            "Sign",
                            icon="draw",
                            on_click=lambda mt=marker_text, dsc=description: _open_signature_dialog(
                                section_pk, mt, dsc, on_change
                            ),
                        ).props("color=primary dense size=sm")
                    ui.button(
                        "Provide value",
                        icon="edit",
                        on_click=lambda mt=marker_text, dsc=description, cat=category: (
                            _open_provide_value_dialog(
                                proposal_id,
                                section_pk,
                                mt,
                                dsc,
                                cat,
                                on_change,
                            )
                        ),
                    ).props(
                        f"{'outline' if _is_signature_placeholder(ph) else 'color=primary'} dense size=sm"
                    )
                    ui.button(
                        "Remove",
                        icon="delete_outline",
                        on_click=lambda mt=marker_text, dsc=description: _open_remove_placeholder_dialog(
                            section_pk, mt, dsc, on_change
                        ),
                    ).props("flat dense size=sm color=red-7")


def _render_section_action_panel(
    proposal_id: int, section_pk: int, placeholders: list[dict], on_change
) -> None:
    """Pinned panel below a section's draft body. Lists every placeholder for
    that section as an action card — resolved ones compact, unresolved ones
    with full buttons. Replaces the old read-only listing."""
    if not placeholders:
        return
    unresolved = [p for p in placeholders if not p.get("resolved")]
    resolved = [p for p in placeholders if p.get("resolved")]

    panel_cls = (
        "w-full bg-amber-50 border-l-4 border-amber-500 mt-2"
        if unresolved
        else "w-full bg-green-50 border-l-4 border-green-500 mt-2"
    )
    with ui.card().classes(panel_cls):
        with ui.row().classes("items-center justify-between w-full"):
            if unresolved:
                ui.label(
                    f"⚠ Action required: {len(unresolved)} placeholder"
                    f"{'s' if len(unresolved) != 1 else ''} need your input"
                ).classes("text-sm font-semibold text-amber-900")
            else:
                ui.label(
                    f"✓ All {len(resolved)} placeholder{'s' if len(resolved) != 1 else ''} resolved"
                ).classes("text-sm font-semibold text-green-900")
            if resolved and unresolved:
                ui.label(f"{len(resolved)} resolved").classes("text-xs opacity-70")
        for idx, ph in enumerate(unresolved):
            _render_placeholder_action_card(
                proposal_id,
                section_pk,
                ph,
                on_change,
                anchor_id=f"nh-{section_pk}-{idx}",
            )
        if resolved:
            with ui.expansion(
                f"{len(resolved)} resolved placeholder{'s' if len(resolved) != 1 else ''}",
                icon="check_circle",
            ).classes("w-full"):
                for ph in resolved:
                    _render_placeholder_action_card(proposal_id, section_pk, ph, on_change)


_REFINE_SUGGESTION_CHIPS = [
    "Make this section more concise",
    "Expand with more concrete detail",
    "Strengthen the case for custom build",
    "Match the RFP's evaluator language more closely",
    "Lead with our strongest past-performance example",
    "Tighten the prose — remove filler",
]


def _open_refine_dialog(proposal_id: int, section_pk: int, section_label: str) -> None:
    """Per-section AI-refine dialog. User describes the change they want,
    submit triggers a regenerate of just this section with the directive
    forwarded to the Writer Team agent."""
    with ui.dialog() as dlg, ui.card().classes("min-w-[32rem] max-w-[48rem]"):
        ui.label("Refine section with AI").classes("text-base font-semibold")
        ui.label(section_label).classes("text-xs opacity-70")
        ui.label(
            "Describe the change you want. The Writer Team will redraft this "
            "section with your directive applied, preserving all honesty rules, "
            "citation requirements, and compliance assignments."
        ).classes("text-sm opacity-80 pt-2")

        directive_input = (
            ui.textarea(
                placeholder=(
                    "e.g., Make the technical approach more concise and lead with "
                    "the custom-build positioning. Move the SLA paragraph above "
                    "the architecture overview."
                ),
            )
            .classes("w-full")
            .props("autogrow rows=4 outlined")
        )

        ui.label("Quick fill:").classes("text-xs opacity-70 pt-2")
        with ui.row().classes("flex-wrap gap-1 pt-1"):
            for chip_text in _REFINE_SUGGESTION_CHIPS:
                ui.chip(
                    chip_text,
                    on_click=lambda txt=chip_text: directive_input.set_value(txt),
                ).props("clickable dense color=blue-1 text-color=blue-9").classes("text-xs cursor-pointer")

        ui.label(
            "Tip: be specific about WHAT you want changed and WHY. The agent "
            "treats this as binding revision guidance, but won't violate "
            "honesty rules — if the directive conflicts with one (e.g. "
            "'claim FedRAMP High'), the conflict surfaces as a [NEEDS_HUMAN] "
            "placeholder explaining why."
        ).classes("text-xs opacity-60 pt-2")

        with ui.row().classes("w-full justify-end gap-2 pt-3"):
            ui.button("Cancel", on_click=dlg.close).props("flat")

            def apply() -> None:
                d = (directive_input.value or "").strip()
                if not d:
                    ui.notify("Type a change you want first.", type="warning")
                    return
                dlg.close()
                _refine_section_with_ai(proposal_id, section_pk, d)

            ui.button("Refine", icon="auto_awesome", on_click=apply).props("color=primary")
    dlg.open()


def _render_draft_tab(
    proposal_id: int,
    status_val: str,
    *,
    on_state_change=None,
) -> None:
    """Draft tab — renders each ProposalSection.draft_text_markdown plus
    citations and inline [NEEDS_HUMAN] placeholders. Per-section toolbar:
    Edit (inline markdown editor) · Refine with AI (directive dialog) ·
    Regenerate (full re-run of the Writer for that section).

    Refresh-on-poll: while status=draft_in_progress, polls every 5s so
    sections appear as the writer finishes them.
    """
    # Edit-mode is owned per-tab; only one section can be in edit mode at a
    # time so Save/Cancel state stays unambiguous.
    edit_state: dict = {"editing_pk": None}

    def _after_change() -> None:
        render.refresh()
        if on_state_change is not None:
            on_state_change()

    def _start_edit(pk: int) -> None:
        edit_state["editing_pk"] = pk
        render.refresh()

    def _cancel_edit() -> None:
        edit_state["editing_pk"] = None
        render.refresh()

    def _save_edit(pk: int, new_text: str) -> None:
        ok = save_manual_edit(pk, new_text or "")
        edit_state["editing_pk"] = None
        if ok:
            ui.notify("Edits saved.", type="positive")
        else:
            ui.notify("Save failed — section not found.", type="negative")
        # _after_change rather than render.refresh — saving a manual edit
        # can change which sections count as "drafted" (Draft badge) and
        # placeholder reconciliation can flip resolved counts (Needs
        # Human badge).
        _after_change()

    @ui.refreshable
    def render() -> None:
        # Defensive reconcile — guarantee every inline [NEEDS_HUMAN: …] marker
        # has a JSON entry before we snapshot. Cheap (regex over the draft);
        # only commits if anything actually changed.
        with SessionLocal() as db:
            sec_pks = [
                row[0]
                for row in db.execute(
                    select(ProposalSection.id).where(ProposalSection.proposal_id == proposal_id)
                ).all()
            ]
        for spk in sec_pks:
            try:
                reconcile_placeholders(spk)
            except Exception:
                log.exception("reconcile_placeholders failed for section pk=%d", spk)

        with SessionLocal() as db:
            sec_rows = (
                db.execute(
                    select(ProposalSection)
                    .where(ProposalSection.proposal_id == proposal_id)
                    .order_by(ProposalSection.section_order, ProposalSection.id)
                )
                .scalars()
                .all()
            )
            sections = [
                {
                    "pk": s.id,
                    "section_id": s.section_id,
                    "section_title": s.section_title,
                    "section_order": s.section_order,
                    "section_brief": s.section_brief or "",
                    "page_limit": s.page_limit,
                    "word_limit": s.word_limit,
                    "requires_cost_analysis": bool(s.requires_cost_analysis),
                    "draft_md": s.draft_text_markdown,
                    "citations": list(s.citations_json or []),
                    "needs_human": list(s.needs_human_placeholders_json or []),
                    "applied_gaps": list(s.shortfall_mitigations_applied_json or []),
                    "revision": s.current_revision_number or 0,
                    "compliance_drift_pending": bool(s.compliance_drift_pending),
                }
                for s in sec_rows
            ]
            # Re-read live status to show progress while drafting.
            p = db.get(Proposal, proposal_id)
            live_status = (
                p.status.value if p and hasattr(p.status, "value") else (str(p.status) if p else status_val)
            )

        if not sections:
            _empty_state(
                "No draft yet — generate the outline first, then approve to begin drafting.",
                icon="article",
            )
            return

        n_total = len(sections)
        n_cost_deferred = sum(1 for s in sections if s["requires_cost_analysis"])
        n_writer_eligible = n_total - n_cost_deferred
        n_drafted = sum(1 for s in sections if s["draft_md"] and not s["requires_cost_analysis"])
        n_missing = max(0, n_writer_eligible - n_drafted)
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.column().classes("gap-0"):
                    ui.label(f"{n_drafted} of {n_writer_eligible} section(s) drafted").classes(
                        "text-base font-semibold"
                    )
                    if n_cost_deferred:
                        ui.label(
                            f"+ {n_cost_deferred} cost-deferred section"
                            f"{'s' if n_cost_deferred != 1 else ''} "
                            f"(awaiting Cost Analysis Agent, Weeks 12-13)"
                        ).classes("text-xs opacity-70")
                if live_status == "draft_in_progress":
                    # Writer Team is currently running. Show the live
                    # spinner and a "Force restart" escape hatch — the
                    # most common reason someone wants to re-run the
                    # writer is that a prior run got killed (terminal
                    # closed, app crashed) and the status is stale at
                    # DRAFT_IN_PROGRESS but no thread is actually
                    # working. The escape hatch flips status back and
                    # respawns; the writer's existing resume logic
                    # skips sections that already have a draft.
                    with ui.row().classes("items-center gap-2"):
                        ui.spinner("dots", color="primary")
                        ui.label("Writer Team drafting now").classes("text-sm opacity-70")
                        ui.button(
                            "Force restart",
                            icon="restart_alt",
                            on_click=lambda: _force_restart_writer_team(
                                proposal_id,
                                on_change=_after_change,
                            ),
                        ).props("flat color=warning size=sm").tooltip(
                            "Use if drafting actually crashed and the "
                            "spinner is stuck. Resets the status and "
                            "respawns; already-drafted sections are "
                            "preserved (resume mode)."
                        )
                elif n_missing > 0 and live_status in (
                    "awaiting_draft",
                    "draft_ready",
                    "reviewing",
                ):
                    # Missing sections after a writer run finished —
                    # most often the writer was interrupted before
                    # every section landed. Resume drafts only the
                    # missing ones; existing drafts are preserved.
                    ui.button(
                        f"Draft {n_missing} missing section{'s' if n_missing != 1 else ''}",
                        icon="auto_stories",
                        on_click=lambda: _begin_drafting(proposal_id),
                    ).props("color=primary").tooltip(
                        "Re-runs the Writer Team. Already-drafted "
                        "sections are skipped — only missing ones "
                        "get drafted."
                    )

        # Pending [NEEDS_HUMAN] placeholders across all sections.
        # Mirrors the red badge on the Draft tab — surfaces the count
        # at the top of the tab so the user doesn't have to scroll
        # through every section to find the work.
        sections_with_pending: list[tuple[dict, int]] = []
        n_pending_total = 0
        for s in sections:
            n_section_pending = sum(1 for ph in s["needs_human"] if not ph.get("resolved"))
            if n_section_pending:
                sections_with_pending.append((s, n_section_pending))
                n_pending_total += n_section_pending

        if n_pending_total:
            with ui.card().classes("w-full bg-amber-50 border-l-4 border-amber-500"):
                with ui.row().classes("items-start gap-3 w-full"):
                    ui.icon("warning").classes("text-amber-700 text-2xl pt-0.5")
                    with ui.column().classes("gap-1 flex-1"):
                        ui.label(
                            f"⚠ {n_pending_total} action item"
                            f"{'s' if n_pending_total != 1 else ''} "
                            f"need your input"
                        ).classes("text-base font-semibold text-amber-900")
                        ui.label(
                            "Resolve the [NEEDS_HUMAN] placeholders "
                            "inline below — Sign, Provide value, or "
                            "Remove from each yellow card."
                        ).classes("text-sm text-amber-800")
                        with ui.row().classes("flex-wrap gap-1 pt-1"):
                            for sec, n_sec in sections_with_pending:
                                anchor = f"#nh-{sec['pk']}-0"
                                label = f"{sec['section_id']}: {n_sec} pending"
                                ui.html(
                                    f'<a href="{anchor}" class="text-xs '
                                    f"px-2 py-0.5 rounded bg-amber-200 "
                                    f"text-amber-900 hover:bg-amber-300 "
                                    f'no-underline">{label}</a>'
                                )

        for s in sections:
            is_editing = edit_state["editing_pk"] == s["pk"]
            is_cost_deferred = s["requires_cost_analysis"]
            with ui.card().classes("w-full"):
                # Section header
                with ui.row().classes("items-start justify-between w-full"):
                    with ui.column().classes("gap-0"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(f"#{s['section_order']}").classes("text-xs font-mono opacity-60")
                            ui.label(s["section_id"]).classes(
                                "text-xs font-mono px-1.5 py-0.5 bg-slate-100 rounded"
                            )
                            ui.label(s["section_title"]).classes("text-lg font-semibold")
                            if s["draft_md"]:
                                ui.chip(
                                    f"rev {s['revision']}",
                                    icon="history",
                                ).props("dense color=slate-2")
                            if is_cost_deferred:
                                ui.chip(
                                    "Cost section",
                                    icon="payments",
                                ).props("dense color=purple-2 text-color=purple-9").tooltip(
                                    "Drafted by the Cost Writer after "
                                    "pricing is built — not by the regular "
                                    "Writer Team."
                                )
                            if is_editing:
                                ui.chip("Editing", icon="edit").props(
                                    "dense color=amber-2 text-color=amber-9"
                                )
                            if s.get("compliance_drift_pending"):
                                ui.chip(
                                    "Stale — compliance changed since draft",
                                    icon="update",
                                ).props("dense color=amber-3 text-color=black").tooltip(
                                    "A recent amendment changed a "
                                    "requirement this section addresses. "
                                    "Re-draft to pick up the new text."
                                )

                    # Drift "Re-draft" button — rendered next to the
                    # chip in its own row so a user with an amendment
                    # in flight can re-draft in one click. Only when
                    # the section is not cost-deferred / not being
                    # edited (the chip + button only make sense for
                    # writer-eligible sections).
                    if s.get("compliance_drift_pending") and not is_editing and not is_cost_deferred:
                        with ui.row().classes("gap-1"):

                            def _redraft(pk=s["pk"]):
                                spawn_writer_for_section(proposal_id, pk)
                                ui.notify(
                                    "Re-drafting this section to reflect the latest compliance matrix.",
                                    type="positive",
                                )
                                if on_state_change is not None:
                                    on_state_change()

                            ui.button(
                                "Re-draft this section",
                                icon="refresh",
                                on_click=_redraft,
                            ).props("dense size=sm color=amber-8")

                    # Toolbar — hidden while editing (Save/Cancel live below the textarea).
                    # Also hidden on cost-deferred sections since they're not
                    # drafted by the Writer Team.
                    if not is_editing and not is_cost_deferred:
                        with ui.row().classes("gap-1"):
                            if s["draft_md"]:
                                ui.button(
                                    "Edit",
                                    icon="edit",
                                    on_click=(lambda pk=s["pk"]: _start_edit(pk)),
                                ).props("flat dense size=sm")
                                ui.button(
                                    "Refine with AI",
                                    icon="auto_awesome",
                                    on_click=(
                                        lambda pk=s["pk"], lbl=(f"#{s['section_order']} {s['section_id']} — {s['section_title']}"): (
                                            _open_refine_dialog(proposal_id, pk, lbl)
                                        )
                                    ),
                                ).props("flat dense size=sm")
                                ui.button(
                                    "Regenerate",
                                    icon="refresh",
                                    on_click=(lambda pk=s["pk"]: _regenerate_section(proposal_id, pk)),
                                ).props("flat dense size=sm")

                if is_cost_deferred and not s["draft_md"]:
                    with ui.card().classes("w-full bg-purple-50 border-l-4 border-purple-400 mt-2"):
                        with ui.row().classes("items-start gap-3 w-full"):
                            ui.icon("payments").classes("text-purple-700 text-2xl pt-0.5")
                            with ui.column().classes("gap-0 flex-1"):
                                ui.label("Awaiting Cost Analysis Agent (Weeks 12-13)").classes(
                                    "text-sm font-semibold text-purple-900"
                                )
                                ui.label(
                                    "This section will be drafted after the "
                                    "Cost Analysis Agent produces the pricing "
                                    "numbers and P&L. Until then it stays empty "
                                    "by design — no point in pasting "
                                    "[NEEDS_HUMAN: $X] placeholders the agent "
                                    "would have to overwrite."
                                ).classes("text-xs text-purple-800 pt-1")
                    continue

                if not s["draft_md"]:
                    if live_status == "draft_in_progress":
                        with ui.row().classes("items-center gap-2 py-3 opacity-70"):
                            ui.spinner("dots", color="primary")
                            ui.label("Drafting…").classes("text-sm")
                    else:
                        ui.label("Not yet drafted. Run the Writer Team to draft this section.").classes(
                            "text-sm opacity-60 pt-2"
                        )
                    continue

                # ---- Edit mode ------------------------------------------
                if is_editing:
                    ui.label(
                        "Editing markdown source. [^cite-N] markers and "
                        "[NEEDS_HUMAN: …] placeholders are active text — "
                        "preserve or remove them deliberately. Removing a "
                        "[NEEDS_HUMAN] marker auto-marks it resolved on Save."
                    ).classes("text-xs opacity-70 pt-2")
                    edit_ta = (
                        ui.textarea(value=s["draft_md"])
                        .classes("w-full font-mono")
                        .props("autogrow rows=20 outlined")
                    )
                    with ui.row().classes("w-full justify-end gap-2 pt-2"):
                        ui.button(
                            "Cancel",
                            on_click=lambda: _cancel_edit(),
                        ).props("flat")
                        ui.button(
                            "Save changes",
                            icon="save",
                            on_click=(lambda pk=s["pk"], ta=edit_ta: _save_edit(pk, ta.value)),
                        ).props("color=primary")
                    continue

                # ---- View mode ------------------------------------------
                # Action panel above the prose — every inline marker has a
                # corresponding card here. Marker text in the prose is also
                # an anchor link that scrolls to its card.
                _render_section_action_panel(
                    proposal_id=proposal_id,
                    section_pk=s["pk"],
                    placeholders=s["needs_human"],
                    on_change=_after_change,
                )

                # Draft body — strip [^cite-N] for display, then turn each
                # unresolved [NEEDS_HUMAN: …] into a markdown anchor link
                # that scrolls to the matching action card above.
                display_md = _strip_cite_markers_for_display(s["draft_md"])
                display_md = _highlight_and_link_placeholders(
                    display_md,
                    s["needs_human"],
                    s["pk"],
                )
                ui.markdown(display_md).classes("w-full prose prose-sm max-w-none pt-2")

                # Citations — collapsible, below the action panel since the
                # placeholders are the primary action surface.
                if s["citations"]:
                    with ui.expansion(f"Citations ({len(s['citations'])})", icon="format_quote").classes(
                        "w-full"
                    ):
                        for c in s["citations"]:
                            marker = c.get("marker", "?")
                            claim = c.get("claim", "")
                            src = c.get("source_kb_doc", "")
                            src_sec = c.get("source_section") or ""
                            conf = c.get("confidence", "")
                            conf_color = {
                                "HIGH": "text-green-700",
                                "MEDIUM": "text-amber-700",
                                "LOW": "text-red-700",
                            }.get(conf, "text-slate-600")
                            with ui.row().classes("items-start gap-2 w-full py-1 border-b border-slate-100"):
                                ui.label(f"[^{marker}]").classes("text-xs font-mono opacity-70 w-20")
                                with ui.column().classes("gap-0 flex-1"):
                                    ui.label(claim).classes("text-sm")
                                    src_str = src + (f" — {src_sec}" if src_sec else "")
                                    ui.label(src_str).classes("text-xs opacity-70 italic")
                                if conf:
                                    ui.label(conf).classes(f"text-xs font-mono {conf_color}")

                # Gap mitigations applied
                if s["applied_gaps"]:
                    with ui.row().classes("flex-wrap gap-1 pt-1 items-center"):
                        ui.label("Applied mitigations:").classes("text-xs opacity-60 mr-1")
                        for gid in s["applied_gaps"]:
                            ui.chip(gid).props("dense color=blue-1 text-color=blue-9").classes("text-xs")

    render()

    # Live polling while the writer is running so completed sections appear.
    def maybe_refresh() -> None:
        with SessionLocal() as db:
            p = db.get(Proposal, proposal_id)
            live = p.status.value if p and hasattr(p.status, "value") else (str(p.status) if p else "")
        if live == "draft_in_progress":
            render.refresh()

    ui.timer(5.0, maybe_refresh)


# ---- Reviewer Findings tab ------------------------------------------------

# Severity → visual styling for finding cards.
