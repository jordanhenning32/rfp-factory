from __future__ import annotations

from types import SimpleNamespace


class _FakeProps(dict):
    def __init__(self, element) -> None:
        super().__init__()
        self.element = element
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.element


class _FakeElement:
    def __init__(self, *, value="") -> None:
        self.value = value
        self.props = _FakeProps(self)

    def classes(self, *_args, **_kwargs):
        return self

    def tooltip(self, *_args, **_kwargs):
        return self

    def open(self):
        return self

    def close(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeUi:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.checkboxes: list[dict] = []
        self.buttons: list[dict] = []
        self.notifications: list[tuple[str, str | None]] = []

    def label(self, text="", **_kwargs):
        self.labels.append(str(text))
        return _FakeElement()

    def checkbox(self, *args, **kwargs):
        element = _FakeElement()
        self.checkboxes.append({"args": args, "element": element, **kwargs})
        return element

    def textarea(self, *args, **kwargs):
        return _FakeElement(value=kwargs.get("value", ""))

    def button(self, *args, **kwargs):
        element = _FakeElement()
        self.buttons.append({"args": args, "element": element, **kwargs})
        return element

    def notify(self, message, *, type=None, **_kwargs):
        self.notifications.append((str(message), type))

    def card(self, *_args, **_kwargs):
        return _FakeElement()

    def dialog(self, *_args, **_kwargs):
        return _FakeElement()

    def element(self, *_args, **_kwargs):
        return _FakeElement()

    def row(self, *_args, **_kwargs):
        return _FakeElement()

    def column(self, *_args, **_kwargs):
        return _FakeElement()


class _EmptyScalarResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _EmptySession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement):
        return _EmptyScalarResult()


def test_zero_matrix_rows_still_render_drafting_commitments(monkeypatch):
    from app.ui.tabs import submission_checklist as checklist

    calls: list[tuple] = []
    fake_ui = SimpleNamespace(refreshable=lambda fn: fn)

    monkeypatch.setattr(checklist, "ui", fake_ui)
    monkeypatch.setattr(checklist, "SessionLocal", lambda: _EmptySession())
    monkeypatch.setattr(
        checklist,
        "_render_system_verified_section",
        lambda proposal_id: calls.append(("system", proposal_id)),
    )
    monkeypatch.setattr(
        checklist,
        "_empty_state",
        lambda message, **kwargs: calls.append(("empty", message, kwargs)),
    )
    monkeypatch.setattr(
        checklist,
        "_render_drafting_commitments",
        lambda proposal_id, **kwargs: calls.append(
            ("commitments", proposal_id, kwargs)
        ),
    )

    checklist._render_submission_checklist_tab(42)

    assert ("system", 42) in calls
    assert any(call[0] == "empty" for call in calls)
    commitment_call = next(call for call in calls if call[0] == "commitments")
    assert commitment_call[1] == 42
    assert commitment_call[2]["hide_obtained"] is False
    assert callable(commitment_call[2]["on_change"])


def test_drafting_commitment_renderer_shows_and_updates_commitment(monkeypatch):
    from app.services import submission_commitments as commitment_service
    from app.ui.tabs import submission_checklist as checklist

    fake_ui = _FakeUi()
    changes: list[str] = []
    toggles: list[tuple[int, bool]] = []
    deletes: list[int] = []
    commitment = {
        "id": 7,
        "description": "Provide the transition plan",
        "obtained": False,
        "source": "needs_human_apply",
        "source_section_id": 12,
        "notes": "Due with final package",
    }

    monkeypatch.setattr(checklist, "ui", fake_ui)
    monkeypatch.setattr(
        commitment_service,
        "list_submission_commitments",
        lambda proposal_id: [commitment] if proposal_id == 42 else [],
    )
    monkeypatch.setattr(
        commitment_service,
        "set_commitment_obtained",
        lambda pk, value: toggles.append((pk, value)),
    )
    monkeypatch.setattr(
        commitment_service,
        "delete_commitment",
        lambda pk: deletes.append(pk) or True,
    )

    checklist._render_drafting_commitments(
        42,
        hide_obtained=False,
        on_change=lambda: changes.append("changed"),
    )

    assert "Drafting commitments" in fake_ui.labels
    assert "Provide the transition plan" in fake_ui.labels
    assert "Notes: Due with final package" in fake_ui.labels
    assert fake_ui.checkboxes[0]["element"].props["aria-label"] == (
        "Obtained status for drafting commitment 7: "
        "Provide the transition plan"
    )

    fake_ui.checkboxes[0]["on_change"](SimpleNamespace(value=True))
    assert toggles == [(7, True)]

    remove_button = next(
        button
        for button in fake_ui.buttons
        if button["args"] and button["args"][0] == "Remove"
    )
    remove_button["on_click"]()
    confirm_remove = fake_ui.buttons[-1]
    assert confirm_remove["args"][0] == "Remove"
    confirm_remove["on_click"]()
    assert deletes == [7]
    assert changes == ["changed", "changed"]
    assert ("Commitment removed.", "positive") in fake_ui.notifications
