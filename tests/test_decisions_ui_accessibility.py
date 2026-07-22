from __future__ import annotations

from pathlib import Path


class _FakeProps(dict):
    def __init__(self, element) -> None:
        super().__init__()
        self.element = element

    def __call__(self, *_args, **_kwargs):
        return self.element


class _FakeElement:
    def __init__(self) -> None:
        self.props = _FakeProps(self)

    def classes(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeUi:
    def __init__(self) -> None:
        self.buttons: list[dict] = []

    @staticmethod
    def refreshable(fn):
        return fn

    def column(self, *_args, **_kwargs):
        return _FakeElement()

    def card(self, *_args, **_kwargs):
        return _FakeElement()

    def row(self, *_args, **_kwargs):
        return _FakeElement()

    def label(self, *_args, **_kwargs):
        return _FakeElement()

    def button(self, *args, **kwargs):
        element = _FakeElement()
        self.buttons.append({"args": args, "element": element, **kwargs})
        return element


def test_decision_delete_launcher_has_unique_descriptive_accessible_name(
    monkeypatch,
    tmp_path: Path,
):
    from app.core import decisions as decision_service
    from app.ui import pages

    fake_ui = _FakeUi()
    monkeypatch.setattr(pages, "ui", fake_ui)
    monkeypatch.setattr(
        decision_service,
        "DECISIONS_PATH",
        tmp_path / "data" / "decisions.json",
    )
    monkeypatch.setattr(decision_service, "reload_decisions", lambda: {})
    monkeypatch.setattr(
        decision_service,
        "get_decisions_list",
        lambda: [
            {
                "id": "DEC-017",
                "topic": 'Transition evidence for "priority" bids',
                "decision": "Use the approved evidence matrix.",
            }
        ],
    )

    pages._render_decisions_tab()

    assert len(fake_ui.buttons) == 1
    assert fake_ui.buttons[0]["element"].props["aria-label"] == (
        'Delete decision DEC-017: Transition evidence for "priority" bids'
    )
