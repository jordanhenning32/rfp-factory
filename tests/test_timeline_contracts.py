from __future__ import annotations

import json
from datetime import UTC, date, datetime


class _FakeElement:
    def __init__(self, *, value="") -> None:
        self.value = value
        self.opened = False
        self.closed = False

    def classes(self, *_args, **_kwargs):
        return self

    def props(self, *_args, **_kwargs):
        return self

    def style(self, *_args, **_kwargs):
        return self

    def tooltip(self, *_args, **_kwargs):
        return self

    def set_value(self, value):
        self.value = value
        return self

    def open(self):
        self.opened = True
        return self

    def close(self):
        self.closed = True
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeUi:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.buttons: list[dict] = []
        self.dialogs: list[_FakeElement] = []
        self.notifications: list[tuple[str, str | None]] = []

    def label(self, text="", **_kwargs):
        self.labels.append(str(text))
        return _FakeElement()

    def button(self, *args, **kwargs):
        self.buttons.append({"args": args, **kwargs})
        return _FakeElement()

    def input(self, *_args, **kwargs):
        return _FakeElement(value=kwargs.get("value", ""))

    def number(self, *_args, **kwargs):
        return _FakeElement(value=kwargs.get("value", ""))

    def textarea(self, *_args, **kwargs):
        return _FakeElement(value=kwargs.get("value", ""))

    def notify(self, message, *, type=None, **_kwargs):
        self.notifications.append((str(message), type))

    def dialog(self, *_args, **_kwargs):
        dialog = _FakeElement()
        self.dialogs.append(dialog)
        return dialog

    def card(self, *_args, **_kwargs):
        return _FakeElement()

    def row(self, *_args, **_kwargs):
        return _FakeElement()

    def column(self, *_args, **_kwargs):
        return _FakeElement()

    def element(self, *_args, **_kwargs):
        return _FakeElement()

    def chip(self, *_args, **_kwargs):
        return _FakeElement()


def _phase(**overrides) -> dict:
    phase = {
        "id": "phase-1",
        "phase_name": "Discovery",
        "start_offset": 0,
        "duration": 30,
        "deliverable": "Baselined plan",
        "owner": "Program Manager",
        "color": "#1F3A5F",
        "order": 0,
    }
    phase.update(overrides)
    return phase


def test_calendar_labels_use_duration_as_an_inclusive_day_count(monkeypatch):
    from app.ui.tabs import timeline as timeline_ui

    anchor = date(2026, 6, 1)
    phase = _phase()

    header_ui = _FakeUi()
    monkeypatch.setattr(timeline_ui, "ui", header_ui)
    timeline_ui._render_header_card(
        7,
        [phase],
        anchor,
        "2026-06-01",
        30,
        False,
        lambda: None,
    )
    assert (
        "1 phase · 30 day total span · Jun 01, 2026 → Jun 30, 2026"
        in header_ui.labels
    )

    row_ui = _FakeUi()
    monkeypatch.setattr(timeline_ui, "ui", row_ui)
    timeline_ui._render_gantt_row(phase, anchor, 30)
    timeline_ui._render_phase_row(7, phase, anchor, lambda: None)
    assert "Jun 01 – Jun 30" in row_ui.labels
    assert "Jun 01, 2026 – Jun 30, 2026  (d0–d29)" in row_ui.labels

    axis_ui = _FakeUi()
    monkeypatch.setattr(timeline_ui, "ui", axis_ui)
    timeline_ui._render_day_axis(30, anchor)
    assert axis_ui.labels[-1] == "Jun 30"
    assert timeline_ui._inclusive_end_offset(5, 1) == 5


def test_edit_dialog_does_not_report_success_when_phase_is_missing(monkeypatch):
    from app.ui.tabs import timeline as timeline_ui

    fake_ui = _FakeUi()
    refreshes: list[str] = []
    update_calls: list[tuple[int, str, dict]] = []
    monkeypatch.setattr(timeline_ui, "ui", fake_ui)
    monkeypatch.setattr(
        timeline_ui,
        "update_phase",
        lambda proposal_id, phase_id, **fields: (
            update_calls.append((proposal_id, phase_id, fields)) or None
        ),
    )

    timeline_ui._open_phase_dialog(
        7,
        phase=_phase(),
        on_saved=lambda: refreshes.append("refreshed"),
    )
    save_button = next(
        button
        for button in fake_ui.buttons
        if button["args"] and button["args"][0] == "Save"
    )
    save_button["on_click"]()

    assert len(update_calls) == 1
    assert fake_ui.dialogs[0].closed
    assert refreshes == ["refreshed"]
    assert fake_ui.notifications == [
        (
            "Update failed — phase may have already been removed. "
            "The timeline was refreshed.",
            "warning",
        )
    ]


def test_legacy_idless_phases_keep_identity_across_edit_and_delete(
    inmemory_db,
    monkeypatch,
):
    import app.db.session as db_session
    from app.models import Proposal, RfpPackage
    from app.services import timeline

    monkeypatch.setattr(timeline, "session_scope", db_session.session_scope)
    with db_session.session_scope() as db:
        package = RfpPackage(
            uploaded_at=datetime.now(UTC),
            storage_dir="memory://legacy-timeline",
        )
        db.add(package)
        db.flush()
        proposal = Proposal(
            rfp_package_id=package.id,
            title="Legacy timeline identity",
            timeline_json=json.dumps(
                {
                    "anchor_date": "2026-06-01",
                    "phases": [
                        {
                            "phase_name": "Legacy Discovery",
                            "start_offset": 0,
                            "duration": 10,
                        },
                        {
                            "phase_name": "Legacy Delivery",
                            "start_offset": 10,
                            "duration": 20,
                        },
                    ],
                }
            ),
        )
        db.add(proposal)
        db.flush()
        proposal_id = proposal.id

    first = timeline.get_timeline(proposal_id)
    second = timeline.get_timeline(proposal_id)
    first_ids = {phase["phase_name"]: phase["id"] for phase in first["phases"]}
    second_ids = {
        phase["phase_name"]: phase["id"] for phase in second["phases"]
    }
    assert first_ids == second_ids

    updated = timeline.update_phase(
        proposal_id,
        first_ids["Legacy Discovery"],
        phase_name="Updated Discovery",
    )
    assert updated is not None
    assert updated["phase_name"] == "Updated Discovery"
    assert timeline.delete_phase(
        proposal_id,
        first_ids["Legacy Delivery"],
    )

    persisted = timeline.get_timeline(proposal_id)
    assert persisted["phases"] == [updated]
    with db_session.session_scope() as db:
        stored = json.loads(db.get(Proposal, proposal_id).timeline_json)
    assert stored["phases"][0]["id"] == first_ids["Legacy Discovery"]
