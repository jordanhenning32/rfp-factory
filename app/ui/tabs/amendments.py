"""Proposal Review > Amendments & Q&A tab.

Upload row (Amendment / Q&A) + timeline + "Latest delta" panel. The
upload handlers mirror the F1 SHA-256 dedup flow from the New Proposal
page; the timeline calls list_amendments(); the latest-delta panel
reads the most recent COMPLETED AmendmentRun's report_json.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nicegui import ui

from app.db.session import session_scope
from app.jobs.amendment import spawn_amendment_ingestion
from app.models import AmendmentRun
from app.services.amendments import (
    attach_amendment_to_proposal,
    list_amendments,
)
from app.services.proposals import (
    MAX_PROPOSAL_FILE_BYTES,
    MAX_PROPOSAL_PACKAGE_BYTES,
    UploadedFile,
    find_duplicate_rfp_documents,
)
from app.ui._shared import _empty_state

log = logging.getLogger(__name__)


# Quasar palette colors per status. Mirrors the compliance-tab convention.
_STATUS_CHIP_COLOR = {
    "running": "blue-3",
    "completed": "green-3",
    "failed": "red-3",
    None: "blue-grey-3",
}


def _render_amendments_tab(
    proposal_id: int,
    *,
    on_state_change: Any = None,
) -> None:
    """Render the Amendments & Q&A tab body.

    Args:
        proposal_id: the proposal whose amendments are shown.
        on_state_change: optional callable to refresh outer chrome (tab
            badges, banners) when the timeline state changes.
    """

    # ── Upload row ──────────────────────────────────────────────────
    # The two pickers stage files in module-local dicts; the user clicks
    # "Upload" to actually persist. This mirrors the New Proposal page's
    # staging-then-confirm pattern so the F1 dedup dialog has a chance
    # to interpose between the click and the write.
    staged_amendment: dict[str, bytes] = {}
    staged_qa: dict[str, bytes] = {}

    def _notify_upload_rejected() -> None:
        ui.notify(
            "Upload rejected: files must be 50 MB or less each and 200 MB or less total.",
            type="negative",
        )

    def _persist_and_spawn(
        files: list[UploadedFile],
        role: str,
        sequence_number: int | None,
    ) -> int:
        """Write files + spawn ingestion threads. Returns count persisted."""
        if not files:
            return 0
        try:
            new_doc_ids: list[int] = []
            with session_scope() as db:
                new_docs = attach_amendment_to_proposal(
                    proposal_id=proposal_id,
                    files=files,
                    document_role=role,
                    sequence_number=sequence_number,
                    db=db,
                )
                new_doc_ids = [d.id for d in new_docs]
            for did in new_doc_ids:
                spawn_amendment_ingestion(proposal_id, did)
            return len(new_doc_ids)
        except Exception as exc:
            log.exception(
                "amendments tab: upload+spawn failed for proposal=%d role=%s",
                proposal_id,
                role,
            )
            ui.notify(f"Upload failed: {exc}", type="negative")
            return 0

    def _upload_with_dedup_dialog(
        files: list[UploadedFile],
        role: str,
        sequence_number: int | None,
    ) -> None:
        """Run the SHA-256 dedup check; open the Cancel / Skip / Upload-anyway
        dialog if any of the candidate files match existing RFP package
        documents. Otherwise persist immediately."""
        if not files:
            ui.notify("Stage at least one file first.", type="warning")
            return
        with session_scope() as db:
            dups = find_duplicate_rfp_documents(db, files)
            dup_info: dict[str, dict] = {}
            for n, doc in dups.items():
                dup_info[n] = {"id": doc.id, "filename": doc.filename}

        def _do_upload(skip_filenames: set[str] | None = None) -> None:
            skip = skip_filenames or set()
            keep = [f for f in files if f.filename not in skip]
            if not keep:
                ui.notify(
                    "All staged files matched existing uploads — skipped.",
                    type="info",
                )
                return
            n = _persist_and_spawn(keep, role, sequence_number)
            if n > 0:
                ui.notify(
                    f"Queued {n} file(s) for amendment ingestion. Watch this tab for status.",
                    type="positive",
                )
                if role == "amendment":
                    staged_amendment.clear()
                else:
                    staged_qa.clear()
                if on_state_change is not None:
                    on_state_change()
                ui.timer(0.5, lambda: ui.navigate.reload(), once=True)

        if not dup_info:
            _do_upload()
            return

        with ui.dialog() as dlg, ui.card().classes("max-w-2xl"):
            n_dups = len(dup_info)
            ui.label(
                f"{n_dups} of these file(s) match existing RFP / amendment files (same content):"
            ).classes("text-base font-medium")
            with ui.column().classes("gap-1 pt-2"):
                for new_name, existing in dup_info.items():
                    ui.label(
                        f"• {new_name}  →  matches existing file #{existing['id']} ({existing['filename']})"
                    ).classes("text-sm")
            ui.label(
                "Recommended: skip the duplicates so you don't re-process "
                "the same amendment. Upload anyway if the file's content "
                "actually changed."
            ).classes("text-xs opacity-60 pt-2")

            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                ui.button("Cancel", on_click=dlg.close).props("flat")

                def skip_dups() -> None:
                    dlg.close()
                    _do_upload(skip_filenames=set(dup_info.keys()))

                def upload_anyway() -> None:
                    dlg.close()
                    _do_upload()

                ui.button(
                    "Skip duplicates",
                    icon="skip_next",
                    on_click=skip_dups,
                ).props("color=primary")
                ui.button(
                    "Upload anyway",
                    icon="warning_amber",
                    on_click=upload_anyway,
                ).props("flat color=amber-9")
        dlg.open()

    with ui.row().classes("w-full gap-4 items-start"):
        # ── Amendment upload (left half) ──────────────────────────
        with ui.card().classes("flex-1"):
            ui.label("Upload Amendment").classes("text-base font-semibold")
            ui.label(
                "Each amendment carries a buyer-assigned sequence number (e.g. Amendment 0001)."
            ).classes("text-xs opacity-60")

            seq_in = ui.number(
                "Sequence number (e.g. 1)",
                value=1,
                min=1,
                precision=0,
            ).classes("w-48")

            async def on_amendment_upload(e) -> None:
                try:
                    data = await e.file.read()
                    staged_amendment[e.file.name] = data
                    ui.notify(
                        f"Staged amendment {e.file.name} ({len(data) / 1024:.1f} KB)",
                        type="positive",
                    )
                except Exception as exc:
                    log.exception("amendment upload handler failed")
                    ui.notify(f"Upload failed: {exc}", type="negative")

            ui.upload(
                multiple=True,
                max_file_size=MAX_PROPOSAL_FILE_BYTES,
                max_total_size=MAX_PROPOSAL_PACKAGE_BYTES,
                auto_upload=True,
                on_upload=on_amendment_upload,
                on_rejected=_notify_upload_rejected,
                label="Drop Amendment files here",
            ).props("accept=.pdf,.docx,.xlsx").classes("w-full")

            def on_upload_amendment_click() -> None:
                if not staged_amendment:
                    ui.notify(
                        "Stage at least one amendment file first.",
                        type="warning",
                    )
                    return
                try:
                    seq = int(seq_in.value) if seq_in.value is not None else None
                except (TypeError, ValueError):
                    ui.notify(
                        "Sequence number must be a positive integer.",
                        type="warning",
                    )
                    return
                files = [UploadedFile(filename=n, content=d) for n, d in staged_amendment.items()]
                _upload_with_dedup_dialog(files, "amendment", seq)

            ui.button(
                "Upload amendment",
                icon="upload",
                on_click=on_upload_amendment_click,
            ).props("color=primary").classes("mt-2")

        # ── Q&A upload (right half) ───────────────────────────────
        with ui.card().classes("flex-1"):
            ui.label("Upload Q&A Response").classes("text-base font-semibold")
            ui.label("Q&A responses have no sequence number — they're processed in upload order.").classes(
                "text-xs opacity-60"
            )

            async def on_qa_upload(e) -> None:
                try:
                    data = await e.file.read()
                    staged_qa[e.file.name] = data
                    ui.notify(
                        f"Staged Q&A {e.file.name} ({len(data) / 1024:.1f} KB)",
                        type="positive",
                    )
                except Exception as exc:
                    log.exception("Q&A upload handler failed")
                    ui.notify(f"Upload failed: {exc}", type="negative")

            ui.upload(
                multiple=True,
                max_file_size=MAX_PROPOSAL_FILE_BYTES,
                max_total_size=MAX_PROPOSAL_PACKAGE_BYTES,
                auto_upload=True,
                on_upload=on_qa_upload,
                on_rejected=_notify_upload_rejected,
                label="Drop Q&A files here",
            ).props("accept=.pdf,.docx,.xlsx").classes("w-full")

            def on_upload_qa_click() -> None:
                if not staged_qa:
                    ui.notify(
                        "Stage at least one Q&A file first.",
                        type="warning",
                    )
                    return
                files = [UploadedFile(filename=n, content=d) for n, d in staged_qa.items()]
                _upload_with_dedup_dialog(files, "qa_response", None)

            ui.button(
                "Upload Q&A",
                icon="upload",
                on_click=on_upload_qa_click,
            ).props("color=primary").classes("mt-2")

    # ── Timeline + latest delta ─────────────────────────────────────
    with session_scope() as db:
        amendments = list_amendments(proposal_id, db)
        latest_completed = (
            db.query(AmendmentRun)
            .filter(
                AmendmentRun.proposal_id == proposal_id,
                AmendmentRun.status == "completed",
            )
            .order_by(AmendmentRun.completed_at.desc())
            .first()
        )
        latest_report_json = latest_completed.report_json if latest_completed is not None else None

    if not amendments:
        _empty_state(
            "No amendments uploaded yet. Use the upload buttons above to attach Amendment XXXX or Q&A files.",
            icon="fact_check",
        )
        return

    # Latest delta panel
    ui.label("Latest delta").classes("text-base font-semibold mt-6")
    if latest_report_json:
        try:
            report = json.loads(latest_report_json)
        except json.JSONDecodeError:
            report = {}
    else:
        report = {}

    n_new = int(report.get("n_new", 0) or 0)
    n_modified = int(report.get("n_modified", 0) or 0)
    n_removed = int(report.get("n_removed", 0) or 0)
    stale_sections = list(report.get("sections_marked_stale", []) or [])

    if latest_report_json:
        with ui.row().classes("flex-wrap gap-2"):
            ui.chip(f"new: {n_new}").props("color=green-3 text-color=black")
            ui.chip(f"modified: {n_modified}").props("color=amber-3 text-color=black")
            ui.chip(f"removed: {n_removed}").props("color=red-3 text-color=black")
            ui.chip(f"sections flagged: {len(stale_sections)}").props("color=blue-3 text-color=black")
        if stale_sections:
            with ui.row().classes("flex-wrap gap-1 mt-2"):
                ui.label("Stale sections:").classes("text-xs opacity-60 self-center")
                for sid in stale_sections:
                    ui.chip(sid).props("color=amber-3 text-color=black dense")
    else:
        ui.label(
            "(No completed amendment run yet — uploads in progress will appear in the timeline below.)"
        ).classes("text-sm opacity-60")

    # Timeline
    ui.label("Timeline").classes("text-base font-semibold mt-6")
    for a in amendments:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                ui.label(a["filename"]).classes("text-sm font-medium")
                role_color = "purple-3" if a["document_role"] == "amendment" else "teal-3"
                ui.chip(a["document_role"] or "?").props(f"color={role_color} text-color=black dense")
                if a.get("sequence_number") is not None:
                    ui.chip(f"seq #{a['sequence_number']}").props("color=blue-grey-3 text-color=black dense")
                status = a.get("latest_run_status")
                status_color = _STATUS_CHIP_COLOR.get(status, "blue-grey-3")
                ui.chip(status or "pending").props(f"color={status_color} text-color=black dense")
                if a.get("uploaded_at"):
                    ui.label(a["uploaded_at"].strftime("%Y-%m-%d %H:%M")).classes(
                        "text-xs opacity-60 ml-auto"
                    )

                if status == "failed":

                    def _rerun(doc_id=a["id"]):
                        try:
                            spawn_amendment_ingestion(proposal_id, doc_id)
                            ui.notify(
                                "Re-running amendment ingestion.",
                                type="positive",
                            )
                            ui.timer(0.5, lambda: ui.navigate.reload(), once=True)
                        except Exception as exc:
                            log.exception("amendments tab: re-run failed")
                            ui.notify(f"Re-run failed: {exc}", type="negative")

                    ui.button(
                        "Re-run",
                        icon="refresh",
                        on_click=_rerun,
                    ).props("flat dense color=primary")
