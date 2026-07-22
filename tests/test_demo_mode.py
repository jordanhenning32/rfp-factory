"""Tests for the optional demo-only presentation guards."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from app.ui import layout, pages


def test_demo_navigation_hides_data_management(monkeypatch) -> None:
    monkeypatch.setattr(
        layout,
        "get_settings",
        lambda: SimpleNamespace(is_demo=True),
    )

    targets = {target for _, target, _ in layout._visible_nav_items()}

    assert "/kb" not in targets
    assert "/config" not in targets
    assert "/" in targets
    assert "/proposals/new" in targets


def test_normal_navigation_keeps_all_surfaces(monkeypatch) -> None:
    monkeypatch.setattr(
        layout,
        "get_settings",
        lambda: SimpleNamespace(is_demo=False),
    )

    assert layout._visible_nav_items() == layout.NAV_ITEMS


def test_direct_data_management_routes_are_locked_in_demo(monkeypatch) -> None:
    rendered: list[tuple[str, str]] = []

    @contextmanager
    def fake_page_frame(_title: str):
        yield

    def fake_empty_state(message: str, *, icon: str = "info") -> None:
        rendered.append((message, icon))

    monkeypatch.setattr(
        pages,
        "get_settings",
        lambda: SimpleNamespace(is_demo=True),
    )
    monkeypatch.setattr(pages, "page_frame", fake_page_frame)
    monkeypatch.setattr(pages, "_empty_state", fake_empty_state)

    pages.knowledge_base()
    pages.config_page(SimpleNamespace(query_params={}))

    assert len(rendered) == 2
    assert all(icon == "lock" for _, icon in rendered)
    assert all("curated demo workspace" in message.lower() for message, _ in rendered)
